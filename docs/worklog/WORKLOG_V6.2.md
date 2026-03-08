# Worklog — Execution Mesh v6.2
## Task Lifecycle · Suspension & Resumption · Human-in-the-Loop

**Project:** ai-infra-runtime-v2
**Base:** v6.1 → v6.2
**Session:** Architecture review, 2026-03-08
**Status:** Analysis complete — awaiting design approval

---

## 1. Trigger và bài toán gốc

Product owner đặt câu hỏi:

> "Em hãy review xem với kiến trúc mới này chúng ta có thể xây dựng nhiều AI dev
> team như các pro real team chưa nhé, các thành viên có thể trao đổi thông tin
> với nhau, khi một task nào chưa rõ thì developer có thể hỏi manager và ngưng
> lại task đó và làm task khác; khi có câu trả lời từ manager rồi thì developer
> đó mới làm tiếp; manager nếu thiếu thông tin để trả lời thì lại có thể hỏi
> agent khác hoặc hỏi user."

### Parsing yêu cầu

Đọc kỹ câu hỏi, có thể extract ra 3 behaviors riêng biệt:

**B1 — Communication:** "các thành viên có thể trao đổi thông tin với nhau"
→ Đây là general communication, không chỉ trong một team.

**B2 — Task suspension với context preservation:**
"khi một task nào chưa rõ thì developer có thể hỏi manager
và ngưng lại task đó và làm task khác;
khi có câu trả lời từ manager rồi thì developer đó mới làm tiếp"

Ba sub-requirements:
- Hỏi manager (emit event)
- **Ngưng task đó** (suspend — đây là cái khó)
- **Làm task khác** (task switching)
- **Làm tiếp** khi có answer (resume với context)

**B3 — Escalation chain:**
"manager nếu thiếu thông tin để trả lời thì lại có thể hỏi agent khác hoặc hỏi user"

Multi-hop: dev → manager → (agent | user) → manager → dev.

---

## 2. Review process: re-reading architecture vs. requirements

### 2.1 Cách review

Thay vì chỉ nhớ lại specs, đọc lại full SPECS_V6.1.md trước khi analysis.
Lý do: tránh confabulation — nếu chỉ dựa vào memory có thể overestimate coverage.

Sau khi đọc xong, mapping từng requirement vào primitives hiện có:

**B1: Communication**
→ EventBus emit/subscribe: ✅ có
→ Multi-gateway membership: ✅ có (v6.1)
→ Cross-team events: ✅ có (join gateway = join channel)
Verdict: **B1 đã cover hoàn toàn.**

**B2: Task suspension**
→ Emit clarification request: ✅ EventBus
→ Suspend task: ❌ không có
→ Task switching: ❌ không có
→ Resume với context: ❌ không có
Verdict: **B2 chưa có gì về lifecycle management.**

**B3: Escalation chain**
→ Agent re-emit escalation: ✅ EventBus re-emit pattern
→ correlation_id để liên kết: ✅ đã thiết kế trong v6.0
→ Human interface bridge: ❌ không có
Verdict: **B3 một phần — agent chain có, human bridge chưa có.**

### 2.2 Finding: root cause là một thứ duy nhất

Sau khi list gaps, nhận ra tất cả thiếu sót đều xuất phát từ một điểm:

**IntentHandler là stateless linear coroutine.**

```python
async def handle(request) -> IntentResponse:
    # state sống trong stack frame của coroutine này
    for turn in range(max_turns):
        ...
    return response
    # coroutine exits → state GC'd
```

Khi coroutine return, tất cả local state (messages, turn count, context) bị garbage collected.
Không có cơ chế nào để "pause và resume" một Python coroutine từ bên ngoài.

Đây là fundamental limitation của thiết kế hiện tại, không phải thiếu feature nhỏ.

---

## 3. Design analysis — Task suspension approaches

### 3.1 Approach A: asyncio.Event (in-memory wait)

```python
# dev-A's IntentHandler
wait_event = asyncio.Event()
pending_questions[question_id] = wait_event

# Emit question
await event_bus.emit(clarification_needed_event)

# Wait for answer (non-blocking for event loop)
answer = await asyncio.wait_for(wait_event.wait(), timeout=7200)

# Resume from here
```

**Pros:**
- Đơn giản nhất
- Coroutine thực sự "paused" — không cần serialize/deserialize
- Resume tức thì khi answer đến
- Task switching: trong khi đợi, event loop có thể xử lý việc khác

**Cons:**
- In-memory only — process restart = mất tất cả pending questions
- Nếu node crash → job stuck RUNNING mãi, không bao giờ resume
- Memory leak nếu answer không bao giờ đến và timeout không được handle
- Nhiều concurrent suspended tasks = nhiều asyncio Tasks parked in memory
- Không durable — không phù hợp cho production

**Verdict:** Phù hợp cho prototype/demo. Không phù hợp cho production.

### 3.2 Approach B: Checkpoint serialization (chosen)

```python
# dev-A's IntentHandler gặp ambiguity
# LLM call: suspend_and_ask(question, ask_node)

# IntentHandler:
checkpoint = serialize_current_state(messages, turn, context)
await checkpoint_store.save(checkpoint)
await job_manager.update_status(job_id, SUSPENDED)
await event_bus.emit(clarification_needed)
return IntentResponse(suspended=True, ...)  # coroutine exits

# Later, when answer arrives:
# New IntentHandler.handle() call, loading checkpoint
checkpoint = await checkpoint_store.get_by_question(question_id)
messages = checkpoint.messages + [{"role": "user", "content": f"Answer: {answer}"}]
# Continue from turn = checkpoint.turn_count
```

**Pros:**
- Durable — survive process restart
- Explicit state — easy to inspect/debug (checkpoint là readable JSONL)
- Clean separation: job lifecycle vs. execution state
- Storage: chỉ tốn disk space, không tốn memory (suspended tasks không chiếm RAM)

**Cons:**
- Serialization overhead (messages có thể lớn)
- Resume không "tiếp tục từ giữa chừng" thực sự — bắt đầu lại từ đầu với full context
  (nhưng LLM vẫn có đủ context trong messages để "nhớ" đã làm gì)
- Cần thiết kế careful về what to serialize (không serialize tất cả — binary data, etc.)

**Verdict:** Đây là approach phù hợp với GNOT philosophy. File-based,
stateless (mỗi resume là một fresh coroutine), crash-safe.

### 3.3 Tại sao không Approach C: External state store (Redis/DB)

Redis, PostgreSQL, etc. có thể lưu checkpoint với better querying.
Nhưng đây là external dependency — vi phạm GNOT zero-external-deps philosophy.
JSONL file đủ cho use case này. SQLite là middle ground (P3).

---

## 4. Design analysis — TaskPool

### 4.1 Câu hỏi: Có cần TaskPool không?

Ban đầu consider: có thể không cần TaskPool, chỉ cần:
- CheckpointStore (lưu state)
- Khi answer đến → kick off new IntentHandler.handle() call
- WorkerAgent poll loop sẽ pick up new job naturally

**Vấn đề với approach không có TaskPool:**

```
Job state machine cần SUSPENDED status.
Khi job = SUSPENDED, gateway phải biết để assign job mới cho node.
Khi answer đến, phải "re-activate" job và resume execution.
```

Nếu không có TaskPool, ai manage state machine này? WorkerAgent không biết về
suspended jobs. JobQueue chỉ có RUNNING/COMPLETED/FAILED.

Cần một component biết:
- "Task X là suspended, đang đợi answer q-abc123"
- "Task Y đang running"
- "Khi q-abc123 có answer → resume X"

→ Đây chính là TaskPool.

### 4.2 Concurrency model của TaskPool

Một node có thể có:
- N active tasks (running concurrently via asyncio)
- M suspended tasks (checkpointed, not using CPU/memory)

N mặc định = 3 (configurable). Lý do 3 thay vì unlimited:
- Mỗi active task = một active LLM call = cost
- Quá nhiều concurrent LLM calls → rate limiting, context confusion
- 3 là heuristic: một primary task, một secondary, một fallback

M là unlimited (suspended tasks chỉ tốn disk space).

### 4.3 Resumption timing

Khi answer đến và task X cần resume, nhưng đang có 3 active tasks:
- Option A: Preempt một task (complex, risky)
- Option B: Queue resume, chạy khi slot trống
- Option C: Increase active limit temporarily

**Quyết định: Option B.** Resume được queued, chạy khi một trong N active tasks
complete. Consistent với job queue model.

---

## 5. Design analysis — Human Interface

### 5.1 Fundamental problem

Agent network là closed loop — agents talk to each other via EventBus.
Human là outside the loop — không có EventBus endpoint.

Bridge problem: làm thế nào để agent → human → agent?

### 5.2 Three delivery channels cho human

**Channel 1: Polling (GET /pending-questions)**
Human (hoặc Claude Web session) định kỳ call endpoint này.
Đơn giản nhất. Cons: latency phụ thuộc polling interval.

**Channel 2: Webhook push**
human-interface-node có `webhook_url` config.
Khi `human.input_required` → POST đến webhook.
Webhook có thể là Telegram bot, Slack, custom endpoint.
Cons: cần external webhook infrastructure.

**Channel 3: /intent session bridge**
User đang active /intent session với human-interface-node.
Events được forwarded vào session conversation.
Cons: user phải maintain active session.

### 5.3 Quyết định thiết kế

Không hard-code một channel duy nhất. human-interface-node là một **node pattern**
(example config + standard actions). Operator deploy và configure cho delivery
channel phù hợp với setup của họ.

Hai standard actions được cung cấp:
- `get_pending_questions`: polling endpoint
- `answer_question`: submit answer, triggers `human.answered` event

Delivery notification (Telegram, Slack, etc.) là operator's responsibility —
có thể implement như custom action trên human-interface-node.

### 5.4 Correlation chain qua multiple hops

Vấn đề: dev → manager → human → manager → dev.
Mỗi hop cần biết "forward answer về đâu".

Giải pháp: `correlation_id` + `reply_chain`.

```
Event: clarification.needed
  correlation_id: "q-dev-abc"
  reply_to: "dev-A"

Event: human.input_required (forwarded by manager)
  correlation_id: "q-dev-abc"   ← SAME correlation_id
  reply_to: "manager-A"
  forward_to: "dev-A"           ← original requester

Event: human.answered
  correlation_id: "q-dev-abc"   ← SAME
  answer: "throw UserNotFoundException"

Event: clarification.answered (manager forwards to dev)
  correlation_id: "q-dev-abc"   ← SAME
  answer: "throw UserNotFoundException"
  answered_by: "human via manager-A"
```

correlation_id là constant throughout the chain. Mọi event trong chain dùng
cùng correlation_id. TaskPool lookup by correlation_id khi resume.

---

## 6. What "90% of pro real team" means

Sau v6.2, analysis cho thấy ~90% achievable. Còn lại 10%:

### 6.1 Implicit peer knowledge (not achievable)

Real engineer biết: "Alice thường quên error handling, Bob code nhanh nhưng
documentation kém, Carol prefer verbose variable names."

Agents không có cross-project, cross-session memory về peer agents.
Mỗi interaction là fresh. Không có "reputation" hay "observation" về peers.

Đây là P4 item — cần persistent agent-to-agent knowledge graph.

### 6.2 Emergent social dynamics (not achievable)

Real teams có: trust building, psychological safety, informal communication,
team culture, humor. Agents chỉ communicate qua structured events/actions.

Không phải infrastructure gap — đây là fundamental LLM limitation.

### 6.3 Domain intuition (partially achievable)

Real engineer gặp ambiguous requirement → dùng domain knowledge + industry
experience để fill gaps. Agents cần `suspend_and_ask` vì không có domain context.

"Partially achievable" vì: LLM có domain knowledge, nhưng không có
project-specific context (business rules, team conventions, historical decisions).

`skills.md` (node persona) giúp một phần — operator có thể inject domain context.
Nhưng đây là static context, không adaptive.

### 6.4 True context switching (achievable với v6.2)

Với CheckpointStore + TaskPool, dev-A có thể:
- Park task X
- Work on task Y
- Resume task X later

Đây là "context switching" ở workflow level. Khác với engineer thực sự có
"global working memory" cross-tasks. Nhưng đủ functional cho use case.

---

## 7. Three-layer coherence requirement

Key insight từ analysis: không thể fix chỉ một layer.

```
Job layer (JobQueue/JobManager):
  Cần SUSPENDED status
  Cần: "node có SUSPENDED job vẫn được assign job mới"
  
Session layer (ConversationStore):
  Cần: checkpoint survival beyond TTL
  Cần: task-aware storage (not just conversation history)
  
Execution layer (IntentHandler/ActionExecutor):
  Cần: checkpoint save on suspend
  Cần: checkpoint load on resume
  Cần: inject answer into resumed context
```

Nếu chỉ fix Job layer (add SUSPENDED status) nhưng không fix Session layer
→ resume không có context → agent "forgets" what it was doing.

Nếu chỉ fix Session layer (persistent checkpoints) nhưng không fix Job layer
→ gateway vẫn mark node as "busy", không assign new jobs.

Nếu chỉ fix Execution layer (save/load checkpoint) nhưng không fix Job layer
→ no mechanism to trigger resume when answer arrives.

**Tất cả ba layers phải thay đổi cùng nhau.**

---

## 8. Comparison với v6.0 P3-d (original checkpoint proposal)

SPECS_V6.0.md P3-d đã propose `CheckpointManager` với concept tương tự.
v6.2 analysis làm rõ và extend:

| v6.0 P3-d | v6.2 |
|-----------|------|
| `CheckpointManager` | `CheckpointStore` + `TaskPool` (tách responsibilities) |
| Vague về storage | File-based JSONL (explicit) |
| Human checkpoint only | All suspension reasons (clarification, dependency, human) |
| No task switching model | Explicit TaskPool with max_active |
| No escalation chain design | Full correlation_id chain spec |
| No event vocabulary | Standard event types defined |

v6.2 là elaboration của một idea đúng đắn từ v6.0.

---

## 9. Summary: evolution của understanding

```
v6.0:  "Cần event bus, scheduler, channels"
        → Foundation đúng, nhưng channels vẫn separate từ gateway

v6.1:  "Gateway IS channel — unify"
        → Simplification đúng, ít code hơn, consistent model

v6.2:  "Communication đã OK, nhưng task lifecycle chưa có"
        → Deep gap: suspension/resumption/task-switching missing
        → Root cause: IntentHandler stateless linear design
        → Solution: CheckpointStore + TaskPool + SUSPENDED status
```

Mỗi version là một iteration của understanding sâu hơn về bài toán.
v6.0 và v6.1 không "sai" — chúng là necessary steps đến v6.2.

---

*Worklog: WORKLOG_V6.2.md | Mesh Runtime v6.2 | Repository: ai-infra-runtime-v2*
*Previous: WORKLOG_V6.1.md*
