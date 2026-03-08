"""ExternalParticipantRegistry — persistent registry of external participants.

v6.0 Phase 6 — External Participant Interaction.

Stores ExternalParticipant records with role-based lookup.  Backed by a
JSONL file for crash-safe durability.  In-memory indexes provide O(1)
lookups at runtime.

Storage:
    {path}/{node_id}-participants.jsonl
    Append-only; startup scans the file and last-write-wins per participant_id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from runtime.models import ExternalParticipant

logger = logging.getLogger(__name__)


class ParticipantNotFoundError(Exception):
    """Raised when a participant_id is not found in the registry."""


class ParticipantAuthError(Exception):
    """Raised when the provided auth token does not match the participant record."""


class ExternalParticipantRegistry:
    """Manages ExternalParticipant records with JSONL persistence.

    Indexes:
        _by_id:   participant_id → ExternalParticipant
        _by_role: role → set[participant_id]
    """

    def __init__(self, path: str, node_id: str) -> None:
        self._path = Path(path)
        self._node_id = node_id
        self._file: Path | None = None
        self._by_id: dict[str, ExternalParticipant] = {}
        self._by_role: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    # ── startup ────────────────────────────────────────────────────────────

    async def startup_load(self) -> int:
        """Load participants from JSONL file.  Returns count loaded."""
        self._path.mkdir(parents=True, exist_ok=True)
        self._file = self._path / f"{self._node_id}-participants.jsonl"
        if not self._file.exists():
            return 0

        loaded = 0
        with self._file.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    p = ExternalParticipant(**data)
                    # Last-write-wins
                    self._put(p)
                    loaded += 1
                except Exception as exc:
                    logger.warning("ExternalParticipantRegistry: skip corrupt record — %s", exc)

        logger.info("ExternalParticipantRegistry: loaded %d participant(s)", loaded)
        return loaded

    # ── write helpers ──────────────────────────────────────────────────────

    def _put(self, participant: ExternalParticipant) -> None:
        """Update in-memory index (does NOT write to disk)."""
        old = self._by_id.get(participant.participant_id)
        if old:
            for role in old.roles:
                self._by_role.setdefault(role, set()).discard(old.participant_id)
        self._by_id[participant.participant_id] = participant
        for role in participant.roles:
            self._by_role.setdefault(role, set()).add(participant.participant_id)

    async def _append(self, participant: ExternalParticipant) -> None:
        """Append participant JSON to JSONL file."""
        if self._file is None:
            return
        try:
            with self._file.open("a") as fh:
                fh.write(participant.model_dump_json() + "\n")
        except Exception as exc:
            logger.error("ExternalParticipantRegistry: write error — %s", exc)

    # ── public API ─────────────────────────────────────────────────────────

    async def register(self, participant: ExternalParticipant) -> ExternalParticipant:
        """Register a new participant.  Returns the saved record."""
        async with self._lock:
            self._put(participant)
            await self._append(participant)
        logger.info(
            "ExternalParticipantRegistry: registered %s roles=%s cluster=%s",
            participant.participant_id, participant.roles, participant.cluster_id,
        )
        return participant

    async def get(self, participant_id: str) -> ExternalParticipant:
        """Get participant by ID.  Raises ParticipantNotFoundError if absent."""
        p = self._by_id.get(participant_id)
        if p is None:
            raise ParticipantNotFoundError(participant_id)
        return p

    async def list_all(
        self,
        cluster_id: str | None = None,
        active_only: bool = True,
    ) -> list[ExternalParticipant]:
        """List participants, optionally filtered by cluster and active status."""
        result = list(self._by_id.values())
        if cluster_id is not None:
            result = [p for p in result if p.cluster_id == cluster_id]
        if active_only:
            result = [p for p in result if p.active]
        return result

    async def list_by_role(
        self,
        role: str,
        cluster_id: str | None = None,
        active_only: bool = True,
    ) -> list[ExternalParticipant]:
        """Return participants that have the given role."""
        ids = self._by_role.get(role, set())
        result: list[ExternalParticipant] = []
        for pid in ids:
            p = self._by_id.get(pid)
            if p is None:
                continue
            if active_only and not p.active:
                continue
            if cluster_id is not None and p.cluster_id != cluster_id:
                continue
            result.append(p)
        return result

    async def update(
        self,
        participant_id: str,
        updates: dict[str, Any],
    ) -> ExternalParticipant:
        """Apply partial updates to a participant record.  Returns updated record."""
        async with self._lock:
            p = self._by_id.get(participant_id)
            if p is None:
                raise ParticipantNotFoundError(participant_id)
            data = p.model_dump()
            data.update({k: v for k, v in updates.items() if v is not None})
            updated = ExternalParticipant(**data)
            self._put(updated)
            await self._append(updated)
        logger.info("ExternalParticipantRegistry: updated %s", participant_id)
        return updated

    async def deactivate(self, participant_id: str) -> ExternalParticipant:
        """Deactivate (soft-delete) a participant."""
        return await self.update(participant_id, {"active": False})

    def verify_auth(self, participant: ExternalParticipant, token: str) -> None:
        """Raise ParticipantAuthError if token does not match."""
        if participant.auth_token != token:
            raise ParticipantAuthError(f"Invalid auth token for {participant.participant_id}")

    def count(self) -> int:
        return len(self._by_id)
