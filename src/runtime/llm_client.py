"""Shared LLM client for action modules.

Provides an OpenAI-compatible async client that actions can use to
interact with any LLM provider (OpenAI, Anthropic, Ollama, vLLM, etc.).

The client is injected into every action's context dict as ``llm``,
so any action can call ``context["llm"].chat(...)`` without managing
its own HTTP client or API keys.

Supported providers (via base_url):
    - OpenAI:     https://api.openai.com/v1
    - Anthropic:  https://api.anthropic.com/v1 (via openai-compatible proxy)
    - Ollama:     http://localhost:11434/v1
    - vLLM:       http://localhost:8000/v1
    - LiteLLM:    http://localhost:4000/v1
    - Any OpenAI-compatible endpoint
"""

from __future__ import annotations

import asyncio
import json
import base64
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL: str = "https://api.openai.com/v1"
DEFAULT_MODEL: str = "gpt-4o-mini"
DEFAULT_TIMEOUT_SECONDS: float = 120.0
DEFAULT_MAX_TOKENS: int = 4096


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LLMMessage:
    """A single message in a chat conversation."""

    role: str  # "system", "user", "assistant"
    content: str | list[dict[str, Any]]  # text or multimodal content


@dataclass
class ToolCall:
    """A single tool/function call requested by the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Parsed response from the LLM API."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""
    # v5.9: populated when finish_reason == "tool_calls"
    tool_calls: list["ToolCall"] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LLMClient:
    """Async OpenAI-compatible LLM client.

    This client wraps ``httpx.AsyncClient`` to provide a clean
    interface for chat completions. It works with any API that
    implements the OpenAI chat/completions format.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        default_model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout = timeout_seconds
        self._default_max_tokens = default_max_tokens
        self._extra_headers = extra_headers or {}

    def _build_headers(self) -> dict[str, str]:
        """Build request headers."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(self._extra_headers)
        return headers

    # -- Core API -----------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]] | list[LLMMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        system: str | None = None,
        json_mode: bool = False,
        tools: list[dict[str, Any]] | None = None,   # v5.9: OpenAI tool specs
        tool_choice: str | dict | None = None,        # v5.9: "auto" | "none" | specific tool
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a chat completion request.

        Args:
            messages: List of message dicts with ``role`` and ``content``.
            model: Override the default model.
            max_tokens: Override the default max_tokens.
            temperature: Sampling temperature (0.0 - 2.0).
            system: Optional system message (prepended to messages).
            json_mode: If True, request JSON output format.
            **kwargs: Additional params forwarded to the API.

        Returns:
            An LLMResponse with the model's reply.

        Raises:
            LLMError: If the API request fails.
        """
        # Normalize messages
        msg_list: list[dict[str, Any]] = []
        if system:
            msg_list.append({"role": "system", "content": system})
        for msg in messages:
            if isinstance(msg, LLMMessage):
                msg_list.append({"role": msg.role, "content": msg.content})
            else:
                msg_list.append(msg)

        body: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": msg_list,
            "max_tokens": max_tokens or self._default_max_tokens,
            "temperature": temperature,
        }

        if json_mode:
            body["response_format"] = {"type": "json_object"}

        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"

        body.update(kwargs)

        url = f"{self._base_url}/chat/completions"
        logger.info(
            "LLM request: model=%s, messages=%d, max_tokens=%d",
            body["model"],
            len(msg_list),
            body["max_tokens"],
        )

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers=self._build_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            error_body = exc.response.text[:500] if exc.response else "unknown"
            logger.error("LLM API error %d: %s", exc.response.status_code, error_body)
            raise LLMError(f"API error {exc.response.status_code}: {error_body}") from exc
        except httpx.HTTPError as exc:
            logger.error("LLM request failed: %s", exc)
            raise LLMError(f"Request failed: {exc}") from exc

        # Parse response
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content") or ""
        usage = data.get("usage", {})

        # v5.9: parse tool_calls if present
        parsed_tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
            parsed_tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=args,
            ))

        response = LLMResponse(
            content=content,
            model=data.get("model", body["model"]),
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            finish_reason=choice.get("finish_reason", ""),
            tool_calls=parsed_tool_calls,
            raw=data,
        )

        logger.info(
            "LLM response: model=%s, tokens=%d, finish=%s",
            response.model,
            response.usage.get("total_tokens", 0),
            response.finish_reason,
        )
        return response

    # -- Convenience methods ------------------------------------------------

    async def ask(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Simple single-turn question → answer.

        Args:
            prompt: The user's question.
            system: Optional system prompt.
            model: Override model.

        Returns:
            The model's text response.
        """
        resp = await self.chat(
            messages=[{"role": "user", "content": prompt}],
            system=system,
            model=model,
            **kwargs,
        )
        return resp.content

    async def generate_image_prompt(
        self,
        description: str,
        *,
        style: str = "photorealistic",
        model: str | None = None,
    ) -> str:
        """Generate an optimized image generation prompt.

        Uses the LLM to craft a detailed prompt suitable for
        image generation models (DALL-E, Stable Diffusion, etc.).

        Args:
            description: High-level description of the desired image.
            style: Desired visual style.

        Returns:
            An optimized, detailed prompt string.
        """
        system = (
            "You are an expert prompt engineer for image generation. "
            "Given a description, create a detailed, specific prompt "
            "suitable for AI image generation. Include style, lighting, "
            "composition, and mood details. Output ONLY the prompt, "
            "no explanations."
        )
        prompt = f"Style: {style}\nDescription: {description}"
        return await self.ask(prompt, system=system, model=model)

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str = "dall-e-3",
        size: str = "1024x1024",
        quality: str = "standard",
        response_format: str = "b64_json",
    ) -> dict[str, Any]:
        """Generate an image using the images/generations endpoint.

        Args:
            prompt: Text prompt for image generation.
            model: Image model (dall-e-2, dall-e-3).
            size: Image dimensions.
            quality: Image quality (standard, hd).
            response_format: "url" or "b64_json".

        Returns:
            Dict with ``url`` or ``b64_json``, and ``revised_prompt``.
        """
        url = f"{self._base_url}/images/generations"
        body = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
            "response_format": response_format,
        }

        logger.info("Image generation: model=%s, size=%s", model, size)

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers=self._build_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            logger.error("Image generation failed: %s", exc)
            raise LLMError(f"Image generation failed: {exc}") from exc

        image_data = data.get("data", [{}])[0]
        return {
            "url": image_data.get("url"),
            "b64_json": image_data.get("b64_json"),
            "revised_prompt": image_data.get("revised_prompt", prompt),
            "model": model,
            "size": size,
        }


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Raised when an LLM API call fails."""
