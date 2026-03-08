# Worklog — Execution Mesh v6.7
## MCP Native Support · Approach Analysis · Design Decisions

**Project:** ai-infra-runtime-v2
**Base:** v6.6 → v6.7
**Session:** Architecture review, 2026-03-08
**Status:** Spec complete — Approach B selected

---

## 1. Trigger

> "Hệ thống hiện tại đã hỗ trợ kết nối và gọi các action của MCP server chưa?"

---

## 2. Baseline audit

```bash
grep -rn "mcp" gnot/src/   → (no results)
cat requirements.txt        → không có mcp-related package
```

Không có một dòng MCP code nào. Clean slate.

Trước khi design, đọc toàn bộ execution pipeline:
`action_executor.py`, `action_loader.py`, `intent_handler.py`, `llm_client.py`.

**Điểm then chốt phát hiện từ code:**

`LLMClient.chat()` đã nhận `tools: list[dict]` — generic, có thể pass N tools.
`IntentHandler` hiện chỉ pass `[MESH_TOOL_SPEC]` — một tool duy nhất.
Đây là injection point tự nhiên cho MCP tool specs.

---

## 3. Ba approaches — lý do phân tích đủ ba

Có nhiều cách integrate MCP. Trước khi chọn, cần đặt ra spectrum đầy đủ:

- Approach A (minimal): wrapping
- Approach B (proper): native protocol
- Approach C (maximal): bidirectional

Phân tích ba để thấy tradeoff rõ, không chỉ chọn "approach đúng" mà còn
giải thích tại sao hai approach kia không đủ hoặc cần defer.

---

## 4. Tại sao Approach A bị loại

### 4.1 Vấn đề cốt lõi: LLM mù về tool schemas

Approach A wrap MCP call vào một GNOT action `mcp_call`.
LLM chỉ thấy:

```json
{"name": "mesh_action", "parameters": {"action": "string", "params": "object"}}
```

`params` là untyped blob. LLM không biết `read_file` cần `path: string`,
không biết `create_issue` cần `owner`, `repo`, `title` và optional `labels: array`.

Hệ quả: LLM phải đoán params từ system prompt text descriptions. Đây là
fragile — LLM hallucinate wrong argument names, wrong types.

So sánh với Approach B — LLM thấy:
```json
{"name": "mcp__filesystem__read_file",
 "parameters": {"path": {"type": "string", "description": "Absolute path"}}}
```

Với full schemas, LLM biết chính xác cần truyền gì. Zero guesswork.

### 4.2 System prompt maintenance burden không scale

Với Approach A, operator phải viết và maintain tool descriptions trong
`intent_system_prompt`:

```yaml
intent_system_prompt: |
  MCP tools available:
  - filesystem.read_file(path: string) → read file
  - github.create_issue(owner, repo, title, body, labels?) → create issue
  ...
```

Khi MCP server update (thêm tool, đổi parameter), operator phải update yaml.
Không có auto-discovery. Không scale với nhiều MCP servers hay nhiều tools.

### 4.3 Effort ratio không justify

Implement Approach A đúng cách (MCP client, stdio process management, JSON-RPC
framing, error handling) tốn ~70% effort của Approach B. Với 70% effort
mà chỉ đạt 40% quality — không hợp lý.

---

## 5. Tại sao Approach B được chọn

### 5.1 "Native" có nghĩa gì

"Native MCP support" = LLM tương tác với MCP tools y hệt cách Anthropic API
expose MCP tools — tool specs trong `tools` array của completions request,
với full JSON Schema, được discovered tự động.

Approach B đạt điều này hoàn toàn.

### 5.2 Không thay đổi core execution model

`mesh_action` vẫn là primary tool, vẫn chạy qua GatewayRouter. MCP tools
được thêm vào `tools` array như additional tools. Routing logic đơn giản:
```python
if tool_name.startswith("mcp__"):
    → MCPRegistry.call_tool()
else:
    → existing mesh_action path
```

Không có priority conflict. Không có breaking change. LLM tự chọn tool phù hợp.

### 5.3 Tool naming convention — tại sao `mcp__server__tool`

Options:
- `read_file` — collision risk (nếu GNOT cũng có action tên `read_file`)
- `filesystem.read_file` — dot notation không valid trong OpenAI function name spec
- `mcp_filesystem_read_file` — dễ confuse với existing actions
- `mcp__filesystem__read_file` — double underscore namespace, unmistakable

Double underscore (`__`) là convention phổ biến trong Python cho namespacing
và không conflict với OpenAI function name validation (alphanumeric + underscore).

Parsing ngược lại trivial: `name.split("__", 2)` → `["mcp", "filesystem", "read_file"]`.

### 5.4 Không cần dependencies mới

stdlib `asyncio.create_subprocess_shell` xử lý stdio transport.
`httpx` (đã có trong requirements.txt) xử lý SSE transport.
`json` stdlib cho JSON-RPC framing.

Consistent với GNOT philosophy: no unnecessary external dependencies.

---

## 6. Tại sao Approach C được defer

### 6.1 Different urgency

B cho phép agent nodes consume external tools ngay. C expose GNOT ra ngoài —
valuable nhưng không blocking cho current use cases.

### 6.2 Schema coverage prerequisite

C cần full `inputSchema` cho mỗi action. Hiện tại `SchemaRegistry` có schemas
nhưng không phải tất cả actions đều có đầy đủ. Phải audit và complete schema
coverage trước khi expose ra ngoài như MCP server.

### 6.3 Auth design question

Khi GNOT node là MCP server, MCP clients cần authenticate. Cần quyết định:
dùng `allowed_tokens` (existing)? Separate MCP tokens? Per-client isolation?
Đây là security decision cần thêm analysis.

### 6.4 B enables C naturally

`MCPClient` và `MCPRegistry` được build trong B là inversely related với
`MCPServerHandler` trong C. Cùng protocol, cùng data structures, ngược chiều.
Code reuse rất cao. Build B trước là prerequisite tự nhiên.

### 6.5 Roadmap value rõ ràng

```
v6.7 (B): GNOT consumes MCP → agent node dùng external tools
v6.8 (C): GNOT is MCP      → external agents control GNOT nodes

Combined: một GNOT cluster là fully MCP-native distributed tool network.
  Node-A consume MCP tools từ node-B (node-B là MCP server).
  Claude Desktop control node-0 qua MCP.
  n8n orchestrate GNOT cluster qua MCP.
```

---

## 7. Key design decisions

### 7.1 StdioTransport — asyncio subprocess, không threading

MCP stdio là line-buffered I/O. Options:
- `subprocess.Popen` với threads → simpler nhưng blocking
- `asyncio.create_subprocess_shell` → non-blocking, consistent với GNOT async model

Chọn asyncio. GNOT là fully async — blocking I/O trong subprocess calls
sẽ starve event loop cho concurrent sessions.

### 7.2 SSETransport — simple POST/GET, không persistent SSE stream

MCP SSE protocol: client GETs SSE stream từ server, POSTs messages vào server.
Nhưng cho GNOT-as-MCP-client use case, cần đơn giản hơn:
- POST `{url}/message` với JSON-RPC request
- Server trả về response trong POST body (synchronous)
- SSE stream chỉ cần cho server-push (notifications) — not required cho basic tool calls

v6.7 dùng simple POST-response. SSE push notifications defer sang v6.8 nếu cần.

### 7.3 MCPRegistry.startup() là non-fatal

Nếu một MCP server fail to connect tại startup (npm not installed, network issue):
- Log error
- Skip server
- Node continues với remaining MCP servers + mesh_action

Không crash node vì một MCP server unavailable. Operator sẽ thấy error trong logs
và trong `GET /mcp/servers` (status: "failed").

### 7.4 Tool result truncation

MCP tools có thể trả về very large content (đọc large file, GitHub issue với nhiều comments).
Context window của LLM là finite. Default: truncate ở 32KB với `[TRUNCATED - {N} bytes omitted]`.

---

## 8. Implementation notes

### 8.1 Request ID management trong MCPClient

JSON-RPC 2.0 cần unique request IDs để match request/response.
`MCPClient._request_id` là simple counter per-client — đủ vì:
- Mỗi MCPClient là single server connection
- StdioTransport dùng asyncio.Lock → serial requests, không parallel
- Counter không cần global uniqueness

### 8.2 Process lifecycle — stdio servers

`StdioTransport` spawn subprocess khi `start()`. Subprocess cần:
- Tắt khi GNOT node tắt (`shutdown()` → `terminate()` + `wait()`)
- Restart nếu crash (`reconnect_on_failure: true`)

Node lifespan handler đảm bảo clean shutdown: `mcp_registry.shutdown()` trong lifespan exit.

### 8.3 IntentHandler tool routing — không cần modify LLMClient

`LLMClient.chat()` đã nhận arbitrary `tools: list[dict]`. Chỉ cần:
1. `IntentHandler` build extended tools array
2. `IntentHandler._execute_tool_call()` check tool name prefix để route

`LLMClient` hoàn toàn unchanged — nó không biết gì về MCP.

---

## 9. Summary

```
Baseline: Zero MCP support. grep returns no results.

Three approaches analyzed:
  A (Shallow Bridge): wrap MCP in action module
    → LLM stays blind to tool schemas
    → Manual system prompt maintenance
    → Not truly "native" — rejected

  B (Native Injection): MCPClient + tool spec injection ← SELECTED
    → Full tool discovery at startup
    → Native schemas in LLM tools array
    → Auto-routing by tool name prefix
    → No new dependencies
    → Backward compat (opt-in)

  C (GNOT as MCP Server): expose GNOT actions as MCP server
    → Valuable but deferred
    → Requires schema coverage audit + auth design
    → B is natural prerequisite
    → Roadmapped as v6.8

Decision: Ship B as v6.7. Roadmap C as v6.8.
Rationale: B is minimal-change, maximum-native approach.
           C follows naturally from B's infrastructure.
```

---

*Worklog: WORKLOG_V6.7.md | Mesh Runtime v6.7 | Repository: ai-infra-runtime-v2*
*Previous: WORKLOG_V6.6.md*
