"""TelegramBridge — orchestrator for all Telegram bot instances.

Implements ChannelTransportBridge ABC.
Manages:
  - N TelegramBotInstance objects (one per registered bot)
  - BotInstanceRegistry (persists bot records to disk)
  - Bot management API (register/list/deregister)
  - Pattern 1 notification routing (notify() method)

Architecture rule: transports/ may import from runtime/ ABC only.
runtime/ must never import from transports/.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from runtime.transport_bridge import ChannelTransportBridge
from runtime.models import ExternalParticipant, InteractionThread

from transports.telegram.bot_instance import TelegramBotInstance
from transports.telegram.bot_registry import BotInstanceRegistry
from transports.telegram.config import TelegramBotRecord
from transports.telegram.bot_api import TelegramBotAPI, TelegramAPIError
from transports.telegram.webhook_router import build_webhook_router

if TYPE_CHECKING:
    from runtime.config import NodeConfig

logger = logging.getLogger(__name__)


class TelegramBridge(ChannelTransportBridge):
    """Transport bridge for Telegram.

    Manages N bot instances (one per registered bot token).
    Routes Pattern 1 (Q&A) notifications to the right bot instance
    based on ExternalParticipant.transport_target (= Telegram user_id/chat_id).
    """

    transport_id = "telegram"

    def __init__(self, config: "NodeConfig") -> None:
        self._config = config
        tg = config.telegram

        # BotInstanceRegistry — persists records to disk
        self._registry = BotInstanceRegistry(storage_path=tg.bots_storage_path)

        # Active bot instances (bot_id → instance)
        self._instances: dict[str, TelegramBotInstance] = {}

        # Settings
        self._message_format = tg.default_message_format
        self._language = tg.default_language
        self._poll_interval = tg.poll_interval_seconds
        self._poll_timeout = tg.poll_timeout_seconds
        self._api_timeout = tg.api_timeout_seconds

        # GNOT base URL for bot instances to call back into GNOT
        # Defaults to http://localhost:{port} if not configured
        self._gnot_base_url = tg.gnot_base_url or f"http://localhost:{config.port}"
        self._gnot_auth_token = config.auth_token

        self._ready = False
        self._router: APIRouter | None = None

    # ── ChannelTransportBridge ABC ─────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def startup(self) -> None:
        """Load persisted bots and start all active instances."""
        loaded = await self._registry.startup_load()
        logger.info("TelegramBridge: loaded %d bot record(s) from registry", loaded)

        # Start all active bots
        for record in self._registry.list_active():
            await self._start_instance(record)

        # Build FastAPI router
        self._router = build_webhook_router(self)
        self._ready = True

        active = len(self._instances)
        logger.info("TelegramBridge: startup complete — %d instance(s) active", active)

    async def shutdown(self) -> None:
        """Stop all bot instances gracefully."""
        self._ready = False
        for bot_id, instance in list(self._instances.items()):
            try:
                await instance.stop()
                logger.info("TelegramBridge: stopped instance bot=%s", bot_id)
            except Exception as exc:
                logger.error("TelegramBridge: error stopping bot=%s: %s", bot_id, exc)
        self._instances.clear()
        logger.info("TelegramBridge: shutdown complete")

    async def notify(
        self,
        participant: ExternalParticipant,
        thread: InteractionThread,
    ) -> None:
        """Push question notification to a Telegram participant (Pattern 1).

        Finds the bot instance for participant's cluster, then sends a
        formatted question notification to participant.transport_target (chat_id).
        Must not raise — catch and log all errors.
        """
        try:
            chat_id_str = participant.transport_target
            if not chat_id_str:
                logger.warning(
                    "TelegramBridge.notify: participant=%s has no transport_target",
                    participant.participant_id,
                )
                return

            try:
                chat_id = int(chat_id_str)
            except ValueError:
                logger.error(
                    "TelegramBridge.notify: invalid transport_target=%s (not a chat_id int)",
                    chat_id_str,
                )
                return

            # Find a bot instance for this cluster
            instance = self._find_instance_for_cluster(thread.cluster_id)
            if instance is None:
                logger.warning(
                    "TelegramBridge.notify: no active bot instance for cluster=%s",
                    thread.cluster_id,
                )
                return

            await instance.send_question_notification(
                chat_id=chat_id,
                question_id=thread.question_id,
                question_text=thread.question_text,
                required_role=thread.required_role,
                source_agent=thread.source_agent,
                cluster_id=thread.cluster_id,
            )
        except Exception as exc:
            # Must not propagate — router logs and moves on
            logger.error(
                "TelegramBridge.notify: failed to notify participant=%s: %s",
                participant.participant_id, exc,
            )

    def get_fastapi_router(self) -> APIRouter | None:
        """Return the webhook FastAPI router (built during startup)."""
        return self._router

    # ── Bot management ─────────────────────────────────────────────────────

    async def register_bot(
        self,
        bot_token: str,
        owner_user_id: str,
        cluster_id: str,
        webhook_url: str | None = None,
    ) -> dict:
        """Register a new Telegram bot. Called by POST /transports/telegram/bots.

        Validates the token with Telegram, determines mode (webhook/polling),
        persists the record, and starts a TelegramBotInstance.

        Returns dict with bot_id, bot_username, mode, status.
        """
        # Check for duplicate token
        existing = self._registry.find_by_token(bot_token)
        if existing is not None:
            raise ValueError(f"Bot token already registered as {existing.bot_id}")

        # Validate token + get bot username
        tmp_api = TelegramBotAPI(token=bot_token, timeout_seconds=self._api_timeout)
        try:
            await tmp_api.start()
            bot_info = await tmp_api.get_me()
        except TelegramAPIError as exc:
            raise ValueError(f"Invalid bot token: {exc}") from exc
        finally:
            await tmp_api.close()

        bot_username = "@" + bot_info.get("username", "unknown")
        bot_id = self._registry.generate_bot_id(owner_user_id)

        # Determine mode
        if webhook_url:
            mode = "webhook"
        else:
            mode = "polling"

        record = TelegramBotRecord(
            bot_id=bot_id,
            bot_token=bot_token,
            bot_username=bot_username,
            owner_user_id=owner_user_id,
            cluster_id=cluster_id,
            mode=mode,
            webhook_url=webhook_url,
            active=True,
            registered_at=time.time(),
        )

        await self._registry.register(record)
        instance = await self._start_instance(record)

        # If webhook mode, register the webhook URL with Telegram
        if mode == "webhook" and webhook_url:
            webhook_endpoint = f"{webhook_url}/transports/telegram/bots/{bot_id}/update"
            try:
                await instance._api.set_webhook(webhook_endpoint)
                logger.info(
                    "TelegramBridge: webhook registered for bot=%s url=%s",
                    bot_id, webhook_endpoint,
                )
            except TelegramAPIError as exc:
                logger.warning("TelegramBridge: webhook registration failed: %s", exc)

        logger.info(
            "TelegramBridge: registered bot=%s %s owner=%s cluster=%s mode=%s",
            bot_id, bot_username, owner_user_id, cluster_id, mode,
        )

        return {
            "bot_id": bot_id,
            "bot_username": bot_username,
            "mode": mode,
            "status": "active",
            "message": f"Bot {bot_username} is now active. Send /start to begin.",
        }

    async def deregister_bot(self, bot_id: str) -> None:
        """Stop and deregister a bot. Raises KeyError if not found."""
        record = self._registry.get(bot_id)
        if record is None:
            raise KeyError(f"Bot {bot_id} not found")

        # Stop running instance
        if bot_id in self._instances:
            instance = self._instances.pop(bot_id)
            await instance.stop()

        # Remove webhook if applicable
        if record.mode == "webhook":
            try:
                tmp_api = TelegramBotAPI(token=record.bot_token, timeout_seconds=self._api_timeout)
                await tmp_api.start()
                await tmp_api.delete_webhook()
                await tmp_api.close()
            except Exception as exc:
                logger.warning("TelegramBridge: failed to delete webhook for %s: %s", bot_id, exc)

        await self._registry.deactivate(bot_id)
        logger.info("TelegramBridge: deregistered bot=%s", bot_id)

    def list_bots(self) -> list[dict]:
        """Return list of all bot records as dicts (for GET /bots)."""
        result = []
        for record in self._registry.list_all():
            info = record.to_dict()
            info.pop("bot_token", None)  # never expose token in API
            info["running"] = record.bot_id in self._instances
            result.append(info)
        return result

    def get_bot_info(self, bot_id: str) -> dict | None:
        """Return bot info dict, or None if not found."""
        record = self._registry.get(bot_id)
        if record is None:
            return None
        info = record.to_dict()
        info.pop("bot_token", None)
        info["running"] = bot_id in self._instances
        instance = self._instances.get(bot_id)
        if instance:
            info["active_chats"] = len(instance._chat_sessions)
        return info

    def get_instance(self, bot_id: str) -> TelegramBotInstance | None:
        """Return active bot instance by bot_id (used by webhook router)."""
        return self._instances.get(bot_id)

    # ── internal ───────────────────────────────────────────────────────────

    async def _start_instance(self, record: TelegramBotRecord) -> TelegramBotInstance:
        """Create and start a TelegramBotInstance for the given record."""
        instance = TelegramBotInstance(
            record=record,
            gnot_base_url=self._gnot_base_url,
            gnot_auth_token=self._gnot_auth_token,
            poll_interval=self._poll_interval,
            poll_timeout=self._poll_timeout,
            api_timeout=self._api_timeout,
            message_format=self._message_format,
            language=self._language,
        )
        try:
            await instance.start()
            self._instances[record.bot_id] = instance
            logger.info("TelegramBridge: started instance bot=%s", record.bot_id)
        except Exception as exc:
            logger.error(
                "TelegramBridge: failed to start instance bot=%s: %s", record.bot_id, exc
            )
            raise
        return instance

    def _find_instance_for_cluster(self, cluster_id: str) -> TelegramBotInstance | None:
        """Find an active bot instance that bridges to the given cluster."""
        for bot_id, instance in self._instances.items():
            if instance.record.cluster_id == cluster_id:
                return instance
        return None
