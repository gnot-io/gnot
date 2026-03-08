"""Seed action — agent_forget.

Remove memory entries from the AgentMemoryStore.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ASYNC: bool = False


async def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Remove memory entries.

    Args:
        params:
            key (str?):          Remove exactly this key.
            scope (str?):        Remove all entries with this scope.
            session_id (str?):   Remove all entries for this session.
            At least one of key, scope, or session_id must be provided.

    Returns:
        Dict with removed count.
    """
    key = params.get("key")
    scope = params.get("scope")
    session_id = params.get("session_id")

    if not any([key, scope, session_id]):
        raise ValueError("agent_forget requires at least one of: key, scope, session_id")

    memory = context.get("memory_store")
    if memory is None:
        logger.warning("agent_forget: memory store not available")
        return {"removed": 0, "status": "skipped", "reason": "memory not enabled"}

    removed = await memory.forget(
        key=key,
        scope=scope,
        session_id=session_id,
    )

    logger.info("agent_forget: removed=%d key=%s scope=%s", removed, key, scope)
    return {"removed": removed}
