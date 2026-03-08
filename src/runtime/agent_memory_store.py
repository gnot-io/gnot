"""AgentMemoryStore — persistent cross-session agent memory for GNOT v6.0 Phase 3.

Stores structured facts and narratives that the agent can recall across sessions.
Automatically injected into the LLM system prompt so the agent always has context.

Storage:
    <storage_dir>/
        <node_id>.jsonl     — append-only memory entries

Entry types:
    fact:      key-value structured fact (e.g. key="preferred_language", value="Python")
    narrative: free-form paragraph (e.g. project background, user preferences)

Scopes:
    global:         available in all sessions (default)
    session:{id}:   only injected in matching session

Design:
- Append-only JSONL + in-memory dict (key → MemoryEntry) for fast recall
- forget() writes a tombstone record to disk + removes from in-memory index
- recall_for_prompt() returns a formatted string for LLM system prompt injection
- Thread-safe via asyncio.Lock
- max_entries: LRU eviction when exceeded (by created_at, oldest first)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Memory entry model
# ---------------------------------------------------------------------------

@dataclass
class MemoryEntry:
    """A single memory record."""
    memory_id: str
    key: str                        # logical name (e.g. "preferred_language")
    value: str                      # the remembered fact
    entry_type: str = "fact"        # "fact" | "narrative"
    scope: str = "global"           # "global" | "session:{id}"
    session_id: str | None = None   # associated session (for session-scoped memories)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    source: str = "agent"           # who created this memory

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "key": self.key,
            "value": self.value,
            "entry_type": self.entry_type,
            "scope": self.scope,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryEntry":
        return cls(
            memory_id=d["memory_id"],
            key=d["key"],
            value=d["value"],
            entry_type=d.get("entry_type", "fact"),
            scope=d.get("scope", "global"),
            session_id=d.get("session_id"),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            source=d.get("source", "agent"),
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class AgentMemoryStore:
    """Persistent cross-session memory store.

    Public API:
        remember(key, value, ...) -> MemoryEntry
        recall(query?, scope?, session_id?) -> list[MemoryEntry]
        forget(key?, scope?) -> int
        recall_for_prompt(session_id?) -> str
        startup_load() -> int
    """

    def __init__(
        self,
        storage_dir: str | Path,
        node_id: str = "node",
        max_entries: int = 1000,
        inject_into_prompt: bool = True,
    ) -> None:
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._node_id = node_id
        self._max_entries = max_entries
        self._inject = inject_into_prompt

        # Primary index: key → MemoryEntry (latest per key wins)
        self._entries: dict[str, MemoryEntry] = {}
        self._lock = asyncio.Lock()

    @property
    def _store_file(self) -> Path:
        return self._dir / f"{self._node_id}.jsonl"

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def startup_load(self) -> int:
        """Load entries from disk. Returns count loaded."""
        if not self._store_file.exists():
            return 0

        loaded = 0
        try:
            with open(self._store_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if obj.get("type") == "tombstone":
                        # Forget: remove key from index
                        key = obj.get("key")
                        if key and key in self._entries:
                            del self._entries[key]
                    elif obj.get("type") == "memory":
                        data = obj.get("data", {})
                        entry = MemoryEntry.from_dict(data)
                        self._entries[entry.key] = entry
                        loaded += 1

        except OSError as exc:
            logger.error("Failed to load memory store from %s: %s", self._store_file, exc)

        logger.info(
            "AgentMemoryStore loaded %d active entries from %s",
            len(self._entries), self._store_file,
        )
        return loaded

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def remember(
        self,
        key: str,
        value: str,
        entry_type: str = "fact",
        scope: str = "global",
        session_id: str | None = None,
        source: str = "agent",
    ) -> MemoryEntry:
        """Store or update a memory entry.

        If key already exists, it is overwritten (last-write-wins).
        """
        async with self._lock:
            existing = self._entries.get(key)
            now = time.time()

            if existing and existing.value == value:
                # No change needed
                return existing

            entry = MemoryEntry(
                memory_id=existing.memory_id if existing else _new_memory_id(),
                key=key,
                value=value,
                entry_type=entry_type,
                scope=scope,
                session_id=session_id,
                created_at=existing.created_at if existing else now,
                updated_at=now,
                source=source,
            )
            self._entries[key] = entry

            # LRU eviction if over limit
            if self._max_entries > 0 and len(self._entries) > self._max_entries:
                self._evict_oldest()

            await self._append_record({
                "type": "memory",
                "data": entry.to_dict(),
            })

        action = "updated" if existing else "remembered"
        logger.info("Memory %s: key=%s scope=%s", action, key, scope)
        return entry

    async def recall(
        self,
        query: str | None = None,
        scope: str | None = None,
        session_id: str | None = None,
        entry_type: str | None = None,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        """Retrieve memories matching the given filters.

        Args:
            query:      Substring search across key + value (case-insensitive).
            scope:      Filter by scope ("global", "session:{id}", or None for all).
            session_id: If given, includes global + session-scoped entries for this session.
            entry_type: "fact" | "narrative" | None for all.
            limit:      Max entries to return.
        """
        async with self._lock:
            entries = list(self._entries.values())

        results: list[MemoryEntry] = []
        query_lower = query.lower() if query else None

        for entry in entries:
            # Scope filter
            if scope is not None and entry.scope != scope:
                continue
            if session_id is not None and entry.scope not in ("global", f"session:{session_id}"):
                continue

            # Type filter
            if entry_type is not None and entry.entry_type != entry_type:
                continue

            # Query filter
            if query_lower:
                if (query_lower not in entry.key.lower() and
                        query_lower not in entry.value.lower()):
                    continue

            results.append(entry)

        # Sort by updated_at descending (most recent first)
        results.sort(key=lambda e: e.updated_at, reverse=True)
        return results[:limit]

    async def forget(
        self,
        key: str | None = None,
        scope: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Remove memory entries matching criteria.

        Returns count of entries removed.
        """
        async with self._lock:
            to_remove: list[str] = []

            if key is not None:
                if key in self._entries:
                    to_remove = [key]
            else:
                for k, entry in self._entries.items():
                    if scope is not None and entry.scope != scope:
                        continue
                    if session_id is not None and entry.session_id != session_id:
                        continue
                    to_remove.append(k)

            for k in to_remove:
                del self._entries[k]
                await self._append_record({"type": "tombstone", "key": k, "ts": time.time()})

        if to_remove:
            logger.info("Memory forget: removed %d entries", len(to_remove))
        return len(to_remove)

    def recall_for_prompt(self, session_id: str | None = None) -> str:
        """Return a formatted string suitable for injecting into the LLM system prompt.

        Returns empty string if no memories or inject_into_prompt is False.
        """
        if not self._inject:
            return ""

        # Collect relevant entries synchronously (for use in _build_system_prompt)
        entries = list(self._entries.values())
        if not entries:
            return ""

        # Filter to global + session-scoped
        relevant: list[MemoryEntry] = []
        for entry in entries:
            if entry.scope == "global":
                relevant.append(entry)
            elif session_id and entry.scope == f"session:{session_id}":
                relevant.append(entry)

        if not relevant:
            return ""

        relevant.sort(key=lambda e: e.updated_at, reverse=True)

        lines: list[str] = ["## Agent Memory", ""]
        facts = [e for e in relevant if e.entry_type == "fact"]
        narratives = [e for e in relevant if e.entry_type == "narrative"]

        if facts:
            lines.append("**Facts:**")
            for entry in facts:
                lines.append(f"- {entry.key}: {entry.value}")
            lines.append("")

        if narratives:
            lines.append("**Notes:**")
            for entry in narratives:
                lines.append(f"- {entry.key}: {entry.value}")
            lines.append("")

        return "\n".join(lines)

    @property
    def count(self) -> int:
        """Current number of in-memory entries."""
        return len(self._entries)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evict_oldest(self) -> None:
        """Remove oldest entries to stay within max_entries limit (called under lock)."""
        sorted_keys = sorted(
            self._entries.keys(),
            key=lambda k: self._entries[k].created_at,
        )
        excess = len(self._entries) - self._max_entries
        for k in sorted_keys[:excess]:
            logger.debug("Memory LRU evict: key=%s", k)
            del self._entries[k]

    async def _append_record(self, record: dict[str, Any]) -> None:
        try:
            with open(self._store_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("Failed to write memory record: %s", exc)


# ---------------------------------------------------------------------------

def _new_memory_id() -> str:
    return "mem-" + uuid.uuid4().hex[:12]
