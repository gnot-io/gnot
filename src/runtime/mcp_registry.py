"""MCPRegistry — manages all MCP server connections for GNOT v6.0 Phase 3.

Connects to configured MCP servers on startup, discovers tools via tools/list,
converts tool definitions to OpenAI-compatible format, and routes tool calls
to the correct server.

Tool naming convention:
    mcp__{server_id}__{tool_name}
    e.g. mcp__filesystem__read_file, mcp__github__search_repos

This namespace prevents collision with GNOT actions and makes routing
unambiguous: any tool whose name starts with "mcp__" is an MCP tool.

IntentHandler integration:
    tools = [MESH_TOOL_SPEC] + mcp_registry.get_tool_specs()
    # route: if mcp_registry.is_mcp_tool(tc.name): call_tool(tc.name, tc.arguments)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from runtime.mcp_client import MCPClient, MCPError, _extract_text

logger = logging.getLogger(__name__)

MCP_TOOL_PREFIX = "mcp__"


# ---------------------------------------------------------------------------
# MCPRegistry
# ---------------------------------------------------------------------------

class MCPRegistry:
    """Manages all MCP server connections.

    Lifecycle:
        registry = MCPRegistry(server_configs)
        await registry.startup()        # connect all, discover tools
        ...
        specs = registry.get_tool_specs()
        result = await registry.call_tool("mcp__fs__read_file", {"path": "/"})
        ...
        await registry.shutdown()
    """

    def __init__(self, server_configs: list[dict[str, Any]]) -> None:
        """
        Args:
            server_configs: list of dicts from node.yaml mcp_servers section.
                Each: {id, transport, command?, url?, env?}
        """
        self._configs = server_configs
        self._clients: dict[str, MCPClient] = {}         # server_id → MCPClient
        self._tools: dict[str, dict[str, Any]] = {}      # qualified_name → raw tool def
        self._tool_to_server: dict[str, str] = {}        # qualified_name → server_id
        self._openai_specs: list[dict[str, Any]] = []    # cached OpenAI-format specs
        self._lock = asyncio.Lock()
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Connect all configured MCP servers and discover their tools."""
        if self._started:
            return

        connect_tasks = []
        for cfg in self._configs:
            sid = cfg.get("id")
            if not sid:
                logger.warning("MCP server config missing 'id' field — skipping: %s", cfg)
                continue
            transport = cfg.get("transport", "stdio")
            client = MCPClient(
                server_id=sid,
                transport=transport,
                command=cfg.get("command"),
                url=cfg.get("url"),
                env=cfg.get("env", {}),
            )
            self._clients[sid] = client
            connect_tasks.append(self._connect_one(sid, client))

        # Connect concurrently; partial failure is OK (log + continue)
        if connect_tasks:
            await asyncio.gather(*connect_tasks, return_exceptions=True)

        self._rebuild_openai_specs()
        self._started = True

        total_tools = len(self._tools)
        server_count = sum(1 for c in self._clients.values() if c._connected)
        logger.info(
            "MCPRegistry started: %d/%d servers connected, %d tools discovered",
            server_count, len(self._clients), total_tools,
        )

    async def _connect_one(self, server_id: str, client: MCPClient) -> None:
        """Connect a single MCP server and discover its tools."""
        try:
            await client.connect()
            tools = await client.list_tools()

            async with self._lock:
                for tool in tools:
                    name = tool.get("name", "")
                    if not name:
                        continue
                    qualified = f"{MCP_TOOL_PREFIX}{server_id}__{name}"
                    self._tools[qualified] = tool
                    self._tool_to_server[qualified] = server_id

            logger.info(
                "MCP server %s: connected, %d tools discovered", server_id, len(tools)
            )
        except Exception as exc:
            logger.warning(
                "MCP server %s: failed to connect — %s (will operate without it)",
                server_id, exc,
            )

    async def shutdown(self) -> None:
        """Disconnect all MCP servers."""
        tasks = [c.disconnect() for c in self._clients.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._clients.clear()
        self._tools.clear()
        self._tool_to_server.clear()
        self._openai_specs.clear()
        self._started = False
        logger.info("MCPRegistry shutdown")

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    def get_tool_specs(self) -> list[dict[str, Any]]:
        """Return OpenAI-compatible tool specs for all discovered MCP tools."""
        return list(self._openai_specs)

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Return True if this tool name belongs to an MCP server."""
        return tool_name.startswith(MCP_TOOL_PREFIX) or tool_name in self._tools

    def list_servers(self) -> list[dict[str, Any]]:
        """Return list of server status dicts for GET /mcp/servers."""
        result = []
        for sid, client in self._clients.items():
            result.append({
                "server_id": sid,
                "transport": client.transport,
                "connected": client._connected,
                "tool_count": sum(
                    1 for qname, svr in self._tool_to_server.items() if svr == sid
                ),
            })
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        """Return list of tool info dicts for GET /mcp/tools."""
        result = []
        for qualified_name, tool_def in self._tools.items():
            server_id = self._tool_to_server.get(qualified_name, "unknown")
            result.append({
                "qualified_name": qualified_name,
                "server_id": server_id,
                "name": tool_def.get("name", ""),
                "description": tool_def.get("description", ""),
                "input_schema": tool_def.get("inputSchema", {}),
            })
        return result

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    async def call_tool(
        self, qualified_name: str, arguments: dict[str, Any]
    ) -> str:
        """Call an MCP tool and return result as a string.

        Args:
            qualified_name: "mcp__{server_id}__{tool_name}" format.
            arguments: Tool arguments dict.

        Returns:
            Result as a string (text content extracted from MCP content blocks).
        """
        server_id = self._tool_to_server.get(qualified_name)
        if server_id is None:
            # Try to extract server_id from qualified name
            parts = qualified_name.split("__")
            if len(parts) >= 3 and parts[0] == "mcp":
                server_id = parts[1]
                tool_name = "__".join(parts[2:])
            else:
                return json.dumps({
                    "error": "MCP_TOOL_NOT_FOUND",
                    "tool": qualified_name,
                })
        else:
            # Extract bare tool name from qualified name
            prefix = f"{MCP_TOOL_PREFIX}{server_id}__"
            tool_name = qualified_name[len(prefix):]

        client = self._clients.get(server_id)
        if client is None or not client._connected:
            return json.dumps({
                "error": "MCP_SERVER_NOT_CONNECTED",
                "server_id": server_id,
                "tool": qualified_name,
            })

        try:
            content_blocks = await client.call_tool(tool_name, arguments)
            text = _extract_text(content_blocks)
            if not text:
                # Return JSON representation of content blocks
                text = json.dumps(content_blocks)
            logger.debug(
                "MCP tool call: %s → %d chars", qualified_name, len(text)
            )
            return text
        except MCPError as exc:
            logger.warning("MCP tool call failed: %s — %s", qualified_name, exc)
            return json.dumps({"error": str(exc), "tool": qualified_name})
        except Exception as exc:
            logger.error("MCP tool call unexpected error: %s — %s", qualified_name, exc)
            return json.dumps({"error": f"UNEXPECTED: {exc}", "tool": qualified_name})

    # ------------------------------------------------------------------
    # Internal: spec conversion
    # ------------------------------------------------------------------

    def _rebuild_openai_specs(self) -> None:
        """Convert raw MCP tool definitions to OpenAI-compatible tool specs."""
        specs = []
        for qualified_name, tool_def in self._tools.items():
            spec = _mcp_tool_to_openai_spec(qualified_name, tool_def)
            specs.append(spec)
        self._openai_specs = specs


def _mcp_tool_to_openai_spec(
    qualified_name: str, mcp_tool: dict[str, Any]
) -> dict[str, Any]:
    """Convert an MCP tool definition to OpenAI tool spec format.

    MCP format:
        {name, description, inputSchema: {type: "object", properties: {...}, required: [...]}}

    OpenAI format:
        {type: "function", function: {name, description, parameters: {...}}}
    """
    input_schema = mcp_tool.get("inputSchema", {})

    # Ensure valid JSON Schema object
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": input_schema.get("properties", {}),
    }
    if "required" in input_schema:
        parameters["required"] = input_schema["required"]

    description = mcp_tool.get("description", "")
    # Truncate very long descriptions to avoid context window bloat
    if len(description) > 500:
        description = description[:497] + "..."

    return {
        "type": "function",
        "function": {
            "name": qualified_name,
            "description": description,
            "parameters": parameters,
        },
    }
