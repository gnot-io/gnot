"""handle_clarification_timeout — v6.0 Phase 4 seed action.

Invoked when a clarification.timeout event is received — i.e. a suspended
task waited too long for an answer and the CheckpointStore sweep has expired it.

Depending on the checkpoint's ``timeout_action`` field:
  - ``use_assumption``: injects the assumption as the answer and resumes.
  - ``fail``: marks the task as failed without resuming.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_clarification_timeout(
    checkpoint_id: str,
    question_id: str,
    question: str = "",
    assumption: str = "",
    timeout_action: str = "use_assumption",
    task_pool: Any = None,
    intent_handler: Any = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Handle a timed-out clarification.

    Args:
        checkpoint_id: The checkpoint_id of the timed-out task.
        question_id: The pending_question_id.
        question: The original question (for logging).
        assumption: The fallback value to use if timeout_action == 'use_assumption'.
        timeout_action: 'use_assumption' | 'fail'.
        task_pool: Injected TaskPool instance.
        intent_handler: Injected IntentHandler instance.

    Returns:
        dict with success/error status.
    """
    if task_pool is None:
        logger.error("handle_clarification_timeout: task_pool not available")
        return {"success": False, "error": "task_pool not available"}

    logger.info(
        "handle_clarification_timeout: question_id=%s action=%s assumption=%r",
        question_id, timeout_action, assumption,
    )

    if timeout_action == "fail":
        # Cancel the task without resuming
        checkpoint = await task_pool._checkpoints.get_by_question_id(question_id)
        if checkpoint:
            ok, msg = await task_pool.cancel_task(checkpoint.task_id)
            return {
                "success": ok,
                "action": "failed",
                "message": msg,
                "question_id": question_id,
            }
        return {
            "success": False,
            "error": f"No checkpoint found for question_id={question_id}",
        }

    # Default: use_assumption — resume with the assumption as the answer
    answer = assumption or "(No assumption provided — proceeding with best judgment)"

    success, message = await task_pool.resume_task(
        question_id=question_id,
        answer=f"[TIMEOUT] No answer received. Using assumption: {answer}",
        answered_by="timeout-handler",
        intent_handler=intent_handler,
    )

    return {
        "success": success,
        "action": "resumed_with_assumption",
        "assumption_used": answer,
        "message": message,
        "question_id": question_id,
    }

# ActionLoader expects a callable named 'run' — alias the main function.
run = handle_clarification_timeout
