# Worklog — Execution Mesh v6.6
## Persistent Agent · Session Persistence · Long-Term Memory · Context Management

**Project:** ai-infra-runtime-v2
**Base:** v6.5 → v6.6
**Session:** Architecture review, 2026-03-08
**Status:** Spec complete — pending implementation

---

## 1. Trigger

Product owner requirement:

> "Hệ thống có thể hỗ trợ node như là persistent agent, có llm, bộ nhớ,
> lưu context theo session, có thể clear/reset nếu cần."

---

## 2. Source code audit trước khi design

Đọc toàn bộ các files liên quan trước khi kết luận gap:

| File | Finding |
|------|---------|
| `runtime/conversation_store.py` | In-memory dict. Comment explicit: "session persistence is P4 item" |
| `runtime/config.py` | `session_ttl_seconds = 3600` default. Không có `persistent` backend option |
| `runtime/intent_handler.py` | Memory injected vào system prompt → đây là injection point |
| `runtime/server.py` | `DELETE /sessions/{id}` tồn tại. `POST /sessions/{id}/clear` không có |

Gap analysis từ code, không từ assumption.

---

## 3. Gap phân loại — priority

**P1 (Critical): Session không persist qua restart**

`ConversationStore` là `dict[str, Session]` trong RAM. Node restart = mất tất cả.
Với persistent agent làm việc trên project nhiều ngày, đây là blocker.

Code comment trong `conversation_store.py` thừa nhận đây là gap đã biết, để P4.
v6.6 nâng lên P1 vì persistent agent là yêu cầu tường minh.

**P2 (Important): Không có cross-session memory**

Session TTL 3600s. Sau 1 giờ không active → session expire → history mất.
Kể cả với session persist qua restart, nếu user quay lại ngày hôm sau (>1h),
session đã expire. Agent không còn biết gì về hôm qua.

Cần hai tầng memory:
- **Short-term**: message history trong session (đã có, chỉ cần persist)
- **Long-term**: facts & narratives xuyên suốt mọi sessions (chưa có)

**P3 (Important): TTL không phù hợp**

Default 1h là đúng cho transactional bots. Persistent agent cần TTL = 0 (infinite)
hoặc rất dài (weeks). Không có per-session TTL override.

**P4 (Nice-to-have): `clear` ≠ `delete`**

User muốn "bắt đầu chủ đề mới" nhưng giữ memory về project.
`DELETE /sessions/{id}` xóa cả session. Không có `clear messages only`.

---

## 4. Design decisions

### 4.1 Hai component mới, không sửa component cũ

Thay vì modify `ConversationStore` để thêm persistence, tạo `PersistentSessionStore`
mới — drop-in replacement.

Rationale:
- Backward compat tuyệt đối: nodes không có `session.backend: persistent` dùng
  `ConversationStore` y hệt v5.x
- `ConversationStore` vẫn là default — không break existing deployments
- `PersistentSessionStore` opt-in per node
- Separation of concerns: in-memory và file-based là hai concerns khác nhau

Tương tự: `AgentMemoryStore` là component mới hoàn toàn, không sửa `skills_file`.

### 4.2 File-based storage — tại sao không SQLite hay Redis

Lựa chọn storage cho persistent sessions:

| Option | Pros | Cons |
|--------|------|------|
| In-memory (current) | Fast, simple | Lost on restart |
| SQLite | ACID, query | Schema migration, thêm dependency |
| Redis | Fast, TTL native | External dependency, separate process |
| **Files (chosen)** | No deps, inspectable, crash-safe | Slower for large scale |

GNOT philosophy: no external dependencies beyond Python stdlib + a few pip packages.
File-based là consistent với OpenClaw approach (memory.md, sessions/) và với
GNOT's existing patterns — `CredentialStore` đã dùng file persistence.

File-based còn dễ debug: `cat sessions/ses-abc.jsonl` để xem conversation history,
`cat memory/memory_index.json` để xem toàn bộ memory state.

### 4.3 JSONL per session — tại sao không one big file

| Format | Issue |
|--------|-------|
| `sessions.json` (one big JSON) | Race conditions khi nhiều sessions concurrent write. Corrupt nếu crash mid-write |
| `sessions.jsonl` (one big JSONL) | Append-safe nhưng hard delete một session |
| **Per-session JSONL** (chosen) | Concurrent-safe, crash-safe, easy delete, easy inspect |

Per-session: `{session_id}.jsonl` cho messages + `{session_id}.meta.json` cho metadata.

Meta file tách riêng vì:
- Meta (timestamps, TTL) cần update thường xuyên (mỗi turn update `last_active`)
- Messages chỉ append — tách ra để meta update không rewrite entire messages file

### 4.4 Memory injection: system prompt, không phải messages

**Option A**: inject memory vào messages
```python
messages = [{"role": "user", "content": "[Memory]\n...\n\n" + user_prompt}]
```
Cons: memory bị truncated khi sliding window, duplicated ở mỗi turn.

**Option B**: inject vào system prompt (chosen)
```python
system_prompt = f"{base_system_prompt}\n\n{memory.recall_for_prompt()}"
```
Pros: memory luôn present ở mọi turn, không bị truncated, không inflate message count.

Memory là static context, không phải conversation content.
System prompt là đúng nơi cho static context.

### 4.5 Fact vs Narrative — tại sao hai types

**Fact**: structured, key-value, dễ lookup và update
```
project_language: "Python FastAPI"
auth_module_status: "Login done, logout pending"
```

**Narrative**: free-form text, không có natural key
```
"User prefers concise responses without markdown headers"
"Monorepo structure with separate packages per service"
```

Chỉ facts sẽ miss behavioral context.
Chỉ narratives sẽ miss structured lookup.
Cả hai cùng tồn tại, render khác nhau trong prompt.

### 4.6 Memory scope: global và session — cluster-scoped defer sang v6.7

v6.6 chỉ implement `global` và `session:{session_id}`.

Cluster-scoped memory (`cluster:{cluster_id}`) được identify trong Q3 nhưng defer:
- Cluster identity không available tại `AgentMemoryStore` init time
  (node không luôn biết cluster của mình — cluster là orchestration concern)
- Global scope + node isolation đã đủ cho v6.6 use case
- Cluster scope có thể thêm sau mà không breaking change

### 4.7 Seed actions thay vì LLM gọi HTTP trực tiếp

LLM interact với memory qua `mesh_action("self", "agent_remember", {...})`,
không gọi `POST /memory` directly.

Rationale: consistent với tất cả LLM tool calls trong GNOT.
LLM chỉ biết `mesh_action` — không cần biết HTTP endpoints, auth tokens, URLs.

"self" target: khi `target_node_id == "self"` hoặc own node_id,
`IntentHandler._execute_tool_call()` shortcuts ra khỏi GatewayRouter,
execute action locally với memory_store injected qua context dict.

### 4.8 Sliding window — full history on disk, window to LLM

`session_max_messages = 100`: last 100 messages passed to LLM.
Full history vẫn stored trên disk — không bao giờ bị xóa trừ khi explicit delete.

Memory compensates for truncated window:
agent_remember → save facts từ old context → LLM nhớ "big picture"
dù chỉ thấy recent 100 messages.

---

## 5. API design notes

`POST /sessions/{id}/clear` thay vì `PATCH`: explicit action name,
không cần request body, semantic rõ ràng — "clear messages, keep session."

`/memory` endpoints trả về flat list với metadata.
Không nest theo scope hay type — client render theo cách họ muốn.

---

## 6. What this means for GNOT identity

v6.6 chính thức biến GNOT nodes từ "stateless workers" thành "persistent agents":

```
v5.x node: nhận request → execute → trả về → forget
v6.6 node: có LLM + session history + long-term memory + skills = agent thực sự
```

Cluster của persistent agents (v6.3 + v6.6):
- Mỗi node nhớ context theo role (architect nhớ design decisions,
  analyst nhớ business requirements, dev nhớ code patterns)
- Human-in-the-loop với full context preservation (v6.4 + v6.6)
- Telegram/multi-channel participation với agent nhớ context (v6.5 + v6.6)

---

## 7. Implementation priority

| Component | Priority | Effort | Dependency |
|-----------|----------|--------|------------|
| `PersistentSessionStore` | P1 | Medium | None |
| `AgentMemoryStore` | P2 | Medium | None |
| `seed/actions/agent_remember/recall/forget` | P2 | Low | AgentMemoryStore |
| Memory injection in IntentHandler | P2 | Low | AgentMemoryStore |
| `POST /sessions/{id}/clear` | P3 | Low | PersistentSessionStore |
| `/memory` CRUD endpoints | P3 | Low | AgentMemoryStore |
| Per-session TTL in get_or_create | P3 | Low | PersistentSessionStore |
| Context window sliding window | P4 | Low | PersistentSessionStore |
| BootstrapRequest SessionSpec/MemorySpec | P4 | Low | Both |

---

## 8. Summary

```
4 gaps identified from source code audit:
  P1: ConversationStore in-memory only → sessions lost on restart
  P2: No cross-session memory → agent forgets between sessions
  P3: TTL 3600s too short for persistent agent
  P4: DELETE /sessions deletes all — no clear-only operation

Solution: two new opt-in components
  PersistentSessionStore  → file-based sessions, survives restart, TTL=0 option
  AgentMemoryStore        → writable facts + narratives, cross-session, prompt-injected

Backward compat: session.backend: memory → v5.x behavior unchanged
No new external dependencies: file-only, consistent with GNOT philosophy
```

---

*Worklog: WORKLOG_V6.6.md | Mesh Runtime v6.6 | Repository: ai-infra-runtime-v2*
*Previous: WORKLOG_V6.5.md*
