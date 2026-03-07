"""Seed action — write_file.

Writes content to a file on disk. Creates parent directories if needed.
This is a synchronous action (no ASYNC flag).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Write content to a file path.

    Args:
        params: Must contain ``path`` (str) and ``content`` (str).
        context: Runtime context injected by the executor.

    Returns:
        Dict with ``success`` flag and the written ``path``.
    """
    file_path = params.get("path")
    content = params.get("content")

    if not file_path:
        raise ValueError("Missing required param: path")
    if content is None:
        raise ValueError("Missing required param: content")

    target = Path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    logger.info("write_file: %s (%d bytes)", file_path, len(content))
    return {"success": True, "path": str(target.resolve())}
