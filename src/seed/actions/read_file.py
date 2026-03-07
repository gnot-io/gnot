"""Seed action — read_file.

Reads the content of a file on disk and returns it as a string.
This is a synchronous action (no ASYNC flag).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Read content from a file path.

    Args:
        params: Must contain ``path`` (str).
        context: Runtime context injected by the executor.

    Returns:
        Dict with ``content`` (str) and ``size_bytes`` (int).

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    file_path = params.get("path")

    if not file_path:
        raise ValueError("Missing required param: path")

    target = Path(file_path)
    if not target.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    content = target.read_text(encoding="utf-8")
    size = target.stat().st_size

    logger.info("read_file: %s (%d bytes)", file_path, size)
    return {"content": content, "size_bytes": size}
