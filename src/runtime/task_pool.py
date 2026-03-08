"""TaskPool — Concurrent task execution manager for v6.0 Phase 4.

Manages the lifecycle of intent tasks:

  - Active tasks:    Running IntentHandler coroutines (capped by max_active_tasks).
  - Suspended tasks: Checkpoints stored in CheckpointStore; unlimited count.

Suspension flow:
    1. IntentHandler detects ``suspend_and_ask`` tool call.
    2. IntentHandler raises TaskSuspendedException with checkpoint data.
    3. TaskPool.handle_suspension() is called by IntentHandler on its way out.
    4. TaskPool saves the checkpoint, decrements active count.
    5. Optionally emits ``clarification.needed`` via EventBus.

Resumption flow:
    1. Answer arrives via POST /tasks/{task_id}/answer or clarification.answered event.
    2. TaskPool.resume_task(question_id, answer) loads the checkpoint.
    3. Creates a new background asyncio Task that continues the ReAct loop
       from the saved message history with the answer injected as a tool result.
    4. Job status updated: SUSPENDED → RESUMING → COMPLETED/FAILED.

Thread safety: asyncio.Lock protects active task counter and task registry.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, TYPE_CHECKING

from runtime.checkpoint_store import CheckpointStore, TaskSuspendedException
from runtime.models import JobStatus, TaskCheckpoint

if TYPE_CHECKING:
    from runtime.intent_handler import IntentHandler
    from runtime.job_manager import JobManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_ACTIVE_TASKS: int = 3


# ---------------------------------------------------------------------------
# TaskPool
# ---------------------------------------------------------------------------

class TaskPool:
    """Manager for concurrent active + unlimited suspended intent tasks.

    Injected into IntentHandler so it can delegate suspension handling.
    Also holds back-references to JobManager (status updates) and EventBus
    (event emission).
    """

    def __init__(
        self,
        checkpoint_store: CheckpointStore,
        job_manager: "JobManager",
        node_id: str,
        max_active_tasks: int = DEFAULT_MAX_ACTIVE_TASKS,
        event_bus: Any = None,          # EventBus | None
        caller_token: str | None = None,
    ) -> None:
        self._checkpoints = checkpoint_store
        self._job_manager = job_manager
        self._node_id = node_id
        self._max_active = max_active_tasks
        self._event_bus = event_bus
        self._caller_token = caller_token

        self._lock = asyncio.Lock()
        self._active_count: int = 0
        # task_id → asyncio.Task (for active resume tasks)
        self._resume_tasks: dict[str, asyncio.Task] = {}

    # ── suspension ──────────────────────────────────────────────────────────

    async def handle_suspension(
        self,
        exc: TaskSuspendedException,
        task_id: str,
        session_id: str,
        original_prompt: str,
        messages: list[dict[str, Any]],
        turn_count: int,
        job_id: str | None = None,
    ) -> TaskCheckpoint:
        """Save checkpoint and emit clarification.needed event.

        Called by IntentHandler after catching TaskSuspendedException.
        Returns the saved checkpoint so the server can build the response.
        """
        question_id = exc.question_id or f"q-{uuid.uuid4().hex[:12]}"

        checkpoint = TaskCheckpoint(
            task_id=task_id,
            session_id=session_id,
            node_id=self._node_id,
            original_prompt=original_prompt,
            messages=messages,
            suspend_tool_call_id=exc.suspend_tool_call_id,
            turn_count=turn_count,
            pending_question=exc.question,
            pending_question_id=question_id,
            asked_node=exc.ask_node,
            target_role=exc.target_role,
            timeout_seconds=exc.timeout_seconds,
            timeout_action=exc.timeout_action,
            assumption=exc.assumption,
            status="suspended",
        )
        await self._checkpoints.save(checkpoint)

        # Update job status to SUSPENDED
        if job_id:
            await self._job_manager.update_job(job_id, status=JobStatus.SUSPENDED)

        # Decrement active task counter
        async with self._lock:
            self._active_count = max(0, self._active_count - 1)

        # Emit clarification.needed event
        await self._emit_clarification_needed(checkpoint)

        logger.info(
            "TaskPool: task %s suspended — question_id=%s ask_node=%s target_role=%s",
            task_id, question_id, exc.ask_node, exc.target_role,
        )
        return checkpoint

    # ── resumption ─────────────────────────────────────────────────────────

    async def resume_task(
        self,
        question_id: str,
        answer: str,
        answered_by: str = "manual",
        intent_handler: "IntentHandler | None" = None,
        job_id: str | None = None,
    ) -> tuple[bool, str]:
        """Resume a suspended task by injecting the answer.

        Args:
            question_id: The pending_question_id from the checkpoint.
            answer: The answer to inject.
            answered_by: Description of who/what provided the answer.
            intent_handler: IntentHandler to use for continuation (required
                            unless the caller will handle execution itself).
            job_id: Optional job ID to update status on.

        Returns:
            (success: bool, message: str)
        """
        checkpoint = await self._checkpoints.get_by_question_id(question_id)
        if checkpoint is None:
            return False, f"No suspended task found for question_id={question_id}"

        if checkpoint.status != "suspended":
            return False, f"Checkpoint status is '{checkpoint.status}', not 'suspended'"

        # Mark checkpoint as answered
        await self._checkpoints.update_status(
            checkpoint.checkpoint_id, "answered", answer=answer
        )

        # Update job status to RESUMING
        if job_id:
            await self._job_manager.update_job(job_id, status=JobStatus.RESUMING)

        # Emit clarification.answered event
        await self._emit_clarification_answered(checkpoint, answer, answered_by)

        if intent_handler is None:
            # Caller will handle execution — we just updated the checkpoint
            logger.info(
                "TaskPool: checkpoint %s answered (no handler — caller manages execution)",
                checkpoint.checkpoint_id,
            )
            return True, f"Answer recorded for question_id={question_id}"

        # Launch background continuation
        resume_task = asyncio.create_task(
            self._run_resume(checkpoint, answer, intent_handler, job_id),
            name=f"resume-{checkpoint.task_id}",
        )
        async with self._lock:
            self._resume_tasks[checkpoint.task_id] = resume_task

        logger.info(
            "TaskPool: resuming task %s (question_id=%s, answered_by=%s)",
            checkpoint.task_id, question_id, answered_by,
        )
        return True, f"Task {checkpoint.task_id} resuming"

    async def resume_by_task_id(
        self,
        task_id: str,
        answer: str,
        answered_by: str = "manual",
        intent_handler: "IntentHandler | None" = None,
        job_id: str | None = None,
    ) -> tuple[bool, str]:
        """Resume a task by task_id (convenience wrapper)."""
        checkpoint = await self._checkpoints.get_by_task_id(task_id)
        if checkpoint is None:
            return False, f"No checkpoint found for task_id={task_id}"
        return await self.resume_task(
            checkpoint.pending_question_id, answer, answered_by, intent_handler, job_id
        )

    # ── status / query ─────────────────────────────────────────────────────

    async def get_status(self) -> dict[str, Any]:
        """Return a summary of active and suspended task counts."""
        active_checkpoints = await self._checkpoints.list_active()
        async with self._lock:
            return {
                "active_tasks": self._active_count,
                "suspended_tasks": len(active_checkpoints),
                "max_active_tasks": self._max_active,
                "resume_tasks_running": len(self._resume_tasks),
            }

    async def list_tasks(self) -> list[dict[str, Any]]:
        """List all checkpoints (active + suspended + recently resolved)."""
        all_checkpoints = await self._checkpoints.list_all()
        return [
            {
                "task_id": cp.task_id,
                "checkpoint_id": cp.checkpoint_id,
                "session_id": cp.session_id,
                "status": cp.status,
                "question": cp.pending_question,
                "question_id": cp.pending_question_id,
                "asked_node": cp.asked_node,
                "target_role": cp.target_role,
                "suspended_at": cp.suspended_at,
                "timeout_seconds": cp.timeout_seconds,
                "assumption": cp.assumption,
                "answer": cp.answer,
                "answered_at": cp.answered_at,
            }
            for cp in all_checkpoints
        ]

    async def cancel_task(self, task_id: str) -> tuple[bool, str]:
        """Cancel a suspended task (mark timed_out, cancel resume if running)."""
        checkpoint = await self._checkpoints.get_by_task_id(task_id)
        if checkpoint is None:
            return False, f"No checkpoint found for task_id={task_id}"

        await self._checkpoints.update_status(checkpoint.checkpoint_id, "timed_out")

        async with self._lock:
            resume_task = self._resume_tasks.pop(task_id, None)
        if resume_task and not resume_task.done():
            resume_task.cancel()

        logger.info("TaskPool: task %s cancelled", task_id)
        return True, f"Task {task_id} cancelled"

    # ── active task tracking ─────────────────────────────────────────────

    async def increment_active(self) -> bool:
        """Increment active task count. Returns False if at max capacity."""
        async with self._lock:
            if self._active_count >= self._max_active:
                return False
            self._active_count += 1
            return True

    async def decrement_active(self) -> None:
        """Decrement active task count (call on task completion/failure)."""
        async with self._lock:
            self._active_count = max(0, self._active_count - 1)

    # ── internal ────────────────────────────────────────────────────────────

    async def _run_resume(
        self,
        checkpoint: TaskCheckpoint,
        answer: str,
        intent_handler: "IntentHandler",
        job_id: str | None,
    ) -> None:
        """Background coroutine: continue the ReAct loop from checkpoint."""
        from runtime.models import IntentRequest

        # Increment active counter
        async with self._lock:
            self._active_count += 1

        try:
            if job_id:
                await self._job_manager.update_job(job_id, status=JobStatus.RUNNING)

            # Mark checkpoint as resumed
            await self._checkpoints.update_status(checkpoint.checkpoint_id, "resumed")

            # Run the continuation via the intent handler
            result = await intent_handler.handle_resume(checkpoint, answer)

            if job_id:
                await self._job_manager.update_job(
                    job_id,
                    status=JobStatus.COMPLETED,
                    output={"reply": result.reply, "turns": result.turns},
                )
            logger.info(
                "TaskPool: task %s resumed and completed (turns=%d)",
                checkpoint.task_id, result.turns,
            )

        except Exception as exc:
            logger.error(
                "TaskPool: resume of task %s failed: %s",
                checkpoint.task_id, exc, exc_info=True,
            )
            if job_id:
                await self._job_manager.update_job(
                    job_id, status=JobStatus.FAILED, error=str(exc)
                )
        finally:
            async with self._lock:
                self._active_count = max(0, self._active_count - 1)
                self._resume_tasks.pop(checkpoint.task_id, None)

    async def _emit_clarification_needed(self, checkpoint: TaskCheckpoint) -> None:
        """Emit clarification.needed event if EventBus is available."""
        if self._event_bus is None:
            return
        try:
            from runtime.models import Event
            event = Event(
                event_type="clarification.needed",
                source_node=self._node_id,
                payload={
                    "task_id": checkpoint.task_id,
                    "session_id": checkpoint.session_id,
                    "question_id": checkpoint.pending_question_id,
                    "question": checkpoint.pending_question,
                    "asked_node": checkpoint.asked_node,
                    "target_role": checkpoint.target_role,
                    "timeout_seconds": checkpoint.timeout_seconds,
                    "assumption": checkpoint.assumption,
                },
                correlation_id=checkpoint.pending_question_id,
                reply_to=self._node_id,
            )
            await self._event_bus.emit(event)
            logger.debug(
                "TaskPool: emitted clarification.needed for question_id=%s",
                checkpoint.pending_question_id,
            )
        except Exception as exc:
            logger.error("TaskPool: failed to emit clarification.needed: %s", exc)

    async def _emit_clarification_answered(
        self,
        checkpoint: TaskCheckpoint,
        answer: str,
        answered_by: str,
    ) -> None:
        """Emit clarification.answered event if EventBus is available."""
        if self._event_bus is None:
            return
        try:
            from runtime.models import Event
            event = Event(
                event_type="clarification.answered",
                source_node=self._node_id,
                payload={
                    "task_id": checkpoint.task_id,
                    "question_id": checkpoint.pending_question_id,
                    "question": checkpoint.pending_question,
                    "answer": answer,
                    "answered_by": answered_by,
                },
                correlation_id=checkpoint.pending_question_id,
            )
            await self._event_bus.emit(event)
        except Exception as exc:
            logger.error("TaskPool: failed to emit clarification.answered: %s", exc)
