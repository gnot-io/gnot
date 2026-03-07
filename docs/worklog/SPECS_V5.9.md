# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v5.9
### (Intent / Agent Loop — POST /intent)

---

## 1. Thay đổi so với v5.8

| Thành phần | v5.8 | v5.9 |
|-----------|------|------|
| Entry point | Chỉ `POST /action` — caller phải tự orchestrate | `POST /intent` — free-form text, hệ thống tự orchestrate |
| Session state | Không có | `ConversationStore` — multi-turn context, lazy TTL |
| LLM tool use | Không | `LLMClient` hỗ trợ `tools` + parse `tool_calls` |
| Agent loop | Không — Claude Web làm thủ công | `IntentHandler` — ReAct loop nội bộ |
| Channels | Claude Web only | Claude Web + Telegram + bất kỳ HTTP client |

---

## 2. Kiến trúc tổng quan v5.9

```
                    ┌─────────────────────────────────────────────────────┐
                    │                  Gateway Node (node-0)               │
                    │                                                       │
  Claude Web ──────▶│ POST /action    ─────────────────────────────────── │
                    │                                                       │
  Telegram Bot ────▶│ POST /intent                                         │
  Custom App ──────▶│   │                                                  │
  CLI Tool ────────▶│   ▼                                                  │
                    │ IntentHandler (ReAct Loop)                           │
                    │   │  ┌─────────────────────────────────────┐        │
                    │   │  │  1. Build system prompt (live nodes) │        │
                    │   │  │  2. LLM call (+ mesh_action tool)   │        │
                    │   │  │  3. Execute tool call via router     │        │
                    │   │  │  4. Feed result back → LLM           │        │
                    │   │  │  5. Repeat until plain reply         │        │
                    │   │  └─────────────────────────────────────┘        │
                    │   │                                                  │
                    │   ▼                                                  │
                    │ GatewayRouter ──▶ Push / Pull ──▶ Worker Nodes      │
                    │                                                       │
                    │ ConversationStore                                     │
                    │   session_id → [messages...]  (TTL 1h)               │
                    └─────────────────────────────────────────────────────┘
```

---

## 3. ReAct Loop (IntentHandler)

```
POST /intent {prompt, session_id?, node_hint?, max_turns?}
  │
  ├─ get_or_create(session_id) → Session
  ├─ session.add_message("user", prompt)
  │
  └─ loop (max_turns):
       │
       ├─ llm.chat(session.messages, tools=[MESH_TOOL_SPEC], temperature=0.2)
       │
       ├─ if tool_calls:
       │    ├─ session.add_raw(assistant_msg_with_tool_calls)
       │    ├─ for tc in tool_calls:
       │    │    ├─ router.route(ActionRequest)
       │    │    ├─ if AsyncActionResponse → poll until done
       │    │    └─ session.add_raw({role:"tool", content: result_json})
       │    └─ continue loop
       │
       └─ else (plain reply):
            ├─ session.add_message("assistant", reply)
            └─ return IntentResponse
```

**Điều kiện dừng loop:**
- LLM trả plain text (không có tool_calls) → done
- `max_turns` bị hit → `truncated=True`, trả partial reply

---

## 4. Tool Spec — `mesh_action`

```json
{
  "type": "function",
  "function": {
    "name": "mesh_action",
    "description": "Execute an action on a specific node in the execution mesh.",
    "parameters": {
      "type": "object",
      "properties": {
        "target_node_id": {"type": "string"},
        "action": {"type": "string"},
        "params": {"type": "object"}
      },
      "required": ["target_node_id", "action", "params"]
    }
  }
}
```

Ví dụ tool call do LLM generate:
```json
{
  "id": "call_abc123",
  "type": "function",
  "function": {
    "name": "mesh_action",
    "arguments": "{\"target_node_id\": \"centos-dbserver\", \"action\": \"execute_command\", \"params\": {\"command\": \"mysqldump -u root -pSECRET mydb | gzip > /tmp/backup.sql.gz\", \"timeout_seconds\": 300}}"
  }
}
```

Tool result trả về LLM:
```json
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "name": "mesh_action",
  "content": "{\"status\": \"completed\", \"output\": {\"exit_code\": 0, \"stdout\": \"\", \"stderr\": \"\"}}"
}
```

---

## 5. API Endpoints (v5.9)

### POST /intent

```json
// Request
{
  "prompt": "Backup database mydb on centos-dbserver and restore it on almalinux-restore",
  "session_id": "12345678",      // optional — Telegram chat_id
  "node_hint": "centos-dbserver", // optional — hint to agent
  "max_turns": 10                // optional — override config
}

// Response 200
{
  "session_id": "12345678",
  "reply": "Done! Backed up mydb (2.3 GB compressed to 180 MB) and restored on almalinux-restore. Verified 1,847,293 rows.",
  "turns": 6,
  "actions_taken": [
    "execute_command on centos-dbserver",
    "execute_command on centos-dbserver",
    "execute_command on almalinux-restore",
    "execute_command on almalinux-restore"
  ],
  "tokens_used": 3840,
  "truncated": false
}

// Response 503 — LLM not configured
{"error": "LLM_NOT_CONFIGURED", "detail": "Set llm_api_key in node.yaml to enable POST /intent."}
```

### GET /sessions/{session_id}

```json
{
  "session_id": "12345678",
  "created_at": 1741099200.0,
  "last_active": 1741099560.0,
  "age_seconds": 360.0,
  "turn_count": 3,
  "messages": [
    {"role": "user", "content": "Backup mydb...", "timestamp": 0.0},
    {"role": "assistant", "content": "Done!", "timestamp": 0.0}
  ]
}
```

### DELETE /sessions/{session_id}
`200 {"session_id": "...", "deleted": true}` — resets conversation context.

### GET /sessions
`200 {"sessions": [...], "total": N}` — admin/debug view.

---

## 6. Config (node.yaml)

```yaml
# LLM settings — required for /intent
llm_api_key: sk-...
llm_base_url: https://api.openai.com/v1  # or LiteLLM, Ollama, etc.
llm_default_model: gpt-4o-mini

# v5.9 — Intent / agent settings (all optional)
intent_max_turns: 10           # default: 10
session_ttl_seconds: 3600      # default: 3600 (1 hour)

# Override default system prompt (optional)
# intent_system_prompt: |
#   You are a DevOps assistant for Company X.
#   Always prefer centos-dbserver for database operations.
```

---

## 7. Multi-provider support

`LLMClient` dùng OpenAI-compatible format. Tool calling hoạt động với:

| Provider | base_url | model |
|----------|----------|-------|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o`, `gpt-4o-mini` |
| Anthropic (via LiteLLM) | `http://localhost:4000/v1` | `claude-3-5-haiku-20241022` |
| Ollama | `http://localhost:11434/v1` | `llama3.1`, `mistral-nemo` |
| vLLM | `http://localhost:8000/v1` | bất kỳ model hỗ trợ tool calling |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.1-70b-versatile` |

---

## 8. Trạng thái hiện tại (v5.9)

**Đã có:**
- ✅ Bootstrap minimal + plugin actions
- ✅ Async job model (job_id polling)
- ✅ Distributed routing (push/pull/NAT)
- ✅ Authentication (Bearer token)
- ✅ Action schema validation
- ✅ NodeRegistry + WorkerAgent + JobQueue
- ✅ Claude Web curl-native workflow (v5.6)
- ✅ Lazy staleness + pull job timeout (v5.7)
- ✅ Queue depth in /health (v5.7)
- ✅ File upload/download — POST /upload, GET /download (v5.8)
- ✅ **POST /intent — ReAct agent loop (v5.9)**
- ✅ **ConversationStore — multi-turn session state (v5.9)**
- ✅ **LLMClient tool calling — tools + tool_calls parsing (v5.9)**
- ✅ **GET/DELETE /sessions — session management (v5.9)**

**Chưa có:**
- ❌ Container isolation (P2)
- ❌ Session persistence across restarts (P3)
- ❌ Streaming intent response / SSE (P3)
- ❌ Upload chunked/resumable (P3)
- ❌ File ACL (P3)

---

*Spec: SPECS_V5.9.md | Mesh Runtime v5.9 | Repository: ai-infra-runtime-v2*
