"""CheckpointStore — Persistent JSONL store for suspended task checkpoints.

v6.0 Phase 4 — Task Suspension & Resumption.

When an intent task calls ``suspend_and_ask``, the current LLM message
history (including all tool calls and results) is serialised into a
TaskCheckpoint and written to a JSONL file.  On resume, the checkpoint
is loaded and the ReAct loop is restarted from the saved state with the
answer injected as a tool result.

Storage design:
    - One JSONL file per node: ``{path}/{node_id}-checkpoints.jsonl``
    - Append-only log — updates append a new record with the same
      ``checkpoint_id`` (last-write wins semantics during startup load).
    - In-memory index (dict) is the source of truth at runtime.
    - Dual index: checkpoint_id → checkpoint, question_id → checkpoint_id.
    - Startup scan is O(n) but checkpoint count is expected to stay small.
    - Compaction: rewrite file from memory when record count exceeds
      ``compaction_threshold`` (default 200 records).

Timeout sweep:
    - Background task runs every ``sweep_interval_seconds`` (default 3600).
    - Expired suspended checkpoints are transitioned to ``timed_out`` and
      the caller is notified by emitting a ``clarification.timeout`` event
      (if EventBus is wired in).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from runtime.models import TaskCheckpoint

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SWEEP_INTERVAL_SECONDS: int = 3600
DEFAULT_COMPACTION_THRESHOLD: int = 200


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class TaskSuspendedException(Exception):
    """Raised by the intent handler when the LLM calls suspend_and_ask.

    Carries the full checkpoint data so the caller can persist it and
    return an appropriate response to the API client.
    """

    def __init__(
        self,
        question: str,
        question_id: str,
        ask_node: str,
        target_role: str,
        timeout_seconds: int,
        timeout_action: str,
        assumption: str,
        suspend_tool_call_id: str,
    ) -> None:
        super().__init__(f"Task suspended — question_id={question_id}")
        self.question = question
        self.question_id = question_id
        self.ask_node = ask_node
        self.target_role = target_role
        self.timeout_seconds = timeout_seconds
        self.timeout_action = timeout_action
        self.assumption = assumption
        self.suspend_tool_call_id = suspend_tool_call_id


# ---------------------------------------------------------------------------
# CheckpointStore
# ---------------------------------------------------------------------------

class CheckpointStore:
    """Persistent JSONL store for TaskCheckpoint records.

    Thread-safe via asyncio.Lock.  Dual-indexed in memory for O(1) lookup
    by both ``checkpoint_id`` and ``pending_question_id``.
    """

    def __init__(
        self,
        path: str,
        node_id: str,
        sweep_interval_seconds: int = DEFAULT_SWEEP_INTERVAL_SECONDS,
        compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
        event_bus: Any = None,           # EventBus | None — for timeout events
    ) -> None:
        self._dir = Path(path)
        self._node_id = node_id
        self._sweep_interval = sweep_interval_seconds
        self._compaction_threshold = compaction_threshold
        self._event_bus = event_bus

        self._file = self._dir / f"{node_id}-checkpoints.jsonl"
        self._lock = asyncio.Lock()
        self._sweep_task: asyncio.Task | None = None

        # In-memory indexes
        self._index: dict[str, TaskCheckpoint] = {}         # checkpoint_id → checkpoint
        self._q_index: dict[str, str] = {}                  # question_id → checkpoint_id
        self._record_count: int = 0                         # total records in file (for compaction)

    # ── startup / shutdown ─────────────────────────────────────────────────

    async def startup_load(self) -> int:
        """Load and index all checkpoints from the JSONL file.

        Returns the number of active (non-terminal) checkpoints loaded.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        if not self._file.exists():
            logger.info("CheckpointStore: no existing file at %s", self._file)
            return 0

        loaded = 0
        record_count = 0
        async with self._lock:
            try:
                with self._file.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            cp = TaskCheckpoint(**data)
                            # Last-write-wins: overwrite earlier records
                            self._index[cp.checkpoint_id] = cp
                            self._q_index[cp.pending_question_id] = cp.checkpoint_id
                            record_count += 1
                        except Exception as exc:
                            logger.warning("CheckpointStore: skipping bad record: %s", exc)
            except OSError as exc:
                logger.error("CheckpointStore: could not read %s: %s", self._file, exc)
                return 0

            self._record_count = record_count
            # Count only active checkpoints
            loaded = sum(
                1 for cp in self._index.values()
                if cp.status == "suspended"
            )

        logger.info(
            "CheckpointStore: loaded %d record(s) from %s (%d active)",
            record_count, self._file, loaded,
        )
        return loaded

    def start_sweep_task(self) -> None:
        """Start background timeout sweep loop."""
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(
                self._sweep_loop(), name="checkpoint-sweep"
            )
            logger.debug("CheckpointStore: sweep task started (interval=%ds)", self._sweep_interval)

    async def stop_sweep_task(self) -> None:
        """Cancel the background sweep task."""
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
        self._sweep_task = None

    # ── CRUD ────────────────────────────────────────────────────────────────

    async def save(self, checkpoint: TaskCheckpoint) -> None:
        """Persist a new or updated checkpoint (append-only)."""
        async with self._lock:
            self._index[checkpoint.checkpoint_id] = checkpoint
            self._q_index[checkpoint.pending_question_id] = checkpoint.checkpoint_id
            await self._append_record(checkpoint)

        # Compact if the file is getting large
        if self._record_count >= self._compaction_threshold:
            await self._compact()

    async def update_status(
        self,
        checkpoint_id: str,
        status: str,
        answer: str | None = None,
    ) -> TaskCheckpoint | None:
        """Update status (and optionally answer) of a checkpoint in-place."""
        async with self._lock:
            cp = self._index.get(checkpoint_id)
            if cp is None:
                return None
            # Pydantic model — recreate with updated fields
            updated = cp.model_copy(update={
                "status": status,
                **({"answer": answer, "answered_at": time.time()} if answer is not None else {}),
            })
            self._index[checkpoint_id] = updated
            self._q_index[updated.pending_question_id] = checkpoint_id
            await self._append_record(updated)
            return updated

    async def get_by_checkpoint_id(self, checkpoint_id: str) -> TaskCheckpoint | None:
        """Look up a checkpoint by its checkpoint_id."""
        async with self._lock:
            return self._index.get(checkpoint_id)

    async def get_by_task_id(self, task_id: str) -> TaskCheckpoint | None:
        """Look up the latest checkpoint for a given task_id."""
        async with self._lock:
            # Linear scan — task_id is not indexed but count is small
            for cp in reversed(list(self._index.values())):
                if cp.task_id == task_id:
                    return cp
            return None

    async def get_by_question_id(self, question_id: str) -> TaskCheckpoint | None:
        """Look up a checkpoint by its pending_question_id."""
        async with self._lock:
            cp_id = self._q_index.get(question_id)
            if cp_id is None:
                return None
            return self._index.get(cp_id)

    async def list_active(self) -> list[TaskCheckpoint]:
        """Return all checkpoints with status == 'suspended'."""
        async with self._lock:
            return [cp for cp in self._index.values() if cp.status == "suspended"]

    async def list_all(self) -> list[TaskCheckpoint]:
        """Return all checkpoints regardless of status."""
        async with self._lock:
            return list(self._index.values())

    async def delete(self, checkpoint_id: str) -> bool:
        """Remove a checkpoint from the in-memory index (mark deleted in file)."""
        async with self._lock:
            cp = self._index.pop(checkpoint_id, None)
            if cp is None:
                return False
            self._q_index.pop(cp.pending_question_id, None)
            # Write a tombstone record so startup scan knows it's deleted
            tombstone = {"checkpoint_id": checkpoint_id, "_deleted": True}
            await self._append_line(json.dumps(tombstone))
            return True

    # ── internal ────────────────────────────────────────────────────────────

    async def _append_record(self, checkpoint: TaskCheckpoint) -> None:
        """Write a single record to the JSONL file (must be called under lock or with care)."""
        await self._append_line(checkpoint.model_dump_json())

    async def _append_line(self, line: str) -> None:
        """Low-level: append one line to the JSONL file."""
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._record_count += 1
        except OSError as exc:
            logger.error("CheckpointStore: write failed: %s", exc)

    async def _compact(self) -> None:
        """Rewrite the JSONL file from in-memory state, shrinking it."""
        async with self._lock:
            try:
                tmp = self._file.with_suffix(".jsonl.tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    for cp in self._index.values():
                        fh.write(cp.model_dump_json() + "\n")
                tmp.replace(self._file)
                self._record_count = len(self._index)
                logger.info(
                    "CheckpointStore: compacted → %d records in %s",
                    self._record_count, self._file,
                )
            except OSError as exc:
                logger.error("CheckpointStore: compaction failed: %s", exc)

    # ── timeout sweep ────────────────────────────────────────────────────────

    async def _sweep_loop(self) -> None:
        """Background task: expire timed-out suspended checkpoints."""
        while True:
            try:
                await asyncio.sleep(self._sweep_interval)
                await self._sweep_expired()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("CheckpointStore: sweep error: %s", exc, exc_info=True)

    async def _sweep_expired(self) -> int:
        """Check all suspended checkpoints for timeout expiry."""
        now = time.time()
        expired: list[TaskCheckpoint] = []

        async with self._lock:
            for cp in list(self._index.values()):
                if cp.status != "suspended":
                    continue
                if now - cp.suspended_at >= cp.timeout_seconds:
                    expired.append(cp)

        count = 0
        for cp in expired:
            updated = await self.update_status(cp.checkpoint_id, "timed_out")
            if updated:
                count += 1
                logger.info(
                    "CheckpointStore: checkpoint %s timed out (task=%s, question_id=%s)",
                    cp.checkpoint_id, cp.task_id, cp.pending_question_id,
                )
                # Emit clarification.timeout event if EventBus is available
                if self._event_bus is not None:
                    try:
                        from runtime.models import Event
                        event = Event(
                            event_type="clarification.timeout",
                            source_node=self._node_id,
                            payload={
                                "checkpoint_id": cp.checkpoint_id,
                                "task_id": cp.task_id,
                                "question_id": cp.pending_question_id,
                                "question": cp.pending_question,
                                "assumption": cp.assumption,
                                "timeout_action": cp.timeout_action,
                            },
                            correlation_id=cp.pending_question_id,
                        )
                        await self._event_bus.emit(event)
                    except Exception as exc:
                        logger.error("CheckpointStore: failed to emit timeout event: %s", exc)

        if count:
            logger.info("CheckpointStore: expired %d checkpoint(s)", count)
        return count
