"""MCPClient — single MCP server connection for GNOT v6.0 Phase 3.

Implements the Model Context Protocol client for GNOT nodes.
Supports two transports:
  - stdio: subprocess (npx, uvx, python -m, …)
  - sse:   HTTP Server-Sent Events (remote MCP servers)

Protocol flow:
  1. connect() → send initialize request → receive initialized response
  2. list_tools() → tools/list request → parse tool definitions
  3. call_tool(name, args) → tools/call request → return result content

MCP JSON-RPC 2.0 wire format:
  Request:  {"jsonrpc": "2.0", "id": 1, "method": "...", "params": {...}}
  Response: {"jsonrpc": "2.0", "id": 1, "result": {...}}
  Notify:   {"jsonrpc": "2.0", "method": "notifications/...", ...}

Spec reference: https://spec.modelcontextprotocol.io/specification/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# Timeout for a single MCP JSON-RPC call
MCP_CALL_TIMEOUT = 30.0
# Maximum retries on transient failure
MCP_MAX_RETRIES = 3
MCP_RETRY_BACKOFF = [1.0, 2.0, 4.0]

_MCP_PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MCPError(Exception):
    """MCP protocol error."""


class MCPConnectionError(MCPError):
    """Failed to connect to MCP server."""


class MCPCallError(MCPError):
    """Tool call returned an error."""


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------

class MCPClient:
    """Client for ONE MCP server.

    Usage:
        client = MCPClient(server_id="filesystem", transport="stdio",
                           command="npx -y @modelcontextprotocol/server-filesystem /ws")
        info = await client.connect()
        tools = await client.list_tools()
        result = await client.call_tool("read_file", {"path": "/foo.txt"})
        await client.disconnect()

    Context manager:
        async with MCPClient(...) as client:
            tools = await client.list_tools()
    """

    def __init__(
        self,
        server_id: str,
        transport: str,                     # "stdio" | "sse"
        command: str | None = None,         # stdio only: shell command
        url: str | None = None,             # sse only
        env: dict[str, str] | None = None,  # extra env vars for subprocess
        timeout: float = MCP_CALL_TIMEOUT,
    ) -> None:
        self.server_id = server_id
        self.transport = transport
        self._command = command
        self._url = url
        self._env = env or {}
        self._timeout = timeout

        # stdio state
        self._proc: asyncio.subprocess.Process | None = None
        self._stdin_lock = asyncio.Lock()

        # sse state
        self._http_client = None

        self._req_id = 0
        self._connected = False
        self._server_info: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> dict[str, Any]:
        """Establish connection and perform MCP initialize handshake.

        Returns server info dict from initialize response.
        """
        if self.transport == "stdio":
            await self._connect_stdio()
        elif self.transport == "sse":
            await self._connect_sse()
        else:
            raise MCPConnectionError(f"Unknown transport: {self.transport}")

        # MCP initialize handshake
        resp = await self._call("initialize", {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {
                "roots": {"listChanged": False},
                "sampling": {},
            },
            "clientInfo": {
                "name": "gnot-mcp-client",
                "version": "6.0.0",
            },
        })

        self._server_info = resp.get("result", {})
        self._connected = True

        # Send initialized notification (required by spec)
        await self._notify("notifications/initialized", {})

        logger.info(
            "MCPClient connected: server_id=%s transport=%s server_name=%s",
            self.server_id, self.transport,
            self._server_info.get("serverInfo", {}).get("name", "unknown"),
        )
        return self._server_info

    async def disconnect(self) -> None:
        """Close the MCP server connection."""
        self._connected = False
        if self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

        logger.info("MCPClient disconnected: server_id=%s", self.server_id)

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Tool operations
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """Discover available tools from the MCP server.

        Returns list of raw tool definition dicts from the server.
        """
        resp = await self._call("tools/list", {})
        return resp.get("result", {}).get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        """Call a named tool with arguments.

        Returns list of content blocks (text, image, resource).
        Raises MCPCallError on tool-level errors.
        """
        resp = await self._call("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        result = resp.get("result", {})

        if result.get("isError"):
            content = result.get("content", [])
            error_text = _extract_text(content) or "Tool call failed"
            raise MCPCallError(f"MCP tool {name!r} returned error: {error_text}")

        return result.get("content", [])

    # ------------------------------------------------------------------
    # stdio transport
    # ------------------------------------------------------------------

    async def _connect_stdio(self) -> None:
        """Launch subprocess and establish stdio communication."""
        if not self._command:
            raise MCPConnectionError(f"stdio transport requires 'command' for {self.server_id}")

        # Merge environment
        proc_env = {**os.environ, **self._env}

        cmd_parts = self._command.split() if isinstance(self._command, str) else self._command

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )
        except FileNotFoundError as exc:
            raise MCPConnectionError(
                f"Failed to launch MCP server {self.server_id!r}: {exc}"
            ) from exc

        logger.info(
            "MCPClient stdio: launched PID=%s command=%s",
            self._proc.pid, self._command,
        )

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        self._req_id += 1
        req_id = self._req_id

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        if self.transport == "stdio":
            return await self._call_stdio(payload)
        elif self.transport == "sse":
            return await self._call_sse(payload)
        else:
            raise MCPError(f"Unknown transport: {self.transport}")

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params}

        if self.transport == "stdio" and self._proc and self._proc.stdin:
            async with self._stdin_lock:
                line = json.dumps(payload, ensure_ascii=False) + "\n"
                try:
                    self._proc.stdin.write(line.encode())
                    await self._proc.stdin.drain()
                except Exception as exc:
                    logger.warning("MCPClient notify failed: %s", exc)

    async def _call_stdio(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send request and read response over stdio."""
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise MCPConnectionError(f"stdio process not running for {self.server_id}")

        line = json.dumps(payload, ensure_ascii=False) + "\n"

        async with self._stdin_lock:
            try:
                self._proc.stdin.write(line.encode())
                await self._proc.stdin.drain()
            except Exception as exc:
                raise MCPConnectionError(f"Failed to write to MCP stdin: {exc}") from exc

        # Read until we get the matching response (ignore notifications)
        req_id = payload["id"]
        deadline = time.time() + self._timeout

        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    self._proc.stdout.readline(),
                    timeout=min(self._timeout, deadline - time.time()),
                )
            except asyncio.TimeoutError:
                raise MCPError(
                    f"Timeout waiting for response to {payload['method']} "
                    f"from {self.server_id}"
                )

            if not raw:
                raise MCPConnectionError(f"MCP server {self.server_id} closed stdout")

            raw_str = raw.decode("utf-8", errors="replace").strip()
            if not raw_str:
                continue

            try:
                obj = json.loads(raw_str)
            except json.JSONDecodeError:
                logger.debug("MCPClient: non-JSON line from %s: %.200s", self.server_id, raw_str)
                continue

            # Skip notifications (no "id" field or id is None)
            if obj.get("id") is None:
                logger.debug("MCPClient notification from %s: %s", self.server_id, obj.get("method"))
                continue

            if obj.get("id") == req_id:
                if "error" in obj:
                    err = obj["error"]
                    raise MCPError(
                        f"MCP JSON-RPC error {err.get('code')}: {err.get('message')}"
                    )
                return obj

            # Response for different request ID — log and continue reading
            logger.debug(
                "MCPClient: got response for id=%s, waiting for id=%s",
                obj.get("id"), req_id,
            )

        raise MCPError(f"Timeout: no response for request {req_id} from {self.server_id}")

    # ------------------------------------------------------------------
    # SSE transport
    # ------------------------------------------------------------------

    async def _connect_sse(self) -> None:
        """Connect to SSE-based MCP server."""
        if not self._url:
            raise MCPConnectionError(f"sse transport requires 'url' for {self.server_id}")

        try:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=self._timeout)
        except ImportError as exc:
            raise MCPConnectionError(
                "httpx required for SSE transport: pip install httpx"
            ) from exc

        logger.info("MCPClient SSE: url=%s server_id=%s", self._url, self.server_id)

    async def _call_sse(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send request to SSE-based MCP server via HTTP POST."""
        if self._http_client is None:
            raise MCPConnectionError(f"SSE client not initialized for {self.server_id}")

        # Standard MCP-over-HTTP: POST /message, GET /sse for server-sent events
        # Simplified: use POST /message which returns JSON response directly
        endpoint = self._url.rstrip("/") + "/message"

        try:
            resp = await self._http_client.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise MCPError(f"SSE call failed for {self.server_id}: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(content: list[dict[str, Any]]) -> str:
    """Extract text from MCP content blocks."""
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif "text" in block:
                texts.append(block["text"])
    return "\n".join(texts)
