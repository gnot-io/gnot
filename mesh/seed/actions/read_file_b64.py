"""Seed action — read_file_b64.

Reads any file (text or binary) and returns its content as a Base64-encoded
string. Safe for binary files (database dumps, compressed archives, etc.)
that would be corrupted by UTF-8 decoding.

Used together with write_file_b64 to transfer files between mesh nodes:
  1. read_file_b64  on source node  → base64 content
  2. write_file_b64 on target node  ← base64 content

File size warning: large files (>50 MB) will produce large JSON payloads.
For production use with very large dumps, prefer scp/rsync via execute_command.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Read a file and return its content as a Base64 string.

    Args:
        params:
            path (str, required): File path to read.
            encoding (str, optional): 'base64' (default) or 'utf8'.
                Use 'utf8' only for known text files.

        context: Runtime context injected by the executor.

    Returns:
        Dict with:
            content_b64 (str): Base64-encoded file content.
            size_bytes  (int): Original file size in bytes.
            path        (str): Resolved absolute path.
    """
    path_str = params.get("path")
    if not path_str:
        raise ValueError("Missing required param: path")

    target = Path(path_str)
    if not target.exists():
        raise FileNotFoundError(f"File not found: {path_str}")

    raw_bytes = target.read_bytes()
    size = len(raw_bytes)
    content_b64 = base64.b64encode(raw_bytes).decode("ascii")

    logger.info("read_file_b64: %s (%d bytes → %d b64 chars)", path_str, size, len(content_b64))
    return {
        "content_b64": content_b64,
        "size_bytes": size,
        "path": str(target.resolve()),
    }
