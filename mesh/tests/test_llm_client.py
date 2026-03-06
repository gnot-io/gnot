"""Tests for the LLM client utility."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from runtime.llm_client import LLMClient, LLMError, LLMResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """Create an LLM client with test config."""
    return LLMClient(
        api_key="test-key-123",
        base_url="https://api.test.com/v1",
        default_model="test-model",
        timeout_seconds=30.0,
    )


@pytest.fixture
def mock_chat_response():
    """Standard chat completion response."""
    return {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello! How can I help?"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 8,
            "total_tokens": 18,
        },
    }


@pytest.fixture
def mock_image_response():
    """Standard image generation response."""
    return {
        "data": [
            {
                "url": "https://example.com/image.png",
                "revised_prompt": "A beautiful sunset over the ocean",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Tests — chat
# ---------------------------------------------------------------------------

class TestChat:
    @pytest.mark.asyncio
    async def test_chat_basic(self, client, mock_chat_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_chat_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_resp
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            result = await client.chat(
                messages=[{"role": "user", "content": "Hello"}]
            )

            assert isinstance(result, LLMResponse)
            assert result.content == "Hello! How can I help?"
            assert result.model == "test-model"
            assert result.usage["total_tokens"] == 18
            assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_chat_with_system(self, client, mock_chat_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_chat_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_resp
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            result = await client.chat(
                messages=[{"role": "user", "content": "Hi"}],
                system="You are helpful.",
            )

            # Verify system message was prepended
            call_args = mock_instance.post.call_args
            body = call_args.kwargs.get("json") or call_args[1].get("json")
            assert body["messages"][0]["role"] == "system"
            assert body["messages"][0]["content"] == "You are helpful."

    @pytest.mark.asyncio
    async def test_chat_json_mode(self, client, mock_chat_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_chat_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_resp
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            await client.chat(
                messages=[{"role": "user", "content": "return json"}],
                json_mode=True,
            )

            call_args = mock_instance.post.call_args
            body = call_args.kwargs.get("json") or call_args[1].get("json")
            assert body["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_chat_model_override(self, client, mock_chat_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_chat_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_resp
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            await client.chat(
                messages=[{"role": "user", "content": "Hi"}],
                model="gpt-4o",
            )

            call_args = mock_instance.post.call_args
            body = call_args.kwargs.get("json") or call_args[1].get("json")
            assert body["model"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_chat_http_error(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "Rate limited"
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "429", request=MagicMock(), response=mock_resp
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_resp
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            with pytest.raises(LLMError, match="API error 429"):
                await client.chat(
                    messages=[{"role": "user", "content": "Hi"}]
                )


# ---------------------------------------------------------------------------
# Tests — ask (convenience)
# ---------------------------------------------------------------------------

class TestAsk:
    @pytest.mark.asyncio
    async def test_ask_returns_text(self, client, mock_chat_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_chat_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_resp
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            result = await client.ask("What is 2+2?")
            assert result == "Hello! How can I help?"


# ---------------------------------------------------------------------------
# Tests — generate_image
# ---------------------------------------------------------------------------

class TestGenerateImage:
    @pytest.mark.asyncio
    async def test_generate_image_url(self, client, mock_image_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_image_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_resp
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            result = await client.generate_image("A sunset")

            assert result["url"] == "https://example.com/image.png"
            assert result["revised_prompt"] == "A beautiful sunset over the ocean"
            assert result["model"] == "dall-e-3"

    @pytest.mark.asyncio
    async def test_generate_image_custom_params(self, client, mock_image_response):
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_image_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_resp
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_instance

            await client.generate_image(
                "A cat",
                model="dall-e-2",
                size="512x512",
                quality="hd",
            )

            call_args = mock_instance.post.call_args
            body = call_args.kwargs.get("json") or call_args[1].get("json")
            assert body["model"] == "dall-e-2"
            assert body["size"] == "512x512"
            assert body["quality"] == "hd"


# ---------------------------------------------------------------------------
# Tests — headers
# ---------------------------------------------------------------------------

class TestHeaders:
    def test_build_headers_with_key(self, client):
        headers = client._build_headers()
        assert headers["Authorization"] == "Bearer test-key-123"
        assert headers["Content-Type"] == "application/json"

    def test_build_headers_no_key(self):
        c = LLMClient(api_key="", base_url="http://localhost/v1")
        headers = c._build_headers()
        assert "Authorization" not in headers

    def test_extra_headers(self):
        c = LLMClient(
            api_key="key",
            base_url="http://localhost/v1",
            extra_headers={"X-Custom": "value"},
        )
        headers = c._build_headers()
        assert headers["X-Custom"] == "value"
