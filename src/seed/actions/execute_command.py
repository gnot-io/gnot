"""Seed action — execute_command.

Runs an arbitrary shell command asynchronously with timeout enforcement.
This is the only seed action marked ASYNC = True.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Async flag — JobManager will spawn this as a background task
# ---------------------------------------------------------------------------

ASYNC: bool = True

DEFAULT_TIMEOUT_SECONDS: int = 60


# ---------------------------------------------------------------------------
# Action entry point
# ---------------------------------------------------------------------------

async def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Execute a shell command and return its output.

    Args:
        params: Must contain ``command`` (str).
                Optional ``timeout_seconds`` (int, default 60).
        context: Runtime context injected by the executor.

    Returns:
        Dict with ``exit_code``, ``stdout``, and ``stderr``.
    """
    command = params.get("command")
    if not command:
        raise ValueError("Missing required param: command")

    timeout = params.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

    logger.info("execute_command: %s (timeout=%ds)", command, timeout)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )

        exit_code = proc.returncode or 0
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        logger.info(
            "execute_command finished: exit_code=%d, stdout_len=%d",
            exit_code,
            len(stdout),
        )

        return {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }

    except asyncio.TimeoutError:
        logger.error("execute_command timed out after %ds: %s", timeout, command)
        # Kill the process if still running
        try:
            proc.kill()  # type: ignore[union-attr]
            await proc.wait()  # type: ignore[union-attr]
        except Exception:
            pass
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Timeout after {timeout}s",
        }
