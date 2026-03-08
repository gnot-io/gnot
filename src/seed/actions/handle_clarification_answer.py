"""handle_clarification_answer — v6.0 Phase 4 seed action.

Invoked when a clarification.answered event is received by the EventBus
subscription wired at startup.

Event payload format:
    {
        "question_id": "q-abc123",
        "answer": "Use snake_case for all field names.",
        "answered_by": "architect-A"
    }

The action looks up the suspended TaskCheckpoint by question_id and
resumes the task via TaskPool.resume_task().

Context injection:
    ActionExecutor injects ``memory_store`` into action kwargs when available.
    For TaskPool access, we use the global app state reference stored in the
    action's execution context (action_context["task_pool"]).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_clarification_answer(
    question_id: str,
    answer: str,
    answered_by: str = "unknown",
    # Injected by ActionExecutor via action_context
    task_pool: Any = None,
    intent_handler: Any = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Resume a suspended task with the provided answer.

    Args:
        question_id: The pending_question_id of the suspended checkpoint.
        answer: The answer to inject into the conversation.
        answered_by: Description of who provided the answer.
        task_pool: Injected TaskPool instance (from action_context).
        intent_handler: Injected IntentHandler instance (from action_context).

    Returns:
        dict with success/error status and task_id.
    """
    if task_pool is None:
        logger.error("handle_clarification_answer: task_pool not available in action context")
        return {
            "success": False,
            "error": "task_pool not available — Phase 4 not configured",
        }

    success, message = await task_pool.resume_task(
        question_id=question_id,
        answer=answer,
        answered_by=answered_by,
        intent_handler=intent_handler,
    )

    if success:
        logger.info(
            "handle_clarification_answer: resumed question_id=%s answered_by=%s",
            question_id, answered_by,
        )
        return {"success": True, "message": message, "question_id": question_id}
    else:
        logger.warning(
            "handle_clarification_answer: failed to resume question_id=%s — %s",
            question_id, message,
        )
        return {"success": False, "error": message, "question_id": question_id}

# ActionLoader expects a callable named 'run' — alias the main function.
run = handle_clarification_answer
