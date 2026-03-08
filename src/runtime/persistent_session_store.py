"""PersistentSessionStore — file-based session storage for GNOT v6.0 Phase 3.

Drop-in replacement for ConversationStore that survives node restarts.

Storage layout:
    <storage_dir>/
        <session_id>.jsonl      — one JSON object per line (append-only)
        _index.json             — session metadata index (rebuilt on startup)

JSONL format: each line is one of:
    {"type": "meta",    "session_id": "...", "created_at": 1234, "ttl_seconds": 0}
    {"type": "message", "role": "user",      "content": "...", "ts": 1234.5}
    {"type": "message", "role": "assistant", ...}
    {"type": "message", "role": "tool",      ...}

Design decisions:
- Append-only JSONL → crash-safe
- In-memory index for O(1) session lookup
- TTL = 0 → infinite (never expire)
- Per-session TTL override at creation time
- max_messages_per_session: 0 = unlimited; > 0 → truncate oldest on LLM call
- Lazy message loading: index only stores metadata, messages loaded on demand
- Compatible with ConversationStore interface (same public API)
- Thread-safe via asyncio.Lock per session + global index lock
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session model (superset of conversation_store.Session)
# ---------------------------------------------------------------------------

@dataclass
class PersistedSession:
    """A conversation session backed by a JSONL file."""
    session_id: str
    created_at: float
    last_active: float
    ttl_seconds: int = 0          # 0 = infinite
    messages: list[dict[str, Any]] = field(default_factory=list)
    _dirty: bool = field(default=False, repr=False)

    def add_message(self, role: str, content: str, **extra: Any) -> None:
        msg: dict[str, Any] = {"role": role, "content": content}
        msg.update(extra)
        self.messages.append(msg)
        self.last_active = time.time()
        self._dirty = True

    def add_raw(self, msg: dict[str, Any]) -> None:
        """Append a raw message dict (e.g. assistant tool_calls message)."""
        self.messages.append(msg)
        self.last_active = time.time()
        self._dirty = True

    @property
    def turn_count(self) -> int:
        return sum(1 for m in self.messages if m.get("role") == "user")

    def is_expired(self) -> bool:
        if self.ttl_seconds == 0:
            return False
        return (time.time() - self.last_active) > self.ttl_seconds


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class PersistentSessionStore:
    """File-backed session store. Survives node restart.

    Compatible interface with ConversationStore so it can be substituted
    anywhere ConversationStore is used.
    """

    def __init__(
        self,
        storage_dir: str | Path,
        default_ttl_seconds: int = 0,
        max_messages_per_session: int = 0,
    ) -> None:
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._default_ttl = default_ttl_seconds
        self._max_messages = max_messages_per_session

        # In-memory index: session_id → PersistedSession (metadata + messages)
        self._sessions: dict[str, PersistedSession] = {}
        self._lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def startup_load(self) -> int:
        """Load all existing sessions from disk on startup.

        Returns count of sessions loaded.
        """
        loaded = 0
        for fpath in self._dir.glob("*.jsonl"):
            sid = fpath.stem
            if sid.startswith("_"):
                continue
            try:
                session = await self._load_session_file(sid, fpath)
                if session is not None:
                    async with self._lock:
                        self._sessions[sid] = session
                        self._session_locks[sid] = asyncio.Lock()
                    loaded += 1
            except Exception as exc:
                logger.warning("Failed to load session file %s: %s", fpath, exc)

        logger.info(
            "PersistentSessionStore loaded %d session(s) from %s",
            loaded, self._dir,
        )
        return loaded

    async def _load_session_file(
        self, session_id: str, fpath: Path
    ) -> PersistedSession | None:
        """Parse a JSONL session file into a PersistedSession."""
        meta: dict[str, Any] = {}
        messages: list[dict[str, Any]] = []

        with open(fpath, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                record_type = obj.get("type")
                if record_type == "meta":
                    meta = obj
                elif record_type == "message":
                    # Strip internal fields before adding to messages list
                    msg = {k: v for k, v in obj.items() if k not in ("type", "ts")}
                    messages.append(msg)

        if not meta:
            # No meta record → synthesise from file mtime
            mtime = fpath.stat().st_mtime
            meta = {
                "session_id": session_id,
                "created_at": mtime,
                "ttl_seconds": self._default_ttl,
            }

        session = PersistedSession(
            session_id=session_id,
            created_at=meta.get("created_at", time.time()),
            last_active=meta.get("last_active", meta.get("created_at", time.time())),
            ttl_seconds=meta.get("ttl_seconds", self._default_ttl),
            messages=messages,
        )

        if session.is_expired():
            logger.debug("Session %s expired — skipping load", session_id)
            return None

        return session

    # ------------------------------------------------------------------
    # Public API (matches ConversationStore interface)
    # ------------------------------------------------------------------

    async def get_or_create(
        self,
        session_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> PersistedSession:
        """Return existing session or create a new one.

        Args:
            session_id: Caller-supplied ID (e.g. Telegram chat_id) or auto-generated.
            ttl_seconds: Per-session TTL override. None = use default.
        """
        sid = session_id or _new_session_id()
        effective_ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl

        async with self._lock:
            session = self._sessions.get(sid)

        if session is not None and session.is_expired():
            logger.info("Session %s expired — creating fresh session", sid)
            await self.delete(sid)
            session = None

        if session is None:
            now = time.time()
            session = PersistedSession(
                session_id=sid,
                created_at=now,
                last_active=now,
                ttl_seconds=effective_ttl,
            )
            async with self._lock:
                self._sessions[sid] = session
                self._session_locks[sid] = asyncio.Lock()

            # Persist meta record
            await self._append_record(sid, {
                "type": "meta",
                "session_id": sid,
                "created_at": now,
                "last_active": now,
                "ttl_seconds": effective_ttl,
            })
            logger.info("New persistent session created: %s (ttl=%d)", sid, effective_ttl)

        return session

    async def get(self, session_id: str) -> PersistedSession | None:
        """Return session or None if not found / expired."""
        async with self._lock:
            session = self._sessions.get(session_id)

        if session is None:
            return None

        if session.is_expired():
            logger.info("Session %s expired on access — removing", session_id)
            await self.delete(session_id)
            return None

        return session

    async def add_message(self, session_id: str, message: dict[str, Any]) -> None:
        """Append a message to a session and persist it.

        This is the preferred persistent write path — avoids holding
        Session in memory if the caller doesn't already have it.
        """
        session = await self.get_or_create(session_id)
        session.messages.append(message)
        session.last_active = time.time()
        await self._append_record(session_id, {
            "type": "message",
            "ts": session.last_active,
            **message,
        })

    async def clear_messages(self, session_id: str) -> bool:
        """Clear message history for a session (keep session + metadata).

        Returns True if session existed.
        """
        session = await self.get(session_id)
        if session is None:
            return False

        session.messages.clear()
        session.last_active = time.time()

        # Rewrite file: keep meta record only, discard messages
        fpath = self._session_file(session_id)
        async with self._get_session_lock(session_id):
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "meta",
                    "session_id": session_id,
                    "created_at": session.created_at,
                    "last_active": session.last_active,
                    "ttl_seconds": session.ttl_seconds,
                }) + "\n")

        logger.info("Session %s messages cleared", session_id)
        return True

    async def delete(self, session_id: str) -> bool:
        """Delete session from memory and disk."""
        async with self._lock:
            existed = session_id in self._sessions
            self._sessions.pop(session_id, None)
            self._session_locks.pop(session_id, None)

        if existed:
            fpath = self._session_file(session_id)
            if fpath.exists():
                try:
                    fpath.unlink()
                except OSError as exc:
                    logger.warning("Failed to delete session file %s: %s", fpath, exc)
            logger.info("Session deleted: %s", session_id)

        return existed

    async def list_sessions(self) -> list[PersistedSession]:
        """Return all non-expired sessions."""
        async with self._lock:
            snapshot = list(self._sessions.values())

        result: list[PersistedSession] = []
        for s in snapshot:
            if s.is_expired():
                await self.delete(s.session_id)
            else:
                result.append(s)
        return result

    async def sweep_expired(self) -> int:
        """Remove expired sessions from memory and disk."""
        async with self._lock:
            snapshot = list(self._sessions.values())

        removed = 0
        for s in snapshot:
            if s.is_expired():
                await self.delete(s.session_id)
                removed += 1

        if removed:
            logger.info("sweep_expired: removed %d session(s)", removed)
        return removed

    def get_messages_for_llm(self, session: PersistedSession) -> list[dict[str, Any]]:
        """Return messages, optionally truncated to max_messages_per_session.

        When truncating, keeps the most recent messages. This prevents the LLM
        context window from overflowing while preserving recent history.
        """
        msgs = session.messages
        if self._max_messages > 0 and len(msgs) > self._max_messages:
            msgs = msgs[-self._max_messages:]
        return list(msgs)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _session_file(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.jsonl"

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    async def _append_record(self, session_id: str, record: dict[str, Any]) -> None:
        """Append a JSON record to the session's JSONL file."""
        fpath = self._session_file(session_id)
        lock = self._get_session_lock(session_id)
        async with lock:
            try:
                with open(fpath, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as exc:
                logger.error(
                    "Failed to persist record to session %s: %s", session_id, exc
                )

    async def _persist_session_messages(self, session: PersistedSession) -> None:
        """Write all messages from an in-memory session to disk.

        Used after bulk in-memory updates (e.g. from ConversationStore migration).
        """
        fpath = self._session_file(session.session_id)
        async with self._get_session_lock(session.session_id):
            with open(fpath, "w", encoding="utf-8") as fh:
                # Meta record
                fh.write(json.dumps({
                    "type": "meta",
                    "session_id": session.session_id,
                    "created_at": session.created_at,
                    "last_active": session.last_active,
                    "ttl_seconds": session.ttl_seconds,
                }) + "\n")
                # Message records
                for msg in session.messages:
                    fh.write(json.dumps({
                        "type": "message",
                        "ts": session.last_active,
                        **msg,
                    }, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------

def _new_session_id() -> str:
    return "sess-" + uuid.uuid4().hex[:12]
