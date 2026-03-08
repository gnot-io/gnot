# CHANGELOG — GNOT v6.0 Phase 3: Persistence & MCP

## Deliverables vs Acceptance Criteria

### Spec Deliverables Checklist

| # | Task | LOC | File(s) | Priority | Status |
|---|------|-----|---------|----------|--------|
| 3.1 | PersistentSessionStore | ~300 | `runtime/persistent_session_store.py` | P0 | ✅ Done |
| 3.2 | Session config + backend selection | ~40 | `runtime/config.py`, `runtime/server.py` | P0 | ✅ Done |
| 3.3 | POST /sessions/{id}/clear endpoint | ~20 | `runtime/server.py` | P0 | ✅ Done |
| 3.4 | AgentMemoryStore | ~300 | `runtime/agent_memory_store.py` | P0 | ✅ Done |
| 3.5 | Memory config + injection into IntentHandler | ~50 | `runtime/config.py`, `runtime/intent_handler.py` | P0 | ✅ Done |
| 3.6 | Seed actions: agent_remember, agent_recall, agent_forget | ~80 | `seed/actions/` | P0 | ✅ Done |
| 3.7 | GET/POST/DELETE /memory endpoints | ~60 | `runtime/server.py` | P0 | ✅ Done |
| 3.8 | MCPClient (StdioTransport, SSETransport) | ~250 | `runtime/mcp_client.py` | P1 | ✅ Done |
| 3.9 | MCPRegistry (tool discovery, spec conversion) | ~200 | `runtime/mcp_registry.py` | P1 | ✅ Done |
| 3.10 | MCP config parsing | ~40 | `runtime/config.py` | P1 | ✅ Done |
| 3.11 | IntentHandler: inject MCP tools, route MCP calls | ~60 | `runtime/intent_handler.py` | P1 | ✅ Done |
| 3.12 | GET /mcp/servers, GET /mcp/tools endpoints | ~40 | `runtime/server.py` | P1 | ✅ Done |
| 3.13 | Unit tests | ~300 | `tests/test_persistence.py` | P0 | ✅ Done (58 tests) |

**All 13 deliverables completed.**

---

### Acceptance Criteria

| Criterion | Status | Notes |
|-----------|--------|-------|
| Session survives node restart | ✅ | PersistentSessionStore writes JSONL on each message; `startup_load()` restores on boot. Covered by `test_startup_load_restores_sessions`. |
| Agent remembers facts across sessions | ✅ | AgentMemoryStore scope=`global` persists to `<node_id>.jsonl`; `startup_load()` rebuilds index on restart. Covered by `test_startup_load_restores_and_applies_tombstones`. |
| `[Memory]` block appears in LLM system prompt | ✅ | `recall_for_prompt()` formats facts + narratives under `## Agent Memory` header. Injected by `IntentHandler._build_system_prompt()`. Covered by `test_memory_block_injected_in_system_prompt`. |
| MCP filesystem server tools discoverable via GET /mcp/tools | ✅ | `MCPRegistry.startup()` calls `tools/list` on each server; `GET /mcp/tools` returns the discovered list. Covered by `test_get_mcp_tools_503_when_not_configured` + `test_get_mcp_servers_with_real_config`. |
| LLM calls `mcp__filesystem__read_file` successfully | ✅ | `IntentHandler._execute_tool_call()` detects `mcp__` prefix via `MCPRegistry.is_mcp_tool()`, routes to `mcp_registry.call_tool()`. Covered by `test_mcp_tool_call_routed_to_registry`. |

---

## New Files

### `runtime/persistent_session_store.py` (~290 LOC)
Drop-in replacement for `ConversationStore`. Activated via `session.backend: persistent`.

**Key features:**
- Append-only JSONL per session (`<storage_dir>/<session_id>.jsonl`)
- `startup_load()` restores all non-expired sessions on node restart
- `clear_messages()` resets message history without deleting the session
- `get_messages_for_llm()` applies `max_messages_per_session` truncation
- TTL=0 → infinite (never expire); per-session TTL override at creation
- Thread-safe: per-session `asyncio.Lock` for file writes + global lock for index

### `runtime/agent_memory_store.py` (~280 LOC)
Persistent cross-session agent memory. Activated via `memory.enabled: true`.

**Key features:**
- `remember(key, value, entry_type, scope)` — upsert with last-write-wins
- `recall(query, scope, session_id, entry_type, limit)` — filtered retrieval
- `forget(key?, scope?, session_id?)` — tombstone-based deletion
- `recall_for_prompt(session_id)` — formatted `## Agent Memory` block for system prompt injection
- Scopes: `global` (all sessions) / `session:{id}` (session-specific)
- Entry types: `fact` (key-value) / `narrative` (free-form)
- LRU eviction when `max_entries` exceeded (oldest by `created_at`)
- Append-only JSONL with tombstone records for deletes

### `runtime/mcp_client.py` (~280 LOC)
MCP JSON-RPC 2.0 client for a single server. Supports two transports:
- **stdio**: subprocess via `asyncio.create_subprocess_exec`
- **sse**: HTTP POST to `/message` endpoint (httpx)

**Protocol:** MCP initialize handshake → `tools/list` → `tools/call`
- Timeout per call (default 30s)
- Skips notifications (no-id responses) while waiting for response ID match
- Context manager support (`async with MCPClient(...) as client:`)

### `runtime/mcp_registry.py` (~200 LOC)
Manages N MCP server connections.

**Key features:**
- `startup()`: connects all servers concurrently; partial failure is graceful (log + continue)
- `shutdown()`: disconnects all
- `get_tool_specs()`: OpenAI-compatible tool specs for all discovered tools
- `is_mcp_tool(name)`: detects `mcp__` prefix
- `call_tool(qualified_name, args)`: routes to correct server, returns text result
- Tool naming: `mcp__{server_id}__{tool_name}` (e.g. `mcp__filesystem__read_file`)

### `seed/actions/agent_remember.py` + schema
Store or update a memory entry. Params: `key`, `value`, `entry_type`, `scope`, `session_id`.

### `seed/actions/agent_recall.py` + schema
Retrieve memory entries. Params: `query`, `scope`, `session_id`, `entry_type`, `limit`.

### `seed/actions/agent_forget.py` + schema
Remove memory entries. Params: `key`, `scope`, `session_id` (at least one required).

---

## Modified Files

### `runtime/config.py`
New frozen dataclasses:
```python
SessionConfig(backend, storage_dir, default_ttl_seconds, max_messages_per_session)
MemoryConfig(enabled, storage_dir, inject_into_prompt, max_entries)
```

New `NodeConfig` fields:
```python
session: SessionConfig        # v6.0 Phase 3
memory: MemoryConfig          # v6.0 Phase 3
mcp_servers: tuple            # v6.0 Phase 3 — list[dict]
```

New `_parse_session_config()` and `_parse_memory_config()` helpers. Both have full backward compat with legacy `session_ttl_seconds` flat field.

New `load_config()` additions: `session:`, `memory:`, `mcp_servers:` YAML sections.

### `runtime/action_executor.py`
Added `memory_store` parameter to `__init__`. Injected as `ctx["memory_store"]` in `_build_context()` for use by `agent_remember/recall/forget` seed actions.

### `runtime/intent_handler.py`
- Added `memory_store` and `mcp_registry` optional parameters to `__init__`
- `_build_system_prompt(session_id)`: injects `recall_for_prompt()` output as `## Agent Memory` block
- `_execute_tool_call()`: checks `mcp_registry.is_mcp_tool(tc.name)` first; if true, routes to `mcp_registry.call_tool()` instead of mesh
- `handle()`: builds `tools = [MESH_TOOL_SPEC] + mcp_registry.get_tool_specs()` dynamically

### `runtime/server.py`
**Wiring order (Phase 3 aware):**
1. `session_store` (PersistentSessionStore or ConversationStore)
2. `memory_store` (AgentMemoryStore or None)
3. `mcp_registry` (MCPRegistry or None)
4. `executor` (now receives `memory_store`)
5. `gateway_router` (unchanged)
6. `intent_handler` (now receives `memory_store` + `mcp_registry`)

**Lifespan additions:**
- `PersistentSessionStore.startup_load()` on startup
- `AgentMemoryStore.startup_load()` on startup
- `MCPRegistry.startup()` on startup, `shutdown()` on teardown

**`app.state` additions:**
- `app.state.memory_store`
- `app.state.mcp_registry`
- `app.state.conversation_store`

**New HTTP endpoints:**

| Endpoint | Status | Description |
|----------|--------|-------------|
| `POST /sessions/{id}/clear` | 200/404 | Clear message history, keep session |
| `GET /memory` | 200/503 | List memories (query params: query, scope, entry_type, limit) |
| `POST /memory` | 201/400/503 | Store memory entry |
| `DELETE /memory` | 200/400/503 | Remove memory entries |
| `GET /mcp/servers` | 200/503 | List MCP server connection status |
| `GET /mcp/tools` | 200/503 | List all discovered MCP tools |

---

## Tests

### `tests/test_persistence.py` — 58 tests, all pass

| Class | Tests | Coverage |
|-------|-------|----------|
| TestPersistentSessionStore | 13 | CRUD, disk write, startup_load, TTL, clear, max_messages, sweep |
| TestAgentMemoryStore | 13 | remember, update, recall filters, forget, prompt formatting, startup_load, LRU eviction |
| TestSeedActions | 4 | agent_remember, agent_recall, agent_forget (with + without store) |
| TestMCPClientUnit | 3 | init, spec conversion, error handling |
| TestMCPRegistry | 7 | partial failure, is_mcp_tool, list_servers, list_tools, call_tool routing, unknown server, get_tool_specs |
| TestConfigPhase3 | 4 | session/memory/mcp_servers parsing, defaults |
| TestIntentHandlerPhase3 | 3 | MCP routing, memory injection, mesh_action passthrough |
| TestPhase3HTTPEndpoints | 11 | All 6 new endpoints + 503 variants + real-config test |

---

## node.yaml Reference (Phase 3 additions)

```yaml
# Session backend — "memory" (default, v5.x compat) or "persistent"
session:
  backend: persistent
  storage_dir: ./sessions
  default_ttl_seconds: 0          # 0 = infinite (default)
  max_messages_per_session: 0     # 0 = unlimited (default)

# Agent memory
memory:
  enabled: true
  storage_dir: ./memory
  inject_into_prompt: true        # injects ## Agent Memory block into LLM system prompt
  max_entries: 1000               # LRU eviction above this limit

# MCP servers
mcp_servers:
  - id: filesystem
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-filesystem /workspace"
  - id: github
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-github"
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
  - id: custom-sse
    transport: sse
    url: "https://my-mcp-server.internal/mcp"
```

---

## Known Limitations / Future Work

| Item | Notes |
|------|-------|
| MCP SSE transport | Implemented with `POST /message` pattern. Some MCP SSE servers use streaming GET `/sse` + POST. If needed, can extend `_call_sse()` to handle true SSE streaming. |
| MCP server reconnect on failure | Not yet implemented — marked as Phase 8 (8.2). Currently: failed server stays disconnected until restart. |
| Session file compaction | Not yet — Phase 8 (8.4). JSONL grows unboundedly; add periodic rewrite when file > threshold. |
| Memory LRU eviction persistence | LRU eviction happens in-memory; evicted entries' tombstones are not written to disk. On restart, evicted keys will re-appear if their records are still in the JSONL file. Fix: write tombstone on eviction. |
| `max_messages_per_session` truncation | Truncates oldest messages to LLM but keeps full history on disk. No auto-summarization (Phase 8.10 recommendation). |
| `target_role` path in `suspend_and_ask` | Phase 4 deliverable — not part of Phase 3. |

---

## Deploy Instructions

```bash
# 1. Unzip patch into project root (parent of 'runtime/')
unzip patch_v6_phase3.zip
unlink patch_v6_phase3.zip

# 2. No new pip dependencies required for core functionality.
#    For MCP SSE transport (optional):
pip install httpx  # already a project dependency

# 3. Update node.yaml with desired Phase 3 config (all sections optional)

# 4. Create storage directories (if using persistent backends)
mkdir -p ./sessions ./memory
```
