"""Action — llm_chat.

General-purpose LLM chat action. Sends a prompt to the configured
LLM provider and returns the response. Supports system prompts,
conversation history, JSON mode, and model override.

Requires LLM configuration in node.yaml with a valid API key.
This is an async action.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Async flag
# ---------------------------------------------------------------------------

ASYNC: bool = True


# ---------------------------------------------------------------------------
# Action entry point
# ---------------------------------------------------------------------------

async def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Send a chat completion request to the configured LLM.

    Args:
        params:
            - prompt (str, required): User message
            - system (str, optional): System prompt
            - messages (list, optional): Full conversation history
              (overrides prompt if provided)
            - model (str, optional): Override default model
            - temperature (float, optional): Sampling temperature (default 0.7)
            - max_tokens (int, optional): Max response tokens
            - json_mode (bool, optional): Request JSON output
        context: Runtime context with ``llm`` client.

    Returns:
        Dict with response content, model, and usage stats.
    """
    llm = context.get("llm")
    if not llm:
        raise RuntimeError(
            "LLM client not configured. Add 'llm' section to node.yaml "
            "with api_key to enable LLM actions."
        )

    prompt = params.get("prompt")
    messages = params.get("messages")
    system = params.get("system")
    model = params.get("model")
    temperature = params.get("temperature", 0.7)
    max_tokens = params.get("max_tokens")
    json_mode = params.get("json_mode", False)

    if not prompt and not messages:
        raise ValueError("Missing required param: prompt or messages")

    # Xây dựng messages list
    if messages:
        msg_list = messages
    else:
        msg_list = [{"role": "user", "content": prompt}]

    logger.info("LLM chat: model=%s, messages=%d", model or "default", len(msg_list))

    response = await llm.chat(
        messages=msg_list,
        system=system,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )

    return {
        "content": response.content,
        "model": response.model,
        "usage": response.usage,
        "finish_reason": response.finish_reason,
    }
