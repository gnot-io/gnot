"""Seed action — agent_remember.

Store or update a memory entry in the AgentMemoryStore.
The agent calls this to persist facts or notes across sessions.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ASYNC: bool = False


async def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Store a memory entry.

    Args:
        params:
            key (str):           Logical name for the memory (e.g. "preferred_language").
            value (str):         The content to remember.
            entry_type (str):    "fact" (default) or "narrative".
            scope (str):         "global" (default) or "session:{id}".
            session_id (str?):   Session context for session-scoped memories.

    Returns:
        Dict with memory_id, key, value, scope, entry_type.
    """
    key = params.get("key")
    value = params.get("value")
    if not key or value is None:
        raise ValueError("agent_remember requires 'key' and 'value'")

    memory = context.get("memory_store")
    if memory is None:
        logger.warning("agent_remember: memory store not available — ignored")
        return {"status": "skipped", "reason": "memory not enabled"}

    entry = await memory.remember(
        key=str(key),
        value=str(value),
        entry_type=params.get("entry_type", "fact"),
        scope=params.get("scope", "global"),
        session_id=params.get("session_id"),
        source="agent",
    )

    logger.info("agent_remember: key=%s scope=%s", key, entry.scope)
    return {
        "memory_id": entry.memory_id,
        "key": entry.key,
        "value": entry.value,
        "entry_type": entry.entry_type,
        "scope": entry.scope,
    }
