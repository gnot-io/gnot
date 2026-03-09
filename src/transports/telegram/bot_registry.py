"""BotInstanceRegistry — persists and manages N TelegramBotRecords.

Persistence: JSONL file (one JSON record per line, last write wins per bot_id).
Bots survive node restart — loaded from disk on startup.

Thread-safety: asyncio-safe (single event loop, no threading).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from transports.telegram.config import TelegramBotRecord

logger = logging.getLogger(__name__)


class BotRegistryError(Exception):
    """Raised for bot registry errors (duplicate bot_id, etc.)."""


class BotInstanceRegistry:
    """Manages persisted TelegramBotRecords.

    One record per registered bot. Records are loaded at startup and
    persisted to a JSONL file (append-only log, compacted on save).
    """

    def __init__(self, storage_path: str) -> None:
        self._storage_path = Path(storage_path)
        self._records: dict[str, TelegramBotRecord] = {}  # bot_id → record

    # ── startup ────────────────────────────────────────────────────────────

    async def startup_load(self) -> int:
        """Load records from disk. Returns number of loaded bots."""
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._storage_path.exists():
            return 0

        loaded = 0
        try:
            with open(self._storage_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        record = TelegramBotRecord.from_dict(data)
                        self._records[record.bot_id] = record
                        loaded += 1
                    except Exception as exc:
                        logger.warning("BotRegistry: skipping malformed record: %s", exc)
        except Exception as exc:
            logger.error("BotRegistry: failed to load from %s: %s", self._storage_path, exc)

        active = sum(1 for r in self._records.values() if r.active)
        logger.info(
            "BotRegistry: loaded %d records (%d active) from %s",
            loaded, active, self._storage_path,
        )
        return loaded

    # ── CRUD ───────────────────────────────────────────────────────────────

    def generate_bot_id(self, owner_user_id: str) -> str:
        """Generate a unique bot_id."""
        short = uuid.uuid4().hex[:6]
        return f"bot-{owner_user_id}-{short}"

    async def register(self, record: TelegramBotRecord) -> TelegramBotRecord:
        """Persist a new bot record. Raises if bot_id already exists and is active."""
        if record.bot_id in self._records and self._records[record.bot_id].active:
            raise BotRegistryError(f"Bot {record.bot_id} already registered and active")
        self._records[record.bot_id] = record
        await self._persist()
        return record

    async def deactivate(self, bot_id: str) -> TelegramBotRecord:
        """Soft-delete a bot (mark inactive). Raises KeyError if not found."""
        if bot_id not in self._records:
            raise KeyError(f"Bot {bot_id} not found")
        record = self._records[bot_id]
        record.active = False
        await self._persist()
        return record

    def get(self, bot_id: str) -> TelegramBotRecord | None:
        """Return record by bot_id, or None if not found."""
        return self._records.get(bot_id)

    def list_active(self) -> list[TelegramBotRecord]:
        """Return all active bot records."""
        return [r for r in self._records.values() if r.active]

    def list_all(self) -> list[TelegramBotRecord]:
        """Return all records (including inactive)."""
        return list(self._records.values())

    def find_by_token(self, token: str) -> TelegramBotRecord | None:
        """Find active record by bot token."""
        for r in self._records.values():
            if r.bot_token == token and r.active:
                return r
        return None

    def find_by_cluster(self, cluster_id: str) -> list[TelegramBotRecord]:
        """Return all active bots for a cluster."""
        return [r for r in self._records.values() if r.cluster_id == cluster_id and r.active]

    # ── persistence ────────────────────────────────────────────────────────

    async def _persist(self) -> None:
        """Write all records to disk (compact JSONL — one line per bot)."""
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._storage_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                for record in self._records.values():
                    fh.write(json.dumps(record.to_dict()) + "\n")
            # Atomic rename
            os.replace(tmp_path, self._storage_path)
        except Exception as exc:
            logger.error("BotRegistry: failed to persist to %s: %s", self._storage_path, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
