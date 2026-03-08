"""Seed action — agent_recall.

Retrieve memory entries from the AgentMemoryStore.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ASYNC: bool = False


async def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Retrieve memory entries.

    Args:
        params:
            query (str?):        Substring search across key + value.
            scope (str?):        Filter by scope ("global", "session:{id}").
            session_id (str?):   Return global + session-scoped for this session.
            entry_type (str?):   "fact" | "narrative" (None = all).
            limit (int):         Max entries to return (default 50).

    Returns:
        Dict with entries list and total count.
    """
    memory = context.get("memory_store")
    if memory is None:
        logger.warning("agent_recall: memory store not available")
        return {"entries": [], "total": 0, "status": "skipped", "reason": "memory not enabled"}

    entries = await memory.recall(
        query=params.get("query"),
        scope=params.get("scope"),
        session_id=params.get("session_id"),
        entry_type=params.get("entry_type"),
        limit=int(params.get("limit", 50)),
    )

    return {
        "entries": [e.to_dict() for e in entries],
        "total": len(entries),
    }
