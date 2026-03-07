# Worklog — Execution Mesh v5.9 (Intent / Agent Loop)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-05
**Base version:** v5.8 → v5.9

---

## 1. Vấn đề / Context

Đến v5.8, hệ thống chỉ có một entry point: Claude Web agent thủ công gọi `/action` từng bước.
Khi user muốn dùng Telegram (hoặc bất kỳ channel khác), không còn Claude Web nữa — cần một
đường vào mới mà user chỉ cần nói "backup database X rồi restore trên Y" mà không cần
biết gì về mesh action API.

Yêu cầu: `POST /intent` — nhận free-form text, tự orchestrate mesh, trả về câu trả lời.

---

## 2. Phân tích Alternative

### Vấn đề: Cơ chế agent reasoning

**Option A — Text-based ReAct (chain-of-thought + regex):**
LLM output text như `ACTION: execute_command, TARGET: node-1`. Server parse bằng regex.

Nhược điểm: fragile, không reliable, dễ hallucinate format sai.

**Option B — OpenAI function calling / tool use (được chọn):**
LLM trả structured `tool_calls` JSON. Server parse chính xác bằng `json.loads`. Loop lại
với `role: "tool"` message chứa kết quả.

Ưu điểm: chuẩn công nghiệp. Hoạt động với GPT-4o, Claude 3 (via LiteLLM), Mistral, Llama 3.1+.
`LLMClient` đã dùng OpenAI-compatible format — chỉ cần thêm `tools` param.

### Vấn đề: Tool spec design

**Sub-option i — N×M tools** (một tool cho mỗi action × node):
`execute_command_on_node_centos`, `read_file_on_node_gateway`... → bùng nổ tool list, waste context.

**Sub-option ii — Generic `mesh_action` tool (được chọn):**
```json
{"name": "mesh_action", "parameters": {"target_node_id", "action", "params"}}
```
Node list + action list được describe trong system prompt. Gọn, dễ extend, consistent.

### Vấn đề: Session state storage

**Option A — Redis/DB:**
Persistent across restarts. Overhead lớn.

**Option B — In-memory + lazy TTL (được chọn):**
Consistent với v5.7 (node staleness) và v5.8 (upload TTL). Session loss on restart chấp nhận được
vì: (1) sessions thường ngắn; (2) user có thể send lại prompt; (3) không cần infra mới.

---

## 3. Chi tiết Implementation

### 3.1 LLMClient (`runtime/llm_client.py`) — Modified

Thêm:
- `ToolCall` dataclass: `{id, name, arguments: dict}`
- `LLMResponse.tool_calls: list[ToolCall]` — populated khi `finish_reason == "tool_calls"`
- `chat(tools=..., tool_choice=...)` — forward trực tiếp sang OpenAI tool spec format
- Parse `message.tool_calls` từ response, `json.loads(arguments)` tự động

Backward compat: caller không pass `tools` → `tool_calls` trả về `[]` như cũ.

### 3.2 ConversationStore (`runtime/conversation_store.py`) — New

Pattern giống UploadManager (v5.8): lazy TTL, asyncio.Lock, sweep on startup.

Key design:
- `Session.messages` là raw `list[dict]` (OpenAI format) → pass thẳng vào `LLMClient.chat()`
  không cần conversion nào.
- `session_id` là caller-supplied (Telegram: `chat_id`, CLI: tự gen). Điều này cho phép
  Telegram bot đơn giản dùng `chat_id` làm session_id mà không cần quản lý mapping.
- `Session.add_raw()` cho phép append assistant message với `tool_calls` array — cần thiết
  vì OpenAI format yêu cầu assistant message phải có tool_calls trước khi có tool result.

### 3.3 IntentHandler (`runtime/intent_handler.py`) — New

Core ReAct loop:

```
User prompt → append to session.messages
  loop (max_turns):
    LLM call (messages + MESH_TOOL_SPEC)
    if tool_calls:
      append assistant message (with tool_calls) to session
      for each tool_call:
        route() via GatewayRouter
        if AsyncActionResponse: poll with exponential backoff
        append tool result message to session
      continue loop
    else:
      reply = llm_resp.content
      append to session
      break
  return IntentResponse
```

System prompt xây động từ live state:
- `node_registry.get_all_statuses()` → node list
- `action_registry.keys()` → action list
- Thêm `config.intent_system_prompt` override nếu operator muốn custom

Async job polling: exponential backoff `[1, 2, 4, 5, 5, 5...]` giây, timeout = `config.pull_job_timeout_seconds`.

Tại sao `temperature=0.2` cho tool use: deterministic reasoning quan trọng hơn creativity.

### 3.4 NodeRegistry — Modified

Thêm `get_all_statuses() -> dict[str, str]` — sync snapshot (không cần asyncio.Lock vì
CPython GIL đảm bảo dict iteration safety cho read-only).

### 3.5 Server (`runtime/server.py`) — Modified

4 endpoints mới:
- `POST /intent` — main entry point
- `GET  /sessions/{id}` — xem history
- `DELETE /sessions/{id}` — reset (Telegram /reset command)
- `GET  /sessions` — list all active sessions (admin/debug)

IntentHandler chỉ khởi tạo nếu `config.llm_enabled`. Nếu không có LLM config → 503 với
message hướng dẫn setup.

---

## 4. Files thay đổi

| File | Loại | Thay đổi chính |
|------|------|---------------|
| `runtime/llm_client.py` | Modified | `ToolCall` dataclass; `tools/tool_choice` params; `tool_calls` parsing |
| `runtime/conversation_store.py` | **New** | Session state: get_or_create/get/delete/list/sweep, lazy TTL |
| `runtime/intent_handler.py` | **New** | ReAct agent loop, mesh_action tool spec, dynamic system prompt |
| `runtime/node_registry.py` | Modified | `get_all_statuses()` sync snapshot method |
| `runtime/models.py` | Modified | `IntentRequest`, `IntentResponse`, `ConversationMessage`, `SessionInfo` |
| `runtime/config.py` | Modified | `intent_max_turns`, `session_ttl_seconds`, `intent_system_prompt` |
| `runtime/server.py` | Modified | 4 endpoints mới; ConversationStore/IntentHandler init; lifespan sweep; v5.9.0 |
| `tests/test_v59_features.py` | **New** | 21 tests |
| `docs/WORKLOG_V5.9.md` | **New** | Document này |
| `docs/SPECS_V5.9.md` | **New** | Spec v5.9 |

---

## 5. Test Suite

| Class | Tests |
|-------|-------|
| TestConversationStore | 9 |
| TestSessionModel | 2 |
| TestIntentHandler | 5 |
| TestIntentEndpoint | 2 |
| TestSessionEndpoints | 3 |
| **Tổng mới** | **21** |

**v5.8: 71 tests → v5.9: 92 tests (+21) — 92/92 pass**

---

## 6. Config mới (node.yaml)

```yaml
# v5.9 — Intent settings (optional, defaults shown)
intent_max_turns: 10           # max agent loop steps per prompt
session_ttl_seconds: 3600      # session expires after 1h of inactivity

# Custom system prompt (optional)
# intent_system_prompt: |
#   You are a helpful DevOps assistant for Company X...
```

---

## 7. Telegram Bot integration (ví dụ minimal)

```python
# telegram_bot.py — minimal example
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters

GATEWAY = "http://203.x.x.x:8080"
TOKEN = "mesh-secret-token"

async def handle_message(update, context):
    chat_id = str(update.message.chat_id)
    text = update.message.text

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{GATEWAY}/intent",
            json={"prompt": text, "session_id": chat_id},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    data = resp.json()
    await update.message.reply_text(data["reply"])

app = ApplicationBuilder().token("TELEGRAM_BOT_TOKEN").build()
app.add_handler(MessageHandler(filters.TEXT, handle_message))
app.run_polling()
```

---

## 8. Remaining Items (cập nhật)

| Priority | Item | Status | Ghi chú |
|----------|------|--------|---------|
| P2 | Lazy node staleness check | ✅ Done (v5.7) | |
| P3 | Pull job timeout | ✅ Done (v5.7) | |
| P3 | FastAPI lifespan migration | ✅ Done (v5.7) | |
| P3 | Queue depth in /health | ✅ Done (v5.7) | |
| P3 | File upload/download | ✅ Done (v5.8) | |
| P3 | **Intent / agent loop** | ✅ Done (v5.9) | |
| P2 | Container isolation | ⏸ Deferred | non-root + ulimit |
| P3 | Job queue persistence | ⏸ Deferred | |
| P4 | Idempotency key | ⏸ Deferred | |
| P3 | Upload chunked/resumable | ⏸ Deferred | |
| P3 | File ACL | ⏸ Deferred | |
| P3 | **Session persistence** | 🆕 New | SQLite; sessions survive restart |
| P3 | **Streaming intent response** | 🆕 New | SSE stream từng step về Telegram |
| P4 | **Telegram bot example** | 🆕 New | Minimal reference implementation |
