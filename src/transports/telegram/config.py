"""Telegram transport data models.

TelegramBotRecord: persisted state for one registered bot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class TelegramBotRecord:
    """Persisted record for one registered Telegram bot.

    Runtime-only fields (chat_sessions, _instance) are not persisted.
    """
    bot_id: str                     # "bot-alice-abc123"
    bot_token: str                  # bot token (plaintext for now, Phase 8: encrypt)
    bot_username: str               # "@alice_gnot_bot"
    owner_user_id: str              # "alice"
    cluster_id: str                 # which GNOT cluster this bot bridges into
    mode: str                       # "polling" | "webhook"
    webhook_url: str | None = None  # None = long-polling
    active: bool = True
    registered_at: float = field(default_factory=time.time)

    # Runtime state — NOT persisted to disk
    # chat_sessions: dict[int, str]  # telegram_chat_id → gnot_session_id
    # Managed by TelegramBotInstance directly

    def to_dict(self) -> dict:
        """Serialize to dict for JSONL persistence."""
        return {
            "bot_id": self.bot_id,
            "bot_token": self.bot_token,
            "bot_username": self.bot_username,
            "owner_user_id": self.owner_user_id,
            "cluster_id": self.cluster_id,
            "mode": self.mode,
            "webhook_url": self.webhook_url,
            "active": self.active,
            "registered_at": self.registered_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TelegramBotRecord":
        """Deserialize from JSONL dict."""
        return cls(
            bot_id=d["bot_id"],
            bot_token=d["bot_token"],
            bot_username=d.get("bot_username", ""),
            owner_user_id=d.get("owner_user_id", ""),
            cluster_id=d.get("cluster_id", ""),
            mode=d.get("mode", "polling"),
            webhook_url=d.get("webhook_url"),
            active=d.get("active", True),
            registered_at=d.get("registered_at", time.time()),
        )
