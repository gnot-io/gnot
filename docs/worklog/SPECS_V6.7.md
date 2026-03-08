# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v6.7
### MCP Native Support · Tool Discovery · Bidirectional MCP Integration

**Base version:** v6.6
**Target version:** v6.7
**Status:** Analysis complete — Approach B selected, Approach C roadmapped
**Authors:** Architecture review session, 2026-03-08

---

## 1. Yêu cầu

### 1.1 Requirement từ product owner

> "Hệ thống hiện tại đã hỗ trợ kết nối và gọi các action của MCP server chưa?"

Câu hỏi này dẫn đến một phân tích đầy đủ về ba cách tiếp cận để
integrate MCP (Model Context Protocol) vào GNOT runtime.

### 1.2 MCP là gì — context ngắn gọn

Model Context Protocol (MCP) là open standard do Anthropic định nghĩa,
cho phép LLM applications kết nối với external tools và data sources
thông qua một giao thức chuẩn hóa.

```
MCP ecosystem:
  MCP Client (host): Claude Desktop, Claude API, OpenClaw, n8n, ...
  MCP Server:        filesystem, GitHub, Postgres, Slack, custom tools, ...
  Transport:         stdio (subprocess) hoặc SSE (HTTP streaming)
  Protocol:          JSON-RPC 2.0

MCP handshake:
  1. client → initialize {clientInfo, capabilities}
  2. server → initialize response {serverInfo, capabilities}
  3. client → tools/list
  4. server → {tools: [{name, description, inputSchema}]}
  5. client → tools/call {name, arguments}
  6. server → {content: [{type, text}]}
```

MCP tools được expose lên LLM y hệt OpenAI function calling —
tool name, description, và JSON Schema cho parameters.

---

## 2. Baseline audit — v5.13b

### 2.1 Source code scan

```bash
grep -rn "mcp" gnot/src/   → (no results)
cat requirements.txt        → không có mcp, mcp-client, hay bất kỳ MCP package
```

**Kết luận: Zero MCP support.** Không có một dòng code nào liên quan.

### 2.2 Hiểu model action hiện tại

Để thiết kế MCP integration đúng, cần hiểu rõ execution pipeline hiện tại:

```
POST /intent {session_id, prompt}
    ↓
IntentHandler.handle()
    ↓ get_or_create session
    ↓ build system_prompt
    ↓
LLMClient.chat(
    messages=session.messages,
    tools=[MESH_TOOL_SPEC],         ← chỉ 1 tool duy nhất
    system=system_prompt
)
    ↓
LLM responds tool_calls: [
    {name: "mesh_action", arguments: {
        target_node_id: "node-1",
        action: "execute_command",
        params: {command: "ls -la"}
    }}
]
    ↓
IntentHandler._execute_tool_call()
    ↓
GatewayRouter.route(ActionRequest)
    ↓
ActionExecutor.execute(action_name, params)
    ↓
ActionRegistry["execute_command"].run(params, context)
    ↑
    Python module trong actions_dir/
    loaded bằng importlib
```

**Điểm quan trọng:**
- `LLMClient.chat()` nhận `tools: list[dict]` — đã generic, có thể nhận N tools
- `IntentHandler` hiện chỉ pass `[MESH_TOOL_SPEC]` — một tool duy nhất
- `ActionRegistry` là `dict[str, ModuleType]` — chỉ biết Python modules
- Không có path nào cho external process (MCP server) calls

### 2.3 Gap map

```
MCP requirement          GNOT v5.13b          Gap
─────────────────────────────────────────────────────
Connect MCP server       No MCPClient         ❌ Missing component
Discover tools           No discovery         ❌ No tools/list call
Expose tool schemas      Only mesh_action     ❌ MCP tools not in tools array
Call MCP tool            No mcp call path     ❌ No execution path
stdio transport          No subprocess mgmt   ❌
SSE transport            No SSE client        ❌
node.yaml config         No mcp_servers key   ❌
```

---

## 3. Ba approaches — phân tích đầy đủ

---

### Approach A — MCP-as-Action (Shallow Bridge)

#### A.1 Thiết kế

Wrap MCP client logic vào một GNOT seed action thông thường.
LLM gọi `mesh_action` với action name `mcp_call`, truyền server name,
tool name, và arguments. Action Python module tự quản lý kết nối MCP.

```
LLM call:
  mesh_action("node-0", "mcp_call", {
      "server":    "filesystem",
      "tool":      "read_file",
      "arguments": {"path": "/etc/config.yaml"}
  })

Execution path:
  ActionRegistry["mcp_call"].run(params, context)
      ↓
  MCPClient.call(
      server=params["server"],
      tool=params["tool"],
      arguments=params["arguments"]
  )
      ↓
  MCP JSON-RPC → subprocess/SSE
      ↓
  return {"content": "...file content..."}
```

**Config trong node.yaml:**
```yaml
mcp_servers:
  filesystem:
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-filesystem /workspace"
  github:
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-github"
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
```

**Tool description injection (thủ công trong system prompt):**
```yaml
intent_system_prompt: |
  You have access to MCP servers via mcp_call action:
  - filesystem: read_file(path), write_file(path, content), list_directory(path)
  - github: search_repositories(query), get_file_contents(owner, repo, path)
  Use: mesh_action("self", "mcp_call", {"server": "...", "tool": "...", "arguments": {...}})
```

**Thay đổi cần thiết:**
- `seed/actions/mcp_call.py` — MCP client, manage connections, call tools
- `node.yaml` — thêm `mcp_servers:` section
- `runtime/config.py` — parse `mcp_servers` vào NodeConfig
- Không sửa `IntentHandler`, `LLMClient`, hay bất kỳ runtime component nào

#### A.2 Pros

- **Implementation nhỏ nhất** — chỉ thêm một action module
- **Backward compat hoàn toàn** — không sửa bất kỳ existing component
- **Ship nhanh** — 1–2 ngày
- **Dễ debug** — MCP call là một action call bình thường, có log đầy đủ

#### A.3 Cons — tại sao approach này không đủ

**Con A.1: LLM mù về MCP tool schemas**

Đây là vấn đề cốt lõi và không thể khắc phục trong Approach A.

LLM chỉ thấy một tool:
```json
{"name": "mesh_action", "parameters": {
    "target_node_id": "string",
    "action": "string",
    "params": "object"   ← untyped blob
}}
```

LLM không biết `read_file` cần `path` string, không biết `search_repositories`
cần `query` string hay `per_page` optional integer. Nó phải đoán từ system prompt
text — fragile, error-prone, và không scale.

So sánh với native tool injection (Approach B):
```json
{"name": "read_file", "parameters": {
    "path": {"type": "string", "description": "Absolute path to file"}
}}
{"name": "search_repositories", "parameters": {
    "query": {"type": "string", "required": true},
    "per_page": {"type": "integer", "default": 10}
}}
```

Với native schemas, LLM biết chính xác cần truyền gì. Ít hallucination hơn,
ít wrong arguments hơn.

**Con A.2: System prompt maintenance burden**

Operator phải manually maintain tool descriptions trong `intent_system_prompt`.
Khi MCP server update (thêm tool mới, đổi argument name), operator phải
update system prompt thủ công. Không có auto-discovery.

**Con A.3: Không thể hiện "native MCP support"**

"Native MCP support" có nghĩa: LLM interact với MCP tools y hệt cách nó
interact với bất kỳ tool nào khác — qua OpenAI function calling protocol,
với full type schemas. Approach A là một workaround, không phải native support.

---

### Approach B — MCPClient + Native Tool Injection ✅ **(Selected)**

#### B.1 Thiết kế

Thêm `MCPClient` component. Tại node startup, connect đến tất cả MCP servers
trong config, gọi `tools/list` để discover tools và schemas. Inject discovered
tool specs vào `tools` array của mỗi LLM call — alongside `mesh_action`.

```
Node startup:
  MCPRegistry.startup()
      ↓
  for each mcp_server in config.mcp_servers:
      MCPClient.connect(server)   → initialize handshake
      tools = MCPClient.list_tools(server)
      MCPRegistry.register(server, tools)

tools discovered:
  filesystem → [read_file, write_file, list_directory, create_directory, ...]
  github     → [search_repositories, get_file_contents, create_issue, ...]
  postgres   → [query, execute, list_tables, describe_table]

IntentHandler.handle():
  mcp_tool_specs = mcp_registry.get_all_tool_specs()
  tools = [MESH_TOOL_SPEC] + mcp_tool_specs   ← LLM thấy ALL tools

  llm.chat(messages, tools=tools)   ← native tool schemas in tools array

LLM responds:
  → mesh_action(...)               → GatewayRouter (existing path)
  → read_file(path="...")          → MCPClient.call("filesystem", "read_file", ...) [NEW]
  → search_repositories(query="…") → MCPClient.call("github", ...) [NEW]
```

#### B.2 MCPClient component

```python
# runtime/mcp_client.py

class MCPTransport:
    """Base class for MCP transports."""
    async def send(self, message: dict) -> dict: ...
    async def close(self) -> None: ...


class StdioTransport(MCPTransport):
    """
    MCP over stdio — spawns a subprocess and communicates via stdin/stdout.
    This is the most common MCP transport (npx, uvx, python -m).

    Message framing: newline-delimited JSON.
    Each message is one JSON line on stdin/stdout.
    """

    def __init__(self, command: str, env: dict[str, str] | None = None) -> None:
        self._command = command
        self._env = env or {}
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        import os
        merged_env = {**os.environ, **self._env}
        self._process = await asyncio.create_subprocess_shell(
            self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        logger.info("MCP stdio process started: %s (pid=%d)", self._command, self._process.pid)

    async def send(self, message: dict) -> dict:
        async with self._lock:
            line = json.dumps(message) + "\n"
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()
            response_line = await self._process.stdout.readline()
            return json.loads(response_line.decode())

    async def close(self) -> None:
        if self._process:
            self._process.terminate()
            await self._process.wait()


class SSETransport(MCPTransport):
    """
    MCP over SSE (Server-Sent Events) — connects to a running HTTP MCP server.
    Used for remote MCP servers (e.g., cloud-hosted tools).

    POST {url}/message  → send JSON-RPC request
    GET  {url}          → SSE stream for responses
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self._url = url.rstrip("/")
        self._headers = headers or {}
        self._client = httpx.AsyncClient(timeout=30.0, headers=self._headers)

    async def send(self, message: dict) -> dict:
        resp = await self._client.post(
            f"{self._url}/message",
            json=message,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()


class MCPClient:
    """
    Client for a single MCP server.

    Handles:
      - Connection lifecycle (connect, initialize, close)
      - Tool discovery (tools/list)
      - Tool calls (tools/call)
      - JSON-RPC 2.0 message framing
    """

    def __init__(self, server_id: str, transport: MCPTransport) -> None:
        self._server_id = server_id
        self._transport = transport
        self._request_id = 0
        self._initialized = False
        self._server_info: dict = {}

    async def connect(self) -> dict:
        """
        Perform MCP initialize handshake.
        Returns server info including server name, version, capabilities.
        """
        if hasattr(self._transport, "start"):
            await self._transport.start()

        response = await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {
                "name": "gnot-runtime",
                "version": "6.7",
            },
            "capabilities": {
                "tools": {},
            },
        })
        self._server_info = response.get("result", {})
        self._initialized = True
        logger.info(
            "MCP server connected: %s → %s",
            self._server_id,
            self._server_info.get("serverInfo", {}).get("name", "unknown"),
        )
        return self._server_info

    async def list_tools(self) -> list[dict]:
        """
        Discover all tools exposed by this MCP server.
        Returns list of tool dicts with name, description, inputSchema.
        """
        response = await self._rpc("tools/list", {})
        return response.get("result", {}).get("tools", [])

    async def call_tool(self, tool_name: str, arguments: dict) -> list[dict]:
        """
        Call a tool on the MCP server.
        Returns list of content blocks: [{type: "text", text: "..."}]
        """
        response = await self._rpc("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        result = response.get("result", {})
        if result.get("isError"):
            error_content = result.get("content", [{"type": "text", "text": "Unknown error"}])
            raise MCPToolError(
                tool=tool_name,
                server=self._server_id,
                detail=_extract_text(error_content),
            )
        return result.get("content", [])

    async def close(self) -> None:
        await self._transport.close()

    async def _rpc(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC 2.0 request and return the response."""
        self._request_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        response = await self._transport.send(message)
        if "error" in response:
            raise MCPRPCError(
                method=method,
                code=response["error"].get("code"),
                message=response["error"].get("message", ""),
            )
        return response


class MCPRPCError(Exception):
    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"MCP RPC error on {method}: [{code}] {message}")
        self.method = method
        self.code = code


class MCPToolError(Exception):
    def __init__(self, tool: str, server: str, detail: str) -> None:
        super().__init__(f"MCP tool error: {server}/{tool}: {detail}")
        self.tool = tool
        self.server = server
        self.detail = detail


def _extract_text(content_blocks: list[dict]) -> str:
    return "\n".join(
        b.get("text", "") for b in content_blocks if b.get("type") == "text"
    )
```

#### B.3 MCPRegistry component

```python
# runtime/mcp_registry.py

@dataclass
class MCPTool:
    """A single tool discovered from an MCP server."""
    server_id: str
    name: str               # original MCP tool name (e.g., "read_file")
    qualified_name: str     # namespaced name exposed to LLM (e.g., "mcp__filesystem__read_file")
    description: str
    input_schema: dict      # JSON Schema for parameters
    raw: dict               # original MCP tool dict


class MCPRegistry:
    """
    Manages all MCP server connections and tool discovery for a node.

    Lifecycle:
      startup()    → connect all servers, discover tools
      shutdown()   → close all connections
      rebuild()    → reconnect and rediscover (after config change)

    Tool naming convention:
      MCP tool "read_file" on server "filesystem"
      → exposed as "mcp__filesystem__read_file"

    Rationale for namespacing:
      - Prevents collisions between servers (two servers may have same tool name)
      - LLM sees "mcp__filesystem__read_file" — unambiguous which server to call
      - When routing tool call back, parse prefix to find server + tool name

    Tool spec format (OpenAI-compatible):
      {
        "type": "function",
        "function": {
          "name": "mcp__filesystem__read_file",
          "description": "[filesystem] Read complete contents of a file. ...",
          "parameters": { ... JSON Schema ... }
        }
      }
    """

    MCP_PREFIX = "mcp__"
    MCP_SEP = "__"

    def __init__(self, server_specs: list["MCPServerSpec"]) -> None:
        self._specs = {s.server_id: s for s in server_specs}
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, MCPTool] = {}   # qualified_name → MCPTool

    async def startup(self) -> None:
        """Connect to all configured MCP servers and discover tools."""
        for server_id, spec in self._specs.items():
            try:
                client = _build_client(server_id, spec)
                await client.connect()
                raw_tools = await client.list_tools()
                self._clients[server_id] = client
                for raw_tool in raw_tools:
                    tool = self._register_tool(server_id, raw_tool)
                    logger.info("MCP tool registered: %s", tool.qualified_name)
                logger.info(
                    "MCP server %s ready — %d tool(s)", server_id, len(raw_tools)
                )
            except Exception as e:
                logger.error(
                    "MCP server %s failed to connect: %s — skipping", server_id, e
                )

    async def shutdown(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        self._tools.clear()

    def get_tool_specs(self) -> list[dict]:
        """
        Return all discovered MCP tools as OpenAI-compatible tool specs.
        These are passed directly into LLMClient.chat(tools=...).
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.qualified_name,
                    "description": f"[{tool.server_id}] {tool.description}",
                    "parameters": tool.input_schema,
                },
            }
            for tool in self._tools.values()
        ]

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name.startswith(self.MCP_PREFIX) and tool_name in self._tools

    async def call_tool(self, qualified_name: str, arguments: dict) -> str:
        """
        Execute an MCP tool by qualified name.
        Returns result as a string (for injection into LLM tool result message).
        """
        tool = self._tools.get(qualified_name)
        if not tool:
            return json.dumps({"error": "MCP_TOOL_NOT_FOUND", "name": qualified_name})

        client = self._clients.get(tool.server_id)
        if not client:
            return json.dumps({"error": "MCP_SERVER_DISCONNECTED", "server": tool.server_id})

        try:
            content_blocks = await client.call_tool(tool.name, arguments)
            # Flatten content blocks to string
            text = "\n".join(
                b.get("text", json.dumps(b)) for b in content_blocks
            )
            return text
        except MCPToolError as e:
            return json.dumps({"error": "MCP_TOOL_ERROR", "detail": str(e)})
        except Exception as e:
            logger.error("MCP tool call failed: %s / %s: %s", tool.server_id, tool.name, e)
            return json.dumps({"error": "MCP_CALL_FAILED", "detail": str(e)})

    def _register_tool(self, server_id: str, raw_tool: dict) -> MCPTool:
        original_name = raw_tool["name"]
        qualified = f"{self.MCP_PREFIX}{server_id}{self.MCP_SEP}{original_name}"
        tool = MCPTool(
            server_id=server_id,
            name=original_name,
            qualified_name=qualified,
            description=raw_tool.get("description", ""),
            input_schema=raw_tool.get("inputSchema", {"type": "object", "properties": {}}),
            raw=raw_tool,
        )
        self._tools[qualified] = tool
        return tool


def _build_client(server_id: str, spec: "MCPServerSpec") -> MCPClient:
    if spec.transport == "stdio":
        transport = StdioTransport(command=spec.command, env=spec.env)
    elif spec.transport == "sse":
        transport = SSETransport(url=spec.url, headers=spec.headers)
    else:
        raise ValueError(f"Unknown MCP transport: {spec.transport}")
    return MCPClient(server_id=server_id, transport=transport)
```

#### B.4 IntentHandler — thay đổi

```python
# runtime/intent_handler.py — v6.7 additions

class IntentHandler:

    def __init__(
        self,
        ...,
        mcp_registry: "MCPRegistry | None" = None,   # NEW v6.7
    ) -> None:
        ...
        self._mcp = mcp_registry

    async def handle(self, req: IntentRequest) -> IntentResponse:
        ...
        # v6.7: build tool list = mesh_action + all MCP tools
        tools = [MESH_TOOL_SPEC]
        if self._mcp:
            tools = tools + self._mcp.get_tool_specs()

        for turn in range(max_turns):
            llm_resp = await self._llm.chat(
                messages=messages,
                system=system_prompt,
                tools=tools,          ← now includes MCP tools
                tool_choice="auto",
            )

            if llm_resp.tool_calls:
                session.add_raw(_build_assistant_tool_call_msg(llm_resp))

                for tc in llm_resp.tool_calls:
                    tool_result = await self._execute_tool_call(tc, caller_credentials)
                    ...

    async def _execute_tool_call(self, tc: ToolCall, ...) -> str:
        # v6.7: route MCP tool calls
        if self._mcp and self._mcp.is_mcp_tool(tc.name):
            logger.info("[intent] MCP tool call: %s", tc.name)
            return await self._mcp.call_tool(tc.name, tc.arguments)

        # existing mesh_action routing (unchanged)
        args = tc.arguments
        target = args.get("target_node_id", "")
        action = args.get("action", "")
        ...
```

**Key insight:** `_execute_tool_call` receives any tool call from the LLM.
Nếu tool name bắt đầu bằng `mcp__` → route qua MCPRegistry.
Nếu là `mesh_action` → route qua GatewayRouter (unchanged path).
LLM không biết sự khác biệt — nó chỉ gọi tools.

#### B.5 Config — node.yaml

```yaml
# node.yaml — v6.7 MCP configuration

node_id: analyst-cluster-A
listen: 0.0.0.0:8092

llm:
  api_key: "${ANTHROPIC_API_KEY}"
  model: claude-sonnet-4-20250514

# v6.7 — MCP servers
mcp_servers:
  - id: filesystem
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-filesystem /workspace"

  - id: github
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-github"
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"

  - id: postgres
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-postgres"
    env:
      POSTGRES_CONNECTION_STRING: "${DB_URL}"

  - id: custom-tools
    transport: sse
    url: "https://my-mcp-server.internal/mcp"
    headers:
      Authorization: "Bearer ${CUSTOM_MCP_TOKEN}"
    # reconnect_interval_seconds: 30  ← optional, for SSE resilience
```

#### B.6 NodeConfig additions

```python
# runtime/config.py additions

@dataclass(frozen=True)
class MCPServerSpec:
    """Config for one MCP server connection."""
    server_id: str          # "filesystem", "github", etc.
    transport: str          # "stdio" | "sse"
    command: str = ""       # for stdio: shell command to spawn
    url: str = ""           # for sse: base URL
    env: dict = field(default_factory=dict)        # extra env vars for subprocess
    headers: dict = field(default_factory=dict)    # HTTP headers for SSE
    enabled: bool = True
    reconnect_on_failure: bool = True
    reconnect_interval_seconds: int = 30

@dataclass(frozen=True)
class NodeConfig:
    # ... (all previous fields)
    mcp_servers: tuple = field(default_factory=tuple)  # tuple[MCPServerSpec]
```

#### B.7 server.py lifespan additions

```python
# server.py — v6.7

from runtime.mcp_client import MCPRegistry
from runtime.config import MCPServerSpec

# -- v6.7: MCP registry ---------------------------------------------------
mcp_registry: MCPRegistry | None = None
if config.mcp_servers:
    mcp_registry = MCPRegistry(server_specs=list(config.mcp_servers))
    logger.info("MCPRegistry initialised — %d server(s)", len(config.mcp_servers))

# -- intent handler (updated) --------------------------------------------
if config.llm_enabled and llm_client is not None:
    intent_handler = IntentHandler(
        ...,
        mcp_registry=mcp_registry,   # NEW
    )

# -- lifespan -----------------------------------------------------------
@asynccontextmanager
async def lifespan(app):
    if mcp_registry:
        await mcp_registry.startup()
        logger.info("MCP servers connected")
    yield
    if mcp_registry:
        await mcp_registry.shutdown()
        logger.info("MCP servers disconnected")
```

#### B.8 New endpoints — v6.7

```
GET /mcp/servers
Authorization: Bearer {auth_token}

Response 200:
{
  "servers": [
    {
      "server_id": "filesystem",
      "transport": "stdio",
      "status": "connected",
      "tools_count": 8,
      "server_name": "@modelcontextprotocol/server-filesystem",
      "server_version": "0.6.2"
    },
    {
      "server_id": "github",
      "transport": "stdio",
      "status": "connected",
      "tools_count": 12,
      "server_name": "@modelcontextprotocol/server-github"
    }
  ],
  "total_tools": 20
}

GET /mcp/tools
Authorization: Bearer {auth_token}

Response 200:
{
  "tools": [
    {
      "qualified_name": "mcp__filesystem__read_file",
      "server_id": "filesystem",
      "original_name": "read_file",
      "description": "Read complete contents of a file",
      "parameters": {"type": "object", "properties": {"path": {...}}}
    },
    ...
  ],
  "count": 20
}
```

---

### Approach C — GNOT Node as MCP Server (Roadmap)

#### C.1 Thiết kế tổng quan

GNOT node expose tất cả registered actions của mình như một MCP server.
Bất kỳ MCP-compatible client (Claude Desktop, Claude API, OpenClaw, n8n,
Cursor) đều có thể connect và dùng GNOT actions như MCP tools.

```
External MCP client (Claude Desktop, Claude API):
  mcp_servers:
    - {type: "sse", url: "http://gnot-node-0:8090/mcp"}

GNOT node exposes:
  GET  /mcp          → SSE stream (MCP transport)
  POST /mcp/message  → JSON-RPC 2.0 handler

MCP handshake:
  client → initialize
  server → {serverInfo: {name: "gnot-node-0", version: "6.7"}, capabilities: {tools: {}}}

  client → tools/list
  server → {tools: [
    {name: "execute_command", description: "...", inputSchema: {...}},
    {name: "read_file", description: "...", inputSchema: {...}},
    {name: "write_file", description: "...", inputSchema: {...}},
    {name: "llm_chat", description: "...", inputSchema: {...}},
    ... all actions in ActionRegistry
  ]}

  client → tools/call {name: "execute_command", arguments: {command: "df -h"}}
  server → {content: [{type: "text", text: "Filesystem      Size..."}]}
```

#### C.2 Tại sao Approach C là natural extension của B

Approach B: GNOT **consumes** MCP (node is a client)
Approach C: GNOT **exposes** MCP (node is a server)

Với cả hai, một GNOT cluster trở thành **fully MCP-native**:
- Agent nodes dùng MCP tools từ external servers (filesystem, GitHub, DBs)
- External agents (Claude Desktop, etc.) control GNOT nodes qua MCP
- GNOT nodes có thể chain: node-A consume tools từ node-B qua MCP

#### C.3 New components cần cho C

```python
# runtime/mcp_server.py — NEW (Approach C only)

class MCPServerHandler:
    """
    Exposes GNOT ActionRegistry as an MCP server.
    Implements MCP server protocol over SSE.

    Endpoints:
      GET  /mcp           → SSE transport (server-sent events)
      POST /mcp/message   → JSON-RPC message handler

    Converts ActionRegistry entries to MCP tool specs:
      ActionRegistry["execute_command"]
          + SchemaRegistry["execute_command"]
      → MCP tool {name, description, inputSchema}
    """

    def __init__(
        self,
        node_id: str,
        action_registry: ActionRegistry,
        schema_registry: SchemaRegistry,
        executor: ActionExecutor,
        auth_token: str | None,
    ) -> None: ...

    async def handle_initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": self._node_id, "version": "6.7"},
            "capabilities": {"tools": {}},
        }

    async def handle_tools_list(self) -> dict:
        tools = []
        for action_name, module in self._registry.items():
            schema = self._schemas.get(action_name, {})
            tools.append({
                "name": action_name,
                "description": getattr(module, "DESCRIPTION", action_name),
                "inputSchema": schema.get("params", {"type": "object"}),
            })
        return {"tools": tools}

    async def handle_tools_call(self, name: str, arguments: dict) -> dict:
        try:
            result = await self._executor.execute(name, arguments, task_id=str(uuid.uuid4()))
            if isinstance(result, SyncActionResponse):
                text = json.dumps(result.output) if isinstance(result.output, dict) else str(result.output)
                return {"content": [{"type": "text", "text": text}]}
        except KeyError:
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Action not found: {name}"}]
            }
        except Exception as e:
            return {
                "isError": True,
                "content": [{"type": "text", "text": str(e)}]
            }
```

#### C.4 Tại sao defer C sang sau B

**C1: Different urgency.** Approach B cho phép GNOT agent nodes dùng
external tools ngay lập tức. Approach C là về exposing GNOT ra ngoài —
useful nhưng không blocking hiện tại.

**C2: Schema gap.** Approach C cần `inputSchema` cho mỗi action.
Hiện tại `SchemaRegistry` có JSON schemas nhưng không phải tất cả actions
đều có schema đầy đủ. Phải complete schema coverage trước khi expose.

**C3: Auth model.** Khi GNOT node là MCP server, cần quyết định auth:
MCP client cần token gì để connect? Node's `auth_token`? Separate MCP token?
Đây là security design question cần thêm thời gian.

**C4: B enables C naturally.** Sau khi B build `MCPClient` và `MCPRegistry`,
C chỉ cần implement inverse: `MCPServerHandler`. Cùng protocol, cùng data
structures, ngược chiều. Code reuse cao.

---

## 4. So sánh ba approaches

| Dimension | A: Shallow Bridge | B: Native Injection | C: GNOT as MCP Server |
|-----------|:-----------------:|:-------------------:|:---------------------:|
| LLM thấy native tool schemas | ❌ | ✅ | N/A (server side) |
| Auto tool discovery | ❌ | ✅ | ✅ |
| Type safety per tool | ❌ | ✅ | ✅ |
| GNOT consumes MCP tools | ✅ (clunky) | ✅ (native) | ❌ |
| GNOT exposed as MCP server | ❌ | ❌ | ✅ |
| External agents control GNOT | ❌ | ❌ | ✅ |
| Architecture change | Minimal | Medium | Large |
| Implementation effort | Low (1–2 days) | Medium (3–5 days) | High (1–2 weeks) |
| Backward compat | Full | Full | Full |
| Requires new dependencies | No | No (stdlib only) | No |
| System prompt maintenance | Manual (burden) | Automatic | N/A |
| Schema gap creates hallucination | High | None | Low |
| "Fully native MCP" | ❌ | ✅ | ✅ |

---

## 5. Quyết định: B now, C later

### 5.1 Tại sao không A

Approach A giải quyết transport nhưng không giải quyết vấn đề cốt lõi.

"Native MCP support" có nghĩa: LLM tương tác với MCP tools y hệt cách nó
tương tác với bất kỳ tool nào khác — qua function calling protocol, với
full type schemas, với auto-discovery. Approach A không đạt điều này.

Với Approach A, operator phải manually maintain system prompt descriptions
cho mỗi MCP tool. Khi một MCP server update tools, operator phải update
node.yaml thủ công. Không scale. LLM sẽ hallucinate wrong arguments vì
không có type schemas.

Thêm nữa, effort để implement A đúng (MCP client, stdio management, JSON-RPC)
chỉ kém B một chút — không đáng để ship solution kém hơn.

### 5.2 Tại sao B là đúng approach

B làm đúng việc MCP được thiết kế để làm: tools được discovered
automatically, schemas được inject vào LLM context chính xác, LLM gọi
tools với full type information. Đây là "native" theo đúng nghĩa.

Approach B cũng **không thay đổi core execution model** của GNOT.
`mesh_action` vẫn là primary tool. MCP tools được thêm vào tools array
như additional tools. LLM tự chọn dùng `mesh_action` hay MCP tools tùy
theo task — không có priority conflicts.

**Backward compat hoàn toàn:** nodes không có `mcp_servers:` trong config
hoạt động y hệt v6.6. MCPRegistry không được khởi tạo. Tools array chỉ
có `mesh_action`. Zero change.

### 5.3 Approach C là roadmap tự nhiên

```
v6.7 (B): GNOT agent consumes MCP tools
  → Agent node với filesystem, GitHub, DB tools
  → Full tool discovery, native schemas

v6.8 (C): GNOT node is MCP server
  → External agents (Claude Desktop, Cursor, n8n) control GNOT nodes
  → GNOT cluster tham gia MCP ecosystem bidirectionally
  → One node's MCPClient connects to another node's MCPServerHandler
     → GNOT mesh becomes MCP-native distributed tool network
```

---

## 6. End-to-end scenario — v6.7 (Approach B)

```
═══════════════════════════════════════════════════════════
Setup: analyst-cluster-A với 3 MCP servers
═══════════════════════════════════════════════════════════

node startup:
  MCPRegistry.startup()
    → connect filesystem → 8 tools discovered
    → connect github     → 12 tools discovered
    → connect postgres   → 6 tools discovered
  Total: 20 MCP tools + 1 mesh_action tool in LLM context

═══════════════════════════════════════════════════════════
User intent: "Analyze the auth module and create a GitHub issue
             with findings, then log to DB"
═══════════════════════════════════════════════════════════

POST /intent {
  session_id: "ses-abc",
  prompt: "Analyze auth module, create GitHub issue, log to DB"
}

Turn 1 — LLM decides to read auth module:
  tool_call: mcp__filesystem__read_file
    {"path": "/workspace/src/auth/auth_module.py"}
  → MCPRegistry.call_tool("mcp__filesystem__read_file", {...})
  → StdioTransport → filesystem MCP server
  → returns: "# auth_module.py\n\ndef login(user, pwd):..."

Turn 2 — LLM analyzes and creates GitHub issue:
  tool_call: mcp__github__create_issue
    {
      "owner": "myorg",
      "repo": "backend",
      "title": "Auth module: missing rate limiting on /login",
      "body": "Found during code analysis:\n1. No rate limit...",
      "labels": ["security", "auth"]
    }
  → MCPRegistry.call_tool("mcp__github__create_issue", {...})
  → StdioTransport → github MCP server
  → returns: "Issue #142 created: https://github.com/myorg/backend/issues/142"

Turn 3 — LLM logs to database:
  tool_call: mcp__postgres__query
    {
      "sql": "INSERT INTO analysis_log (module, issue_url, created_at)
              VALUES ('auth_module', 'https://github.com/.../142', NOW())"
    }
  → MCPRegistry.call_tool("mcp__postgres__query", {...})
  → StdioTransport → postgres MCP server
  → returns: "INSERT 1"

Turn 4 — LLM also wants to run tests via GNOT mesh:
  tool_call: mesh_action
    {
      "target_node_id": "dev-cluster-A",
      "action": "execute_command",
      "params": {"command": "pytest tests/auth/ -v"}
    }
  → GatewayRouter.route() (existing path)
  → returns: "...tests pass..."

Turn 5 — LLM returns final reply:
  "Analysis complete:
   - Auth module has missing rate limiting (Issue #142 created on GitHub)
   - Analysis logged to database
   - All 23 auth tests passing"
```

LLM tự nhiên mix giữa MCP tools và mesh_action tools trong cùng một task.
Không cần special instructions.

---

## 7. Files thay đổi — v6.7

| File | Type | Description |
|------|------|-------------|
| `runtime/mcp_client.py` | NEW | MCPClient, StdioTransport, SSETransport, MCPRPCError, MCPToolError |
| `runtime/mcp_registry.py` | NEW | MCPRegistry, MCPTool, tool naming, tool spec conversion |
| `runtime/config.py` | MODIFY | MCPServerSpec dataclass; `mcp_servers` tuple trong NodeConfig; parse from yaml |
| `runtime/intent_handler.py` | MODIFY | Inject MCP tool specs vào tools array; route MCP tool calls trong `_execute_tool_call` |
| `runtime/server.py` | MODIFY | MCPRegistry init; startup/shutdown trong lifespan; `GET /mcp/servers`; `GET /mcp/tools` |
| `runtime/models.py` | MODIFY | MCPServerStatus, MCPToolInfo response models |
| `runtime/bootstrap.py` | MODIFY | MCPServerSpec trong BootstrapRequest; `_step_write_config` |

**Approach C adds (v6.8 roadmap):**

| File | Type | Description |
|------|------|-------------|
| `runtime/mcp_server.py` | NEW | MCPServerHandler, SSE transport, JSON-RPC handler |
| `runtime/server.py` | MODIFY | Add `GET /mcp`, `POST /mcp/message` endpoints |

---

## 8. Dependencies — không cần thêm package

Approach B chỉ dùng Python stdlib và existing deps:

| Need | Solution |
|------|---------|
| subprocess (stdio) | `asyncio.create_subprocess_shell` — stdlib |
| HTTP/SSE client | `httpx` — already in requirements.txt |
| JSON-RPC framing | `json` — stdlib |
| Async I/O | `asyncio` — stdlib |

**Không cần thêm bất kỳ pip package nào.** Consistent với GNOT philosophy.

Note: MCP servers themselves (filesystem, github, etc.) thường là npm packages
(`npx -y @modelcontextprotocol/server-filesystem`) — operator cần Node.js trên
host machine để run stdio MCP servers. SSE-based MCP servers không có yêu cầu này.

---

## 9. Open questions — v6.7

**Q1: MCP server restart policy**

Nếu stdio MCP server process crash (OOM, error) — MCPRegistry có nên
tự restart không? Recommendation: yes, với exponential backoff.
`reconnect_on_failure: true` (default) trong MCPServerSpec.

**Q2: Tool name collision**

Nếu hai MCP servers có cùng tool name (ví dụ: `search` trên cả `github` và `slack`):
Namespacing `mcp__github__search` và `mcp__slack__search` giải quyết hoàn toàn.
No collision possible.

**Q3: MCP tool timeouts**

MCP tools (đặc biệt DB queries, GitHub API calls) có thể slow.
`MCPServerSpec.timeout_seconds` override timeout per server.
Default: inherit từ `llm_timeout_seconds` (120s).

**Q4: MCP tool results quá lớn**

Một số MCP tools trả về large content (ví dụ: read large file).
Cần truncation strategy để không exceed LLM context window.
Recommendation: max 32KB per tool result, truncate với `[TRUNCATED...]`.

**Q5: Approach C auth model**

Khi GNOT node là MCP server:
- MCP client cần present Bearer token trong SSE connection request
- Token validated bằng `allowed_tokens` trong node config (existing auth model)
- Không cần separate MCP auth config — reuse hiện có

---

## 10. Readiness sau v6.7

```
v6.7 (Approach B):
  ✅ GNOT agent nodes consume MCP tools natively
  ✅ Tool discovery tự động (tools/list)
  ✅ Native tool schemas trong LLM tool spec
  ✅ stdio transport (subprocess MCP servers)
  ✅ SSE transport (remote MCP servers)
  ✅ LLM mix mesh_action + MCP tools naturally
  ✅ Observability: GET /mcp/servers, GET /mcp/tools
  ✅ No new dependencies
  ✅ Backward compat (opt-in via mcp_servers: in node.yaml)
  ❌ GNOT as MCP server → v6.8

v6.8 (Approach C):
  ✅ Everything in v6.7
  ✅ GNOT nodes exposed as MCP servers (SSE endpoint)
  ✅ Claude Desktop, Cursor, n8n can control GNOT nodes
  ✅ GNOT nodes chain via MCP (node-A tools → node-B as MCP server)
  ✅ Full bidirectional MCP ecosystem participation
```

**Full v6.x persistent mesh agent stack sau v6.7:**

```
v6.0: Event-driven foundation, scheduler, autonomy
v6.1: Multi-cluster topology, multi-gateway membership
v6.2: Task suspension, human-in-the-loop
v6.3: Self-provisioning clusters, ClusterOrchestrator
v6.4: Multi-participant external interaction
v6.5: Multi-channel transport (Telegram, SSE)
v6.6: Persistent sessions, long-term memory
v6.7: Native MCP tool consumption
v6.8: GNOT as MCP server (bidirectional)
```

---

*Spec: SPECS_V6.7.md | Mesh Runtime v6.7 | Repository: ai-infra-runtime-v2*
*Builds on: SPECS_V6.6.md through SPECS_V6.0.md*
*Problem statement: product owner review session, 2026-03-08*
