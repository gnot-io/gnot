"""Seed action — write_file_b64.

Accepts a Base64-encoded string and writes the decoded bytes to disk.
The counterpart of read_file_b64 — together they enable binary-safe
file transfer between mesh nodes.

Typical transfer workflow orchestrated by the Cloud Planner:
    Step 1:  read_file_b64 {path: "/var/backup/db.sql.gz"}  on node-1
             → {content_b64: "...", size_bytes: 52428800}

    Step 2:  write_file_b64 {path: "/backups/db.sql.gz",
                              content_b64: "<from step 1>"}  on node-0
             → {success: true, size_bytes: 52428800, path: "..."}
"""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Decode Base64 content and write it to a file.

    Args:
        params:
            path        (str, required): Destination file path.
                                         Parent directories are created automatically.
            content_b64 (str, required): Base64-encoded file content
                                         (as returned by read_file_b64).

        context: Runtime context injected by the executor.

    Returns:
        Dict with:
            success    (bool): Always True on success.
            path       (str):  Resolved absolute path of the written file.
            size_bytes (int):  Number of bytes written.
    """
    path_str = params.get("path")
    content_b64 = params.get("content_b64")

    if not path_str:
        raise ValueError("Missing required param: path")
    if content_b64 is None:
        raise ValueError("Missing required param: content_b64")

    try:
        raw_bytes = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid Base64 content: {exc}") from exc

    target = Path(path_str)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw_bytes)

    size = len(raw_bytes)
    logger.info("write_file_b64: %s (%d bytes written)", path_str, size)
    return {
        "success": True,
        "path": str(target.resolve()),
        "size_bytes": size,
    }
