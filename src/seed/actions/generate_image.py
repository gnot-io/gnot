"""Action — generate_image.

Generates an image from a text description using the LLM client's
image generation endpoint (OpenAI DALL-E or compatible).

Requires LLM configuration in node.yaml with a valid API key.
This is an async action.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
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
    """Generate an image from text description.

    Args:
        params:
            - prompt (str, required): Text description of the image
            - output_path (str, optional): Save image to this path
            - size (str, optional): Image size (default: 1024x1024)
            - model (str, optional): Image model (default: dall-e-3)
            - quality (str, optional): standard or hd (default: standard)
            - optimize_prompt (bool, optional): Use LLM to enhance prompt (default: false)
        context: Runtime context with ``llm`` client.

    Returns:
        Dict with image data, path, and metadata.
    """
    prompt = params.get("prompt")
    if not prompt:
        raise ValueError("Missing required param: prompt")

    llm = context.get("llm")
    if not llm:
        raise RuntimeError(
            "LLM client not configured. Add 'llm' section to node.yaml "
            "with api_key to enable image generation."
        )

    output_path = params.get("output_path")
    size = params.get("size", "1024x1024")
    model = params.get("model", "dall-e-3")
    quality = params.get("quality", "standard")
    optimize = params.get("optimize_prompt", False)

    # Tối ưu prompt bằng LLM nếu yêu cầu
    final_prompt = prompt
    if optimize:
        logger.info("Optimizing prompt with LLM...")
        final_prompt = await llm.generate_image_prompt(prompt)
        logger.info("Optimized prompt: %s", final_prompt[:100])

    # Gọi image generation API
    logger.info("Generating image: model=%s, size=%s", model, size)
    result = await llm.generate_image(
        prompt=final_prompt,
        model=model,
        size=size,
        quality=quality,
        response_format="b64_json" if output_path else "url",
    )

    output: dict[str, Any] = {
        "success": True,
        "prompt_used": final_prompt,
        "revised_prompt": result.get("revised_prompt", ""),
        "model": model,
        "size": size,
    }

    # Lưu file nếu có output_path
    if output_path and result.get("b64_json"):
        img_bytes = base64.b64decode(result["b64_json"])
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(img_bytes)
        output["saved_path"] = str(target.resolve())
        output["file_size_bytes"] = len(img_bytes)
        logger.info("Image saved: %s (%d bytes)", output_path, len(img_bytes))
    elif result.get("url"):
        output["image_url"] = result["url"]

    return output
