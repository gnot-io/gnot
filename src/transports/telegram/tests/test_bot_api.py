"""Tests for TelegramBotAPI — pure Telegram HTTP client.

Covers:
  1. get_me() — happy path and error response
  2. send_message() — happy path, truncation, reply_to
  3. send_chat_action() — basic call
  4. get_updates() — returns list of updates
  5. set_webhook() / delete_webhook()
  6. TelegramAPIError on non-ok response
  7. _call() handles HTTP exceptions
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from transports.telegram.bot_api import TelegramBotAPI, TelegramAPIError, MAX_MESSAGE_LENGTH


class MockResponse:
    """Mock aiohttp response."""
    def __init__(self, body: dict, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def json(self) -> dict:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def make_api(token: str = "test-token") -> TelegramBotAPI:
    api = TelegramBotAPI(token=token, timeout_seconds=5)
    return api


def mock_session(response_body: dict, status: int = 200):
    """Create a mock aiohttp session that returns the given response."""
    mock_resp = MockResponse(response_body, status)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    return mock_session


class TestTelegramBotAPI:

    @pytest.mark.asyncio
    async def test_get_me_success(self):
        """get_me returns bot info dict on success."""
        api = make_api()
        api._session = mock_session({"ok": True, "result": {"id": 123, "username": "test_bot"}})

        result = await api.get_me()

        assert result["id"] == 123
        assert result["username"] == "test_bot"

    @pytest.mark.asyncio
    async def test_get_me_api_error(self):
        """get_me raises TelegramAPIError when ok=False."""
        api = make_api()
        api._session = mock_session({"ok": False, "error_code": 401, "description": "Unauthorized"})

        with pytest.raises(TelegramAPIError) as exc_info:
            await api.get_me()

        assert exc_info.value.error_code == 401
        assert "Unauthorized" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_send_message_success(self):
        """send_message returns sent Message object."""
        api = make_api()
        api._session = mock_session({"ok": True, "result": {"message_id": 42, "text": "hello"}})

        result = await api.send_message(chat_id=123, text="hello")

        assert result["message_id"] == 42

    @pytest.mark.asyncio
    async def test_send_message_truncation(self):
        """send_message truncates messages longer than MAX_MESSAGE_LENGTH."""
        api = make_api()
        captured_calls = []

        class CapturingResponse:
            async def json(self):
                return {"ok": True, "result": {"message_id": 1}}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class CapturingSession:
            def post(self, url, json=None, timeout=None):
                captured_calls.append(json)
                return CapturingResponse()

        api._session = CapturingSession()
        long_text = "x" * (MAX_MESSAGE_LENGTH + 500)

        await api.send_message(chat_id=123, text=long_text)

        assert len(captured_calls) == 1
        sent_text = captured_calls[0]["text"]
        assert len(sent_text) <= MAX_MESSAGE_LENGTH
        assert "(truncated" in sent_text

    @pytest.mark.asyncio
    async def test_send_message_with_reply_to(self):
        """send_message passes reply_to_message_id when provided."""
        api = make_api()
        captured = []

        class CapResp:
            async def json(self):
                return {"ok": True, "result": {"message_id": 2}}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class CapSession:
            def post(self, url, json=None, timeout=None):
                captured.append(json)
                return CapResp()

        api._session = CapSession()
        await api.send_message(chat_id=123, text="reply", reply_to_message_id=99)

        assert captured[0]["reply_to_message_id"] == 99

    @pytest.mark.asyncio
    async def test_send_chat_action(self):
        """send_chat_action returns True on success."""
        api = make_api()
        api._session = mock_session({"ok": True, "result": True})

        result = await api.send_chat_action(chat_id=123, action="typing")
        assert result is True

    @pytest.mark.asyncio
    async def test_get_updates_returns_list(self):
        """get_updates returns list of update dicts."""
        api = make_api()
        updates = [{"update_id": 1, "message": {"text": "hi"}}]
        api._session = mock_session({"ok": True, "result": updates})

        result = await api.get_updates()
        assert result == updates

    @pytest.mark.asyncio
    async def test_get_updates_empty(self):
        """get_updates returns empty list when no updates."""
        api = make_api()
        api._session = mock_session({"ok": True, "result": []})

        result = await api.get_updates()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_updates_none_result(self):
        """get_updates handles null result gracefully."""
        api = make_api()
        api._session = mock_session({"ok": True, "result": None})

        result = await api.get_updates()
        assert result == []

    @pytest.mark.asyncio
    async def test_set_webhook_success(self):
        """set_webhook returns True on success."""
        api = make_api()
        api._session = mock_session({"ok": True, "result": True})

        result = await api.set_webhook("https://example.com/webhook")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_webhook_success(self):
        """delete_webhook returns True on success."""
        api = make_api()
        api._session = mock_session({"ok": True, "result": True})

        result = await api.delete_webhook()
        assert result is True

    @pytest.mark.asyncio
    async def test_call_http_exception_raises_api_error(self):
        """_call wraps HTTP exceptions in TelegramAPIError."""
        api = make_api()

        class FailingSession:
            def post(self, *args, **kwargs):
                raise RuntimeError("connection refused")

        api._session = FailingSession()

        with pytest.raises(TelegramAPIError, match="HTTP request failed"):
            await api.get_me()

    @pytest.mark.asyncio
    async def test_start_creates_session(self):
        """start() initializes aiohttp session (mocked)."""
        api = make_api()
        assert api._session is None

        # We can't create a real aiohttp session in tests, so just verify
        # that calling start() without aiohttp raises ImportError or creates session
        try:
            import aiohttp
            await api.start()
            assert api._session is not None
            await api.close()
        except ImportError:
            pass  # aiohttp not installed in test env

    @pytest.mark.asyncio
    async def test_close_clears_session(self):
        """close() sets _session to None."""
        api = make_api()
        mock_sess = MagicMock()
        mock_sess.close = AsyncMock()
        api._session = mock_sess

        await api.close()

        mock_sess.close.assert_called_once()
        assert api._session is None
