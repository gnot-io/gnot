"""Telegram Bot API HTTP client.

Pure HTTP wrapper around the Telegram Bot API.
Zero GNOT dependencies — only aiohttp + standard library.

Supports:
  - get_me()         — validate token, get bot info
  - send_message()   — send text message to a chat
  - send_chat_action() — show typing indicator
  - get_updates()    — long-polling for new messages
  - set_webhook()    — register webhook URL
  - delete_webhook() — remove webhook
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LENGTH = 4096  # Telegram hard limit


class TelegramAPIError(Exception):
    """Raised when Telegram API returns an error response."""
    def __init__(self, description: str, error_code: int = 0) -> None:
        super().__init__(description)
        self.error_code = error_code
        self.description = description


class TelegramBotAPI:
    """Minimal async HTTP client for the Telegram Bot API.

    Designed to be instantiated per-bot (one TelegramBotAPI per bot token).
    Uses a shared aiohttp session for connection pooling.
    """

    def __init__(
        self,
        token: str,
        timeout_seconds: int = 10,
    ) -> None:
        self._token = token
        self._timeout = timeout_seconds
        self._session: Any = None  # aiohttp.ClientSession — lazy init

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize aiohttp session. Call once before using API methods."""
        try:
            import aiohttp
            self._session = aiohttp.ClientSession()
        except ImportError:
            raise ImportError("aiohttp is required for Telegram transport. Install with: pip install aiohttp")

    async def close(self) -> None:
        """Close aiohttp session. Call during bot shutdown."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ── core request ───────────────────────────────────────────────────────

    async def _call(self, method: str, **params: Any) -> Any:
        """Make a Telegram Bot API call. Returns the 'result' field on success.

        Raises TelegramAPIError on API errors.
        """
        if self._session is None:
            await self.start()

        import aiohttp

        url = TELEGRAM_API_BASE.format(token=self._token, method=method)
        # Remove None values
        data = {k: v for k, v in params.items() if v is not None}

        try:
            async with self._session.post(
                url,
                json=data,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                body = await resp.json()
        except Exception as exc:
            raise TelegramAPIError(f"HTTP request failed: {exc}") from exc

        if not body.get("ok"):
            error_code = body.get("error_code", 0)
            description = body.get("description", "Unknown error")
            raise TelegramAPIError(description, error_code)

        return body.get("result")

    # ── API methods ────────────────────────────────────────────────────────

    async def get_me(self) -> dict:
        """Validate token + get bot info. Returns bot user object."""
        return await self._call("getMe")

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = "Markdown",
        reply_to_message_id: int | None = None,
    ) -> dict:
        """Send a text message to a chat. Returns the sent Message object.

        Long messages (> MAX_MESSAGE_LENGTH) are truncated with a note.
        """
        if len(text) > MAX_MESSAGE_LENGTH:
            truncated_note = "\n\n_(truncated — response too long)_"
            text = text[:MAX_MESSAGE_LENGTH - len(truncated_note)] + truncated_note
            logger.warning("send_message: message truncated to %d chars", MAX_MESSAGE_LENGTH)

        return await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
        )

    async def send_chat_action(self, chat_id: int | str, action: str = "typing") -> bool:
        """Send a chat action (e.g. 'typing'). Returns True on success."""
        return await self._call("sendChatAction", chat_id=chat_id, action=action)

    async def get_updates(
        self,
        offset: int | None = None,
        limit: int = 100,
        timeout: int = 30,
    ) -> list[dict]:
        """Long-poll for new updates. Returns list of Update objects.

        offset: ID of the last processed update + 1 (to skip already-seen updates).
        timeout: long-poll timeout in seconds (0 = short-poll).
        """
        result = await self._call(
            "getUpdates",
            offset=offset,
            limit=limit,
            timeout=timeout,
        )
        return result or []

    async def set_webhook(self, url: str) -> bool:
        """Register a webhook URL. Returns True on success."""
        return await self._call("setWebhook", url=url)

    async def delete_webhook(self) -> bool:
        """Remove the currently configured webhook. Returns True on success."""
        return await self._call("deleteWebhook")

    async def get_webhook_info(self) -> dict:
        """Get current webhook configuration."""
        return await self._call("getWebhookInfo")
