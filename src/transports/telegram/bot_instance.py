"""TelegramBotInstance — lifecycle and message handling for one Telegram bot.

One instance per registered bot token. Manages:
  - Polling loop (long-polling) or webhook mode
  - Command handling: /start, /conversation, /session, /status, /end, /register
  - Pattern 2 (conversational): forward messages to /intent, push response back
  - Pattern 1 (transactional): reply-to-message → answer routing

Design:
  - Stateless between messages except for _chat_sessions (telegram_chat_id → session_id)
  - Async: _handle_message() spawns tasks for long-running operations
  - Fire-and-forget for /intent calls (ACK Telegram immediately)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from transports.telegram.bot_api import TelegramBotAPI, TelegramAPIError
from transports.telegram.config import TelegramBotRecord
from transports.telegram.formatters import TelegramFormatter
from transports.telegram.gnot_client import GNOTClient, GNOTAPIError

logger = logging.getLogger(__name__)


class TelegramBotInstance:
    """One active Telegram bot.

    Manages its own polling loop (or receives webhook updates via bridge).
    Maintains a mapping of telegram_chat_id → gnot_session_id for linked conversations.
    """

    def __init__(
        self,
        record: TelegramBotRecord,
        gnot_base_url: str,
        gnot_auth_token: str | None = None,
        poll_interval: int = 1,
        poll_timeout: int = 30,
        api_timeout: int = 10,
        message_format: str = "full",
        language: str = "vi",
    ) -> None:
        self._record = record
        self._api = TelegramBotAPI(token=record.bot_token, timeout_seconds=api_timeout)
        self._gnot = GNOTClient(base_url=gnot_base_url, auth_token=gnot_auth_token)
        self._formatter = TelegramFormatter(style=message_format, language=language)
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

        # Runtime state
        self._chat_sessions: dict[int, str] = {}         # chat_id → session_id
        self._question_messages: dict[int, str] = {}     # telegram message_id → gnot question_id
        self._polling_task: asyncio.Task | None = None
        self._running = False
        self._last_update_id: int | None = None
        self._bot_info: dict | None = None

    @property
    def bot_id(self) -> str:
        return self._record.bot_id

    @property
    def record(self) -> TelegramBotRecord:
        return self._record

    @property
    def is_polling(self) -> bool:
        return self._record.mode == "polling"

    @property
    def is_ready(self) -> bool:
        return self._running

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the bot instance. Validates token and starts polling if needed."""
        await self._api.start()
        await self._gnot.start()

        try:
            self._bot_info = await self._api.get_me()
            username = self._bot_info.get("username", "unknown")
            logger.info(
                "TelegramBotInstance: started bot=%s @%s mode=%s",
                self.bot_id, username, self._record.mode,
            )
        except TelegramAPIError as exc:
            logger.error(
                "TelegramBotInstance: token validation failed for bot=%s: %s",
                self.bot_id, exc,
            )
            await self._api.close()
            await self._gnot.close()
            raise

        self._running = True

        if self._record.mode == "polling":
            self._polling_task = asyncio.create_task(
                self._polling_loop(),
                name=f"telegram-poll-{self.bot_id}",
            )

    async def stop(self) -> None:
        """Stop the bot instance gracefully."""
        self._running = False
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None
        await self._api.close()
        await self._gnot.close()
        logger.info("TelegramBotInstance: stopped bot=%s", self.bot_id)

    # ── polling loop ───────────────────────────────────────────────────────

    async def _polling_loop(self) -> None:
        """Long-polling loop. Runs until self._running = False."""
        logger.info("TelegramBotInstance: polling loop started for bot=%s", self.bot_id)
        while self._running:
            try:
                updates = await self._api.get_updates(
                    offset=self._last_update_id,
                    timeout=self._poll_timeout,
                )
                for update in updates:
                    update_id = update.get("update_id", 0)
                    self._last_update_id = update_id + 1
                    asyncio.create_task(
                        self._process_update(update),
                        name=f"telegram-update-{update_id}",
                    )
                if not updates:
                    await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break
            except TelegramAPIError as exc:
                logger.error(
                    "TelegramBotInstance: polling error bot=%s: %s — retrying in 5s",
                    self.bot_id, exc,
                )
                await asyncio.sleep(5)
            except Exception as exc:
                logger.error(
                    "TelegramBotInstance: unexpected polling error bot=%s: %s — retrying in 5s",
                    self.bot_id, exc,
                )
                await asyncio.sleep(5)

        logger.info("TelegramBotInstance: polling loop ended for bot=%s", self.bot_id)

    # ── update routing (used by both polling and webhook) ──────────────────

    async def process_webhook_update(self, update: dict) -> None:
        """Handle an update received via webhook (called by webhook router)."""
        asyncio.create_task(
            self._process_update(update),
            name=f"telegram-wh-update-{update.get('update_id', 0)}",
        )

    async def _process_update(self, update: dict) -> None:
        """Route an incoming Telegram update to the appropriate handler."""
        try:
            if "message" in update:
                await self._handle_message(update["message"])
        except Exception as exc:
            logger.error(
                "TelegramBotInstance: error processing update bot=%s: %s",
                self.bot_id, exc,
            )

    # ── message handling ───────────────────────────────────────────────────

    async def _handle_message(self, message: dict) -> None:
        """Route an incoming message to the appropriate handler.

        Commands → command handlers
        Reply-to-question → answer routing
        Normal message → /intent forward (if session linked)
        """
        text: str = message.get("text", "")
        chat_id: int = message["chat"]["id"]
        message_id: int = message.get("message_id", 0)

        if not text:
            return  # ignore non-text messages (photos, stickers, etc.)

        # ── command routing ──
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].split("@")[0].lower()  # strip @bot_name if present
            arg = parts[1] if len(parts) > 1 else ""
            await self._handle_command(chat_id, message_id, cmd, arg, message)
            return

        # ── reply-to-message routing (Pattern 1 answer) ──
        reply_to = message.get("reply_to_message")
        if reply_to:
            replied_msg_id = reply_to.get("message_id")
            question_id = self._question_messages.get(replied_msg_id)
            if question_id is not None:
                await self._handle_question_reply(chat_id, message_id, question_id, text)
                return

        # ── check for suspended session (smart routing) ──
        session_id = self._chat_sessions.get(chat_id)
        if session_id:
            # Check if session has a pending question that can be answered directly
            try:
                tasks = await self._gnot.get_tasks()
                suspended = [
                    t for t in tasks
                    if t.get("status") == "suspended"
                    and t.get("session_id") == session_id
                ]
                if suspended:
                    # Treat as answer to the suspended task
                    task = suspended[0]
                    question_id = task.get("pending_question", {}).get("question_id")
                    cluster_id = self._record.cluster_id
                    if question_id:
                        await self._answer_pending_question(chat_id, cluster_id, question_id, text)
                        return
            except Exception as exc:
                logger.debug("TelegramBotInstance: could not check tasks: %s", exc)

            # Normal conversational message → forward to /intent
            await self._api.send_chat_action(chat_id, "typing")
            asyncio.create_task(
                self._call_intent_and_push(chat_id, session_id, text),
                name=f"intent-{chat_id}",
            )
        else:
            # No session linked
            if self._formatter.language == "vi":
                await self._api.send_message(
                    chat_id,
                    "Chưa có cuộc hội thoại. Dùng `/conversation <session_id>` để liên kết.",
                )
            else:
                await self._api.send_message(
                    chat_id,
                    "No conversation linked. Use `/conversation <session_id>` to link one.",
                )

    # ── command handlers ───────────────────────────────────────────────────

    async def _handle_command(
        self, chat_id: int, message_id: int, cmd: str, arg: str, message: dict
    ) -> None:
        """Dispatch command to the appropriate handler."""
        handlers = {
            "/start": self._cmd_start,
            "/conversation": self._cmd_conversation,
            "/session": self._cmd_session,
            "/status": self._cmd_status,
            "/end": self._cmd_end,
            "/register": self._cmd_register,
        }
        handler = handlers.get(cmd)
        if handler:
            await handler(chat_id, message_id, arg, message)
        else:
            if self._formatter.language == "vi":
                await self._api.send_message(chat_id, f"Lệnh không hỗ trợ: `{cmd}`")
            else:
                await self._api.send_message(chat_id, f"Unknown command: `{cmd}`")

    async def _cmd_start(self, chat_id: int, message_id: int, arg: str, message: dict) -> None:
        """Handle /start — welcome message."""
        username = (self._bot_info or {}).get("username", self.bot_id)
        text = self._formatter.format_start(f"@{username}")
        await self._api.send_message(chat_id, text)

    async def _cmd_conversation(
        self, chat_id: int, message_id: int, arg: str, message: dict
    ) -> None:
        """/conversation <session_id> — link this Telegram chat to a GNOT session."""
        session_id = arg.strip()
        if not session_id:
            if self._formatter.language == "vi":
                await self._api.send_message(chat_id, "Cú pháp: `/conversation <session_id>`")
            else:
                await self._api.send_message(chat_id, "Usage: `/conversation <session_id>`")
            return

        # Validate session exists
        try:
            session = await self._gnot.get_session(session_id)
        except Exception as exc:
            logger.error("TelegramBotInstance: error fetching session %s: %s", session_id, exc)
            await self._api.send_message(chat_id, f"⚠️ Error checking session: {exc}")
            return

        if session is None:
            if self._formatter.language == "vi":
                await self._api.send_message(
                    chat_id,
                    f"❌ Session `{session_id}` không tồn tại.\n"
                    "Dùng `/new` để tạo mới hoặc kiểm tra lại session ID.",
                )
            else:
                await self._api.send_message(
                    chat_id,
                    f"❌ Session `{session_id}` not found.\n"
                    "Use `/new` to start a new conversation or check the session ID.",
                )
            return

        # Link the session
        self._chat_sessions[chat_id] = session_id

        # Get suspended tasks for context
        tasks = []
        try:
            tasks = await self._gnot.get_tasks()
        except Exception:
            pass

        text = self._formatter.format_conversation_linked(session_id, session, tasks)
        await self._api.send_message(chat_id, text)

    async def _cmd_session(
        self, chat_id: int, message_id: int, arg: str, message: dict
    ) -> None:
        """/session — show currently linked session."""
        session_id = self._chat_sessions.get(chat_id)
        if not session_id:
            if self._formatter.language == "vi":
                await self._api.send_message(chat_id, "Chưa liên kết session nào.")
            else:
                await self._api.send_message(chat_id, "No session linked.")
        else:
            await self._api.send_message(chat_id, f"📎 Session hiện tại: `{session_id}`")

    async def _cmd_status(
        self, chat_id: int, message_id: int, arg: str, message: dict
    ) -> None:
        """/status — show session status and pending tasks."""
        session_id = self._chat_sessions.get(chat_id)
        if not session_id:
            if self._formatter.language == "vi":
                await self._api.send_message(
                    chat_id, "Chưa liên kết session. Dùng `/conversation <id>` trước."
                )
            else:
                await self._api.send_message(
                    chat_id, "No session linked. Use `/conversation <id>` first."
                )
            return

        session = None
        tasks: list[dict] = []
        try:
            session = await self._gnot.get_session(session_id)
            tasks = await self._gnot.get_tasks()
        except Exception as exc:
            logger.error("TelegramBotInstance: error fetching status: %s", exc)

        text = self._formatter.format_session_status(session_id, session, tasks)
        await self._api.send_message(chat_id, text)

    async def _cmd_end(
        self, chat_id: int, message_id: int, arg: str, message: dict
    ) -> None:
        """/end — unlink session from this Telegram chat."""
        session_id = self._chat_sessions.pop(chat_id, None)
        if session_id:
            if self._formatter.language == "vi":
                await self._api.send_message(
                    chat_id, f"✅ Đã hủy liên kết session `{session_id}`."
                )
            else:
                await self._api.send_message(
                    chat_id, f"✅ Unlinked from session `{session_id}`."
                )
        else:
            if self._formatter.language == "vi":
                await self._api.send_message(chat_id, "Không có session nào để hủy.")
            else:
                await self._api.send_message(chat_id, "No session to unlink.")

    async def _cmd_register(
        self, chat_id: int, message_id: int, arg: str, message: dict
    ) -> None:
        """/register <role> — register this Telegram user as an ExternalParticipant.

        Pattern 1 setup: creates participant with transport=telegram.
        transport_target is the Telegram user_id (= chat_id for DM chats).
        """
        role = arg.strip()
        if not role:
            if self._formatter.language == "vi":
                await self._api.send_message(chat_id, "Cú pháp: `/register <role>` (ví dụ: `/register pm`)")
            else:
                await self._api.send_message(chat_id, "Usage: `/register <role>` (e.g. `/register pm`)")
            return

        # Get Telegram user info
        from_user = message.get("from", {})
        tg_user_id = from_user.get("id", chat_id)
        tg_username = from_user.get("username") or from_user.get("first_name", f"user{tg_user_id}")

        participant_id = f"tg-{tg_user_id}-{role}"

        payload = {
            "participant_id": participant_id,
            "name": tg_username,
            "roles": [role],
            "transport": "telegram",
            "transport_target": str(tg_user_id),
            "cluster_id": self._record.cluster_id,
        }

        try:
            result = await self._gnot.register_participant(payload)
            if self._formatter.language == "vi":
                await self._api.send_message(
                    chat_id,
                    f"✅ Đã đăng ký *{tg_username}* với vai trò `{role}` trong cluster `{self._record.cluster_id}`.\n"
                    f"ID participant: `{participant_id}`\n"
                    "Bạn sẽ nhận thông báo khi agent cần input từ vai trò này.",
                )
            else:
                await self._api.send_message(
                    chat_id,
                    f"✅ Registered *{tg_username}* as `{role}` in cluster `{self._record.cluster_id}`.\n"
                    f"Participant ID: `{participant_id}`\n"
                    "You'll receive notifications when the agent needs input from this role.",
                )
        except GNOTAPIError as exc:
            logger.error("TelegramBotInstance: register participant failed: %s", exc)
            await self._api.send_message(chat_id, f"⚠️ Registration failed: {exc}")
        except Exception as exc:
            logger.error("TelegramBotInstance: unexpected error during /register: %s", exc)
            await self._api.send_message(chat_id, f"⚠️ Error: {exc}")

    # ── Pattern 2 — conversational intent forwarding ───────────────────────

    async def _call_intent_and_push(
        self, chat_id: int, session_id: str, prompt: str
    ) -> None:
        """Forward prompt to /intent and push response back to Telegram.

        Fire-and-forget: Telegram ACK has already been sent.
        Runs as a background asyncio task.
        """
        try:
            response = await self._gnot.post_intent(prompt, session_id)
            text = self._formatter.format_intent_response(response)
            await self._api.send_message(chat_id, text, parse_mode="Markdown")
        except GNOTAPIError as exc:
            logger.error(
                "TelegramBotInstance: intent call failed for bot=%s chat=%d: %s",
                self.bot_id, chat_id, exc,
            )
            await self._api.send_message(chat_id, f"⚠️ Error from GNOT: {exc}")
        except TelegramAPIError as exc:
            logger.error(
                "TelegramBotInstance: failed to send intent response bot=%s chat=%d: %s",
                self.bot_id, chat_id, exc,
            )
        except Exception as exc:
            logger.error(
                "TelegramBotInstance: unexpected error in intent push bot=%s chat=%d: %s",
                self.bot_id, chat_id, exc,
            )
            try:
                await self._api.send_message(chat_id, f"⚠️ Unexpected error: {exc}")
            except Exception:
                pass

    # ── Pattern 1 — question answer routing ───────────────────────────────

    async def _handle_question_reply(
        self,
        chat_id: int,
        message_id: int,
        question_id: str,
        answer_text: str,
    ) -> None:
        """Route a reply-to-question message as a GNOT channel answer.

        Called when user replies to a Telegram message that was a question notification.
        The replied-to message_id is mapped to a gnot question_id in _question_messages.
        """
        cluster_id = self._record.cluster_id
        # Use bot_id as a pseudo participant_id for bot-forwarded answers
        # In a real setup the participant is the registered ExternalParticipant
        participant_id = f"telegram-bot-{self.bot_id}"

        try:
            await self._gnot.post_channels_respond(
                cluster_id=cluster_id,
                question_id=question_id,
                participant_id=participant_id,
                auth_token="",  # public channel answer, no auth required
                content=answer_text,
            )
            if self._formatter.language == "vi":
                await self._api.send_message(chat_id, "✅ Đã gửi câu trả lời.")
            else:
                await self._api.send_message(chat_id, "✅ Answer submitted.")
        except GNOTAPIError as exc:
            logger.error(
                "TelegramBotInstance: channel respond failed question=%s: %s",
                question_id, exc,
            )
            await self._api.send_message(chat_id, f"⚠️ Failed to submit answer: {exc}")

    async def _answer_pending_question(
        self,
        chat_id: int,
        cluster_id: str,
        question_id: str,
        answer_text: str,
    ) -> None:
        """Answer a pending (suspended) question directly."""
        participant_id = f"tg-{chat_id}"
        try:
            await self._gnot.post_channels_respond(
                cluster_id=cluster_id,
                question_id=question_id,
                participant_id=participant_id,
                auth_token="",
                content=answer_text,
            )
            if self._formatter.language == "vi":
                await self._api.send_message(chat_id, "✅ Câu trả lời đã được gửi. Task tiếp tục chạy...")
            else:
                await self._api.send_message(chat_id, "✅ Answer submitted. Task resuming...")
        except GNOTAPIError as exc:
            logger.error("TelegramBotInstance: answer pending question failed: %s", exc)
            await self._api.send_message(chat_id, f"⚠️ Failed: {exc}")

    # ── Pattern 1 — outbound notification (called by TelegramBridge.notify) ──

    async def send_question_notification(
        self,
        chat_id: int,
        question_id: str,
        question_text: str,
        required_role: str,
        source_agent: str,
        cluster_id: str,
    ) -> None:
        """Send a question notification to a specific chat (Pattern 1).

        Records the sent message_id → question_id mapping so that
        replies can be routed as answers.
        """
        text = self._formatter.format_question(
            question_id=question_id,
            question_text=question_text,
            required_role=required_role,
            source_agent=source_agent,
            cluster_id=cluster_id,
        )
        try:
            sent_msg = await self._api.send_message(chat_id, text, parse_mode="Markdown")
            if sent_msg and "message_id" in sent_msg:
                self._question_messages[sent_msg["message_id"]] = question_id
                logger.info(
                    "TelegramBotInstance: sent question notification bot=%s chat=%d qid=%s",
                    self.bot_id, chat_id, question_id,
                )
        except TelegramAPIError as exc:
            logger.error(
                "TelegramBotInstance: failed to send question notification: %s", exc
            )
            raise
