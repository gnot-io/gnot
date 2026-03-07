"""Conversation store — in-memory session state for the intent agent loop.

v5.9 — Stores per-session message histories for multi-turn conversations
arriving via POST /intent (Telegram, custom bots, etc.).

Design:
  - Session ID is caller-supplied (e.g. Telegram chat_id) or auto-generated.
  - Messages are stored as raw dicts (OpenAI message format) so they can be
    passed directly to LLMClient.chat() without conversion.
  - TTL is enforced lazily on get() and list_sessions() — no background loop.
  - Thread-safe via asyncio.Lock (same pattern as UploadManager v5.8).
  - On restart: sessions are lost (in-memory). This is intentional — session
    persistence is a P4 item; stateless retry is cleaner than stale history.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session record
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """A single conversation session."""
    session_id: str
    created_at: float
    last_active: float
    messages: list[dict[str, Any]] = field(default_factory=list)

    def add_message(self, role: str, content: str, **extra: Any) -> None:
        msg: dict[str, Any] = {"role": role, "content": content}
        msg.update(extra)
        self.messages.append(msg)
        self.last_active = time.time()

    def add_raw(self, msg: dict[str, Any]) -> None:
        """Append a raw message dict (e.g. assistant tool_calls message)."""
        self.messages.append(msg)
        self.last_active = time.time()

    @property
    def turn_count(self) -> int:
        """Count user turns (one turn = one user message)."""
        return sum(1 for m in self.messages if m.get("role") == "user")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class ConversationStore:
    """Thread-safe in-memory conversation store with lazy TTL.

    Public interface:
        get_or_create(session_id?) -> Session
        get(session_id)            -> Session | None   (None if expired/missing)
        delete(session_id)         -> bool
        list_sessions()            -> list[Session]    (active only)
        sweep_expired()            -> int
    """

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        logger.info("ConversationStore initialised — ttl=%ds", ttl_seconds)

    # ── write ────────────────────────────────────────────────────────────────

    async def get_or_create(self, session_id: str | None = None) -> Session:
        """Return existing session or create a new one."""
        sid = session_id or _new_session_id()

        async with self._lock:
            session = self._sessions.get(sid)

        if session is not None and self._is_expired(session):
            logger.info("Session %s expired — creating fresh session", sid)
            await self.delete(sid)
            session = None

        if session is None:
            now = time.time()
            session = Session(session_id=sid, created_at=now, last_active=now)
            async with self._lock:
                self._sessions[sid] = session
            logger.info("New session created: %s", sid)

        return session

    # ── read ─────────────────────────────────────────────────────────────────

    async def get(self, session_id: str) -> Session | None:
        """Return session or None if not found / expired."""
        async with self._lock:
            session = self._sessions.get(session_id)

        if session is None:
            return None

        if self._is_expired(session):
            logger.info("Session %s expired on access — removing", session_id)
            await self.delete(session_id)
            return None

        return session

    # ── delete ───────────────────────────────────────────────────────────────

    async def delete(self, session_id: str) -> bool:
        async with self._lock:
            existed = session_id in self._sessions
            self._sessions.pop(session_id, None)
        if existed:
            logger.info("Session deleted: %s", session_id)
        return existed

    # ── list ─────────────────────────────────────────────────────────────────

    async def list_sessions(self) -> list[Session]:
        """Return all non-expired sessions, deleting expired ones lazily."""
        async with self._lock:
            snapshot = list(self._sessions.values())

        result: list[Session] = []
        for s in snapshot:
            if self._is_expired(s):
                await self.delete(s.session_id)
            else:
                result.append(s)
        return result

    # ── maintenance ──────────────────────────────────────────────────────────

    async def sweep_expired(self) -> int:
        async with self._lock:
            snapshot = list(self._sessions.values())

        removed = 0
        for s in snapshot:
            if self._is_expired(s):
                await self.delete(s.session_id)
                removed += 1

        if removed:
            logger.info("sweep_expired: removed %d session(s)", removed)
        return removed

    # ── helpers ──────────────────────────────────────────────────────────────

    def _is_expired(self, session: Session) -> bool:
        return (time.time() - session.last_active) > self._ttl


# ---------------------------------------------------------------------------

def _new_session_id() -> str:
    return "sess-" + uuid.uuid4().hex[:12]
