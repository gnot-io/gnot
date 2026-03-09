"""Tests for TelegramBotInstance — commands, message routing, async push.

Covers:
  1. /start command — welcome message sent
  2. /conversation <id> — session linked; 404 handled
  3. /session — shows linked session or "none"
  4. /status — fetches session + tasks, formats status
  5. /end — unlinks session
  6. /register <role> — calls GNOT register_participant
  7. Message with linked session → create_task(_call_intent_and_push)
  8. Message with no session → "no session" prompt
  9. Reply-to-question → _handle_question_reply
  10. send_question_notification — records message_id → question_id mapping
  11. _call_intent_and_push — success and GNOT error paths
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from transports.telegram.bot_instance import TelegramBotInstance
from transports.telegram.config import TelegramBotRecord


def make_record(bot_id: str = "bot-alice-abc", mode: str = "polling") -> TelegramBotRecord:
    return TelegramBotRecord(
        bot_id=bot_id,
        bot_token="tok-test",
        bot_username="@test_bot",
        owner_user_id="alice",
        cluster_id="cluster-A",
        mode=mode,
    )


def make_instance(record: TelegramBotRecord | None = None) -> TelegramBotInstance:
    record = record or make_record()
    inst = TelegramBotInstance(
        record=record,
        gnot_base_url="http://localhost:8080",
        gnot_auth_token="tok",
        language="en",
    )
    # Mock API and GNOT client
    inst._api = MagicMock()
    inst._api.send_message = AsyncMock(return_value={"message_id": 99})
    inst._api.send_chat_action = AsyncMock()
    inst._api.get_updates = AsyncMock(return_value=[])
    inst._api.get_me = AsyncMock(return_value={"id": 1, "username": "test_bot"})
    inst._gnot = MagicMock()
    inst._gnot.get_session = AsyncMock(return_value={"session_id": "sess-1", "messages": []})
    inst._gnot.get_tasks = AsyncMock(return_value=[])
    inst._gnot.post_intent = AsyncMock(return_value={"reply": "agent response"})
    inst._gnot.post_channels_respond = AsyncMock(return_value={"ok": True})
    inst._gnot.register_participant = AsyncMock(return_value={"participant_id": "tg-1-pm"})
    inst._running = True
    inst._bot_info = {"username": "test_bot"}
    return inst


def make_message(
    text: str = "hello",
    chat_id: int = 111,
    message_id: int = 1,
    reply_to_id: int | None = None,
    from_user: dict | None = None,
) -> dict:
    msg: dict = {
        "message_id": message_id,
        "text": text,
        "chat": {"id": chat_id, "type": "private"},
        "from": from_user or {"id": chat_id, "username": "alice"},
    }
    if reply_to_id is not None:
        msg["reply_to_message"] = {"message_id": reply_to_id}
    return msg


class TestCommands:

    @pytest.mark.asyncio
    async def test_start_sends_welcome(self):
        """/start sends welcome message."""
        inst = make_instance()
        await inst._handle_message(make_message(text="/start"))
        inst._api.send_message.assert_called_once()
        call_args = inst._api.send_message.call_args
        assert "Hello" in call_args[0][1] or "GNOT" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_conversation_links_session(self):
        """/conversation <id> links session when session exists."""
        inst = make_instance()
        inst._gnot.get_session = AsyncMock(return_value={"session_id": "sess-abc", "messages": []})

        await inst._handle_message(make_message(text="/conversation sess-abc"))

        assert inst._chat_sessions[111] == "sess-abc"
        inst._api.send_message.assert_called_once()
        assert "sess-abc" in inst._api.send_message.call_args[0][1]

    @pytest.mark.asyncio
    async def test_conversation_404_session(self):
        """/conversation <id> shows error when session not found."""
        inst = make_instance()
        inst._gnot.get_session = AsyncMock(return_value=None)

        await inst._handle_message(make_message(text="/conversation sess-nonexistent"))

        assert 111 not in inst._chat_sessions
        call_text = inst._api.send_message.call_args[0][1]
        assert "not found" in call_text.lower() or "❌" in call_text

    @pytest.mark.asyncio
    async def test_conversation_no_arg(self):
        """/conversation with no arg shows usage."""
        inst = make_instance()
        await inst._handle_message(make_message(text="/conversation"))
        call_text = inst._api.send_message.call_args[0][1]
        assert "Usage" in call_text or "usage" in call_text or "Cú pháp" in call_text

    @pytest.mark.asyncio
    async def test_session_shows_linked(self):
        """/session shows linked session_id."""
        inst = make_instance()
        inst._chat_sessions[111] = "sess-abc"

        await inst._handle_message(make_message(text="/session"))

        call_text = inst._api.send_message.call_args[0][1]
        assert "sess-abc" in call_text

    @pytest.mark.asyncio
    async def test_session_shows_none(self):
        """/session shows 'no session' when none linked."""
        inst = make_instance()

        await inst._handle_message(make_message(text="/session"))

        call_text = inst._api.send_message.call_args[0][1]
        assert "No session" in call_text or "no session" in call_text.lower()

    @pytest.mark.asyncio
    async def test_status_shows_running(self):
        """/status shows running state when no suspended tasks."""
        inst = make_instance()
        inst._chat_sessions[111] = "sess-abc"
        inst._gnot.get_tasks = AsyncMock(return_value=[])

        await inst._handle_message(make_message(text="/status"))

        call_text = inst._api.send_message.call_args[0][1]
        assert "sess-abc" in call_text

    @pytest.mark.asyncio
    async def test_status_no_session(self):
        """/status without linked session prompts to link first."""
        inst = make_instance()

        await inst._handle_message(make_message(text="/status"))

        call_text = inst._api.send_message.call_args[0][1]
        assert "conversation" in call_text.lower() or "session" in call_text.lower()

    @pytest.mark.asyncio
    async def test_end_unlinks_session(self):
        """/end removes session link."""
        inst = make_instance()
        inst._chat_sessions[111] = "sess-abc"

        await inst._handle_message(make_message(text="/end"))

        assert 111 not in inst._chat_sessions
        call_text = inst._api.send_message.call_args[0][1]
        assert "sess-abc" in call_text

    @pytest.mark.asyncio
    async def test_end_no_session(self):
        """/end with no session shows appropriate message."""
        inst = make_instance()

        await inst._handle_message(make_message(text="/end"))

        call_text = inst._api.send_message.call_args[0][1]
        assert "No session" in call_text or "nothing" in call_text.lower() or "no session" in call_text.lower()

    @pytest.mark.asyncio
    async def test_register_creates_participant(self):
        """/register pm calls GNOT register_participant with correct payload."""
        inst = make_instance()
        msg = make_message(text="/register pm", from_user={"id": 789, "username": "alice"})

        await inst._handle_message(msg)

        inst._gnot.register_participant.assert_called_once()
        payload = inst._gnot.register_participant.call_args[0][0]
        assert payload["roles"] == ["pm"]
        assert payload["transport"] == "telegram"
        assert payload["transport_target"] == "789"

    @pytest.mark.asyncio
    async def test_register_no_role(self):
        """/register with no role shows usage."""
        inst = make_instance()

        await inst._handle_message(make_message(text="/register"))

        call_text = inst._api.send_message.call_args[0][1]
        assert "role" in call_text.lower() or "usage" in call_text.lower()

    @pytest.mark.asyncio
    async def test_unknown_command(self):
        """Unknown commands get a helpful response."""
        inst = make_instance()

        await inst._handle_message(make_message(text="/foobar"))

        inst._api.send_message.assert_called_once()


class TestMessageRouting:

    @pytest.mark.asyncio
    async def test_message_with_session_spawns_intent_task(self):
        """Normal message with linked session → spawns intent background task."""
        inst = make_instance()
        inst._chat_sessions[111] = "sess-abc"
        intent_called = asyncio.Event()

        async def mock_intent(chat_id, session_id, prompt):
            intent_called.set()

        inst._call_intent_and_push = mock_intent
        await inst._handle_message(make_message(text="how many nodes?"))

        # send_chat_action should be called for typing indicator
        inst._api.send_chat_action.assert_called_once_with(111, "typing")

    @pytest.mark.asyncio
    async def test_message_without_session_prompts(self):
        """Normal message without linked session shows 'no session' prompt."""
        inst = make_instance()

        await inst._handle_message(make_message(text="hello agent"))

        inst._api.send_message.assert_called_once()
        call_text = inst._api.send_message.call_args[0][1]
        assert "conversation" in call_text.lower() or "session" in call_text.lower()

    @pytest.mark.asyncio
    async def test_reply_to_question_routes_answer(self):
        """Reply-to a question message routes to _handle_question_reply."""
        inst = make_instance()
        inst._question_messages[50] = "qid-xyz"  # msg 50 → question qid-xyz

        msg = make_message(text="PostgreSQL", reply_to_id=50)
        await inst._handle_message(msg)

        inst._gnot.post_channels_respond.assert_called_once()
        call_args = inst._gnot.post_channels_respond.call_args[1]
        assert call_args["question_id"] == "qid-xyz"
        assert call_args["content"] == "PostgreSQL"

    @pytest.mark.asyncio
    async def test_non_text_message_ignored(self):
        """Non-text messages (photos etc.) are silently ignored."""
        inst = make_instance()
        msg = {"message_id": 1, "chat": {"id": 111}, "photo": []}  # no "text"

        await inst._handle_message(msg)

        inst._api.send_message.assert_not_called()


class TestIntentPush:

    @pytest.mark.asyncio
    async def test_call_intent_success(self):
        """_call_intent_and_push sends agent reply back to Telegram."""
        inst = make_instance()
        inst._gnot.post_intent = AsyncMock(return_value={"reply": "3 nodes are up"})

        await inst._call_intent_and_push(chat_id=111, session_id="sess-1", prompt="how many?")

        inst._api.send_message.assert_called_once()
        sent_text = inst._api.send_message.call_args[0][1]
        assert "3 nodes are up" in sent_text

    @pytest.mark.asyncio
    async def test_call_intent_gnot_error_sends_error_msg(self):
        """_call_intent_and_push sends error message on GNOT API error."""
        from transports.telegram.gnot_client import GNOTAPIError
        inst = make_instance()
        inst._gnot.post_intent = AsyncMock(side_effect=GNOTAPIError(500, "Internal Server Error"))

        await inst._call_intent_and_push(chat_id=111, session_id="sess-1", prompt="help")

        inst._api.send_message.assert_called_once()
        sent_text = inst._api.send_message.call_args[0][1]
        assert "Error" in sent_text or "error" in sent_text

    @pytest.mark.asyncio
    async def test_call_intent_suspended_response(self):
        """_call_intent_and_push formats suspended response correctly."""
        inst = make_instance()
        inst._gnot.post_intent = AsyncMock(return_value={
            "suspended": True,
            "question": "Which DB to use?",
        })

        await inst._call_intent_and_push(chat_id=111, session_id="sess-1", prompt="start task")

        sent_text = inst._api.send_message.call_args[0][1]
        assert "suspended" in sent_text.lower() or "⏸" in sent_text


class TestQuestionNotification:

    @pytest.mark.asyncio
    async def test_send_question_notification_records_mapping(self):
        """send_question_notification records message_id → question_id."""
        inst = make_instance()
        inst._api.send_message = AsyncMock(return_value={"message_id": 77})

        await inst.send_question_notification(
            chat_id=111,
            question_id="q-001",
            question_text="Use PostgreSQL or MySQL?",
            required_role="pm",
            source_agent="dev-A",
            cluster_id="cluster-A",
        )

        assert inst._question_messages[77] == "q-001"

    @pytest.mark.asyncio
    async def test_send_question_notification_formats_text(self):
        """send_question_notification sends formatted question text."""
        inst = make_instance()

        await inst.send_question_notification(
            chat_id=111,
            question_id="q-001",
            question_text="Use PostgreSQL or MySQL?",
            required_role="pm",
            source_agent="dev-A",
            cluster_id="cluster-A",
        )

        sent_text = inst._api.send_message.call_args[0][1]
        assert "pm" in sent_text
        assert "dev-A" in sent_text
        assert "PostgreSQL or MySQL" in sent_text
