# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v6.2
### Task Lifecycle · Suspension & Resumption · Human-in-the-Loop · Pro Dev Team Model

**Base version:** v6.1
**Target version:** v6.2
**Status:** Analysis complete — design pending
**Authors:** Architecture review session, 2026-03-08

---

## 1. Bài toán gốc

### 1.1 Yêu cầu từ product owner

> "Em hãy review xem với kiến trúc mới này chúng ta có thể xây dựng nhiều AI dev
> team như các pro real team chưa nhé, các thành viên có thể trao đổi thông tin
> với nhau, khi một task nào chưa rõ thì developer có thể hỏi manager và ngưng
> lại task đó và làm task khác; khi có câu trả lời từ manager rồi thì developer
> đó mới làm tiếp; manager nếu thiếu thông tin để trả lời thì lại có thể hỏi
> agent khác hoặc hỏi user."

Ba behaviors cụ thể được yêu cầu:

**Behavior 1 — Collaborative communication:**
Các thành viên trong team (và cross-team) có thể trao đổi thông tin tự do.

**Behavior 2 — Intelligent task suspension:**
Developer gặp ambiguity → hỏi manager → dừng task đó → chuyển sang task khác.
Khi có answer → tiếp tục task cũ từ chỗ dừng.

**Behavior 3 — Escalation chain:**
Manager không biết → hỏi agent khác hoặc hỏi user → answer propagate ngược về.
Chain có thể nhiều levels: dev → manager → senior → user.

### 1.2 Tại sao bài toán này quan trọng

Đây không chỉ là feature request — đây là test case để xác định liệu GNOT có thể
model được **cognitive work patterns** của một real professional team hay không.

Trong một pro dev team thực tế:
- Không ai block chờ answer trước khi làm việc khác
- Clarification flow là multi-hop (dev không nhất thiết hỏi thẳng senior)
- Context được preserve khi resume ("ah đúng rồi, câu hỏi của tôi là về edge case X")
- Timeout có xử lý ("nếu 24h không ai trả lời thì tôi sẽ dùng assumption mặc định")

---

## 2. Baseline: Những gì v6.1 đã có

### 2.1 Communication infrastructure (✅ đủ)

```
Multi-team topology:
  - Nhiều gateways độc lập, mỗi gateway = một team channel
  - Node register với nhiều gateways = tham gia nhiều teams
  - Full member equality — không phân biệt native/cross-team member

Event-driven messaging:
  - EventBus: emit / subscribe / deliver
  - Scoped delivery: chỉ members của gateway nhận events
  - Wildcard subscriptions: "test.*", "*.failed", "*"
  - correlation_id: liên kết events trong cùng workflow
  - Event log: audit trail, replay

Reactive agents:
  - Scheduler level-3: event-triggered action
  - Scheduler level-1: self-check condition
  - Scheduler level-2: adaptive polling
```

**Kết luận Behavior 1:** Đã cover. Members có thể emit và receive events tự do.
Cross-team communication qua multi-gateway membership.

### 2.2 Task execution infrastructure (⚠️ partial)

```
Job lifecycle hiện tại:
  ACCEPTED → QUEUED → RUNNING → COMPLETED | FAILED

IntentHandler (ReAct loop):
  - Nhận prompt → call LLM → execute tool → loop → return reply
  - Session state: ConversationStore (in-memory, per session_id)
  - Tool: mesh_action(target_node_id, action, params)

WorkerAgent:
  - Poll jobs từ gateway queue
  - Execute via ActionExecutor
  - Report result về gateway
```

**Gap rõ ràng:** Không có SUSPENDED status. Không có task parking. Không có resume.

### 2.3 Human interface (❌ thiếu hoàn toàn)

Không có mechanism nào để:
- Agent gửi câu hỏi đến user và đợi answer
- User nhận notification về pending questions
- Answer từ user được route về đúng agent đang đợi

---

## 3. Gap analysis chi tiết — Behavior 2

### 3.1 Flow đầy đủ cần support

```
t=0:  gateway-A assign task X cho dev-A
      dev-A bắt đầu: IntentHandler.handle(task_X)
      dev-A call llm_chat → LLM phân tích → bắt đầu code

t=1:  dev-A gặp requirement mơ hồ:
      "Khi user là null thì phải throw exception hay return empty?"
      
t=2:  dev-A cần:
      (a) Formulate câu hỏi rõ ràng
      (b) Identify ai có thể trả lời (manager-A)
      (c) Emit clarification request
      (d) DỪNG task X — preserve toàn bộ state
      (e) Signal gateway: "tôi available cho task khác"
      
t=3:  gateway-A thấy dev-A available → assign task Y
      dev-A bắt đầu task Y bình thường

t=4:  manager-A nhận clarification request
      manager-A consult → có answer
      manager-A emit answer với correlation_id của câu hỏi

t=5:  dev-A nhận answer
      dev-A RESUME task X:
      (a) Load saved state (messages, context, progress)
      (b) Inject answer vào context
      (c) Tiếp tục từ chỗ dừng
      
t=6:  dev-A complete task X
      dev-A đang làm task Y (có thể song song hoặc tuần tự)
```

### 3.2 Tại sao IntentHandler hiện tại không support điều này

```python
# intent_handler.py — linear coroutine, không thể suspend

async def handle(self, request: IntentRequest) -> IntentResponse:
    session = await self._store.get_or_create(request.session_id)
    
    for turn in range(self._max_turns):
        # ← Tất cả state nằm trong local variables của coroutine này
        response = await self._llm.chat(session.messages)
        
        if response.tool_calls:
            result = await self._execute_tool(response.tool_calls[0])
            session.add_raw(result)
            continue
        
        # ← Khi return, toàn bộ call stack bị GC
        # ← session.messages được save vào ConversationStore
        # ← nhưng "đang ở turn nào", "đang làm task gì" → mất
        return IntentResponse(reply=response.text)
```

**Vấn đề 1: ConversationStore chỉ lưu messages**

Session trong ConversationStore là list of messages (OpenAI format). Đây là
conversation history, không phải task execution state. Không có:
- `task_id` — đang làm task gì
- `task_status` — RUNNING / SUSPENDED
- `suspension_reason` — tại sao dừng
- `pending_question_id` — đang đợi answer nào
- `resume_at_turn` — resume từ turn nào

**Vấn đề 2: Không có SUSPENDED job status**

`JobStatus` enum: ACCEPTED, QUEUED, RUNNING, COMPLETED, FAILED.

Khi dev-A suspend task X, job vẫn ở RUNNING. Gateway không biết dev-A đang bị block.
Gateway sẽ không assign job mới vì dev-A "đang bận".

**Vấn đề 3: Không có task pool / concurrent execution**

Node execute một job tại một thời điểm. Không có concept "job đang suspended,
tôi đang chạy job khác song song".

**Vấn đề 4: Answer routing không có địa chỉ**

Khi answer đến (dưới dạng event), không biết route nó về đâu. dev-A đang chạy
task Y — IntentHandler của task Y không biết có answer cho task X đến.

### 3.3 Missing primitives — Behavior 2

| Primitive | Mô tả | Gap level |
|-----------|-------|-----------|
| `JobStatus.SUSPENDED` | Job bị block, available cho jobs khác | Critical |
| `CheckpointStore` | Lưu full task state (messages + metadata) | Critical |
| `TaskPool` | Quản lý nhiều concurrent tasks (active + suspended) | Critical |
| Task resume trigger | Khi answer đến → resume đúng task | Critical |
| `correlation_id` routing | Map answer event về đúng suspended task | Important |
| Suspension timeout | Nếu không có answer trong N giờ → action fallback | Important |

---

## 4. Gap analysis chi tiết — Behavior 3

### 4.1 Flow đầy đủ cần support

```
dev-A → [clarification.needed, question="Null user behavior?"]
                    ↓
         manager-A nhận (subscribed)
         manager-A không biết chắc
         manager-A có 3 lựa chọn:
           (a) Hỏi senior-dev-A (agent khác trong team)
           (b) Hỏi user trực tiếp  
           (c) Dùng judgment của LLM tự trả lời
                    ↓
         Nếu (a): manager-A emit [clarification.needed, to=senior-dev-A]
                  senior-dev-A nhận → answer → emit [clarification.answered]
                  manager-A nhận → forward về dev-A với correlation chain
                    ↓
         Nếu (b): manager-A emit [participant.input_required]
                  external-adapter nhận → notify user
                  user answer → emit [participant.answered]
                  manager-A nhận → forward về dev-A
                    ↓
         Nếu (c): manager-A call llm_chat → generate answer
                  manager-A emit [clarification.answered]
                  dev-A nhận → resume task X
```

### 4.2 Những gì đã có

**EventBus re-emit pattern (✅ available):**
Manager-A có thể emit `clarification.needed` với `source_node: manager-A`
và `correlation_id: <original_question_id>`. Senior-dev-A subscribed →
nhận → answer → emit `clarification.answered` với same `correlation_id`.
Manager-A subscribed → forward về dev-A.

Chain này **hoạt động với EventBus hiện tại**. Không cần primitive mới.

**`correlation_id` (✅ đã thiết kế trong v6.0):**
Event có `correlation_id` field — cho phép chain events lại với nhau.
Dev-A khi nhận answer có thể verify: "đây là answer cho question của tôi."

### 4.3 Những gì chưa có — Human interface

Khi manager-A emit `participant.input_required`, cần:
1. Một node subscribe event này và forward đến user
2. Mechanism để user reply
3. Reply được inject lại vào agent network

**Gap: Không có ExternalAdapter node pattern.**

Với v6.1 EventBus, khi `participant.input_required` được emitted:
- Event được deliver đến tất cả subscribers
- Nhưng không có subscriber nào là "bridge đến real human"
- Không có `/intent` session tương ứng để receive human reply

### 4.4 Human interface options

**Option A: Polling-based (simplest)**
```
external-adapter-node expose một action: get_pending_questions()
User (hoặc Claude Web) định kỳ call action này
→ Nhận list pending questions
→ Submit answer qua POST /action answer_question(question_id, answer)
→ external-adapter-node emit participant.answered
```

Pros: đơn giản, không cần realtime. Cons: latency (polling interval).

**Option B: Webhook push**
```
external-adapter-node config webhook_url (Telegram bot, Slack, custom endpoint)
Khi nhận participant.input_required → POST đến webhook với formatted message
User reply qua Telegram/Slack → webhook handler emit participant.answered
```

Pros: realtime, familiar UX. Cons: cần external webhook endpoint.

**Option C: /intent session bridge**
```
User đang có active /intent session với external-adapter-node
Session "subscribed" đến participant.input_required events
Khi event đến → LLM trong session format và present câu hỏi cho user
User reply trong same /intent session → LLM emit participant.answered
```

Pros: natural conversation UX, không cần separate channel.
Cons: user phải actively maintain /intent session.

---

## 5. Tổng hợp: Readiness assessment

### 5.1 Behavior matrix

| Behavior | Sub-requirement | Status | Gap |
|----------|----------------|--------|-----|
| **B1: Communication** | Member emit events | ✅ | — |
| | Member receive events | ✅ | — |
| | Cross-team communication | ✅ | — |
| | Multi-hop escalation chain | ✅ | — |
| **B2: Task suspension** | Detect need to suspend | ✅ | — |
| | Emit clarification request | ✅ | — |
| | Save task state (checkpoint) | ❌ | CheckpointStore |
| | Signal availability for new task | ❌ | SUSPENDED status |
| | Accept and run new task | ✅ | — |
| | Receive answer event | ✅ | — |
| | Route answer to correct task | ❌ | correlation routing |
| | Resume task with injected context | ❌ | TaskPool.resume() |
| | Suspension timeout + fallback | ❌ | CheckpointStore TTL |
| **B3: Escalation** | Agent-to-agent escalation | ✅ | — |
| | Human question notification | ❌ | ExternalAdapter node |
| | User reply routing | ❌ | ExternalAdapter node |
| | Answer correlation across hops | ✅ | (correlation_id) |

### 5.2 Verdict

**Đạt ~65% yêu cầu với v6.1.**

- Communication infrastructure: hoàn chỉnh
- Multi-team topology: hoàn chỉnh
- Event-driven coordination: hoàn chỉnh
- Task suspension/resumption: chưa có — đây là critical gap

**Core blocker:** IntentHandler là stateless linear coroutine. Không có task
lifecycle management. Tất cả missing features (SUSPENDED status, CheckpointStore,
TaskPool, resume routing) là hệ quả của một root cause này.

---

## 6. Root cause analysis

### 6.1 IntentHandler design assumption

IntentHandler (v5.9) được thiết kế với assumption: **một /intent call = một complete
workflow**. User gửi prompt → agent loop runs to completion → trả về final reply.

Assumption này hợp lý cho:
- Simple agentic tasks: "deploy frontend", "run migration", "send notification"
- Workflows có thể complete trong một session

Assumption này **không hợp lý** cho:
- Long-running tasks có thể block ngày/giờ
- Tasks cần external input ở giữa
- Tasks cần parallel execution với other tasks

### 6.2 ConversationStore design assumption

ConversationStore (v5.9) lưu message history per session_id. Design assumption:
**session = conversation = một continuous interaction**.

Session TTL default 3600s — sau 1 giờ inactive, session expire.

Vấn đề: task X suspend vào t=0, answer đến t=4h sau. Session đã expire.
Khi resume, không có context để load.

### 6.3 JobQueue design assumption

JobQueue (v5.3) track jobs với statuses ACCEPTED/QUEUED/RUNNING/COMPLETED/FAILED.
Design assumption: **job là atomic** — một lần claim → chạy → done hoặc fail.

Không có concept "job tạm dừng, sẽ tiếp tục sau". Không có concept "job đang đợi
external input".

### 6.4 The three-layer problem

```
Layer 1: Job level (JobQueue/JobManager)
  Cần: SUSPENDED status, allow new jobs while suspended

Layer 2: Session level (ConversationStore)
  Cần: persistent checkpoint beyond TTL, task-aware (not just conversation)

Layer 3: Execution level (IntentHandler/ActionExecutor)
  Cần: pause coroutine, park state, resume from checkpoint
```

Ba layers cần thay đổi **coherently** — thay đổi một layer mà không thay đổi hai
layers kia thì không giải quyết được vấn đề.

---

## 7. Proposed solution architecture

*Phần này là proposed design — chưa được approve để implement.*

### 7.1 New primitive: CheckpointStore

```python
# runtime/checkpoint_store.py

@dataclass
class TaskCheckpoint:
    """Complete saved state of a suspended task."""

    checkpoint_id: str           # uuid4
    task_id: str                 # job_id từ JobQueue
    node_id: str                 # node đang execute task
    session_id: str              # ConversationStore session

    # Task context
    original_prompt: str         # original user/gateway request
    messages: list[dict]         # full LLM message history tại điểm suspend
    turn_count: int              # số turns đã chạy
    tool_results: list[dict]     # results đã có

    # Suspension metadata
    suspended_at: float          # unix timestamp
    suspension_reason: str       # "clarification_needed" | "human_input_required" | "dependency_blocked"
    pending_question: str        # câu hỏi cụ thể
    pending_question_id: str     # uuid, dùng làm correlation_id cho answer event

    # Answer routing
    asked_node: str              # manager-A, external-adapter, ...
    asked_at: float
    answer: str | None = None    # được fill khi answer đến
    resolved_at: float | None = None

    # Timeout handling
    timeout_seconds: int = 86400           # 24h default
    timeout_action: str = "use_assumption" # "use_assumption" | "cancel" | "escalate"
    assumption: str = ""                    # LLM-generated assumption nếu timeout

    # Status
    status: str = "suspended"    # "suspended" | "answered" | "resumed" | "timed_out"


class CheckpointStore:
    """
    Persistent store for suspended task state.

    Storage: JSONL file per node (path configurable in node.yaml).
    In-memory index for fast lookup by task_id and question_id.
    TTL enforced by background sweep (daily).

    Why file-based (not SQLite):
    - Consistent với UploadManager pattern (file-based, simple)
    - JSONL append-only = crash-safe (no partial writes)
    - Human-readable for debugging
    - Zero schema migration overhead

    Public API:
        save(checkpoint) → checkpoint_id
        get(checkpoint_id) → TaskCheckpoint | None
        get_by_question(question_id) → TaskCheckpoint | None
        get_by_task(task_id) → TaskCheckpoint | None
        update_answer(question_id, answer) → TaskCheckpoint | None
        list_suspended(node_id) → list[TaskCheckpoint]
        sweep_expired() → int
    """
```

### 7.2 New primitive: TaskPool

```python
# runtime/task_pool.py

class TaskPool:
    """
    Manages concurrent task execution with suspend/resume capability.

    Each task is an asyncio.Task wrapping an IntentHandler.handle() coroutine.
    Suspension: coroutine is NOT paused (Python doesn't support that) —
    instead, task voluntarily exits after saving checkpoint.
    Resume: new IntentHandler.handle() call with checkpoint loaded.

    States:
        ACTIVE:    coroutine running right now
        SUSPENDED: checkpoint saved, waiting for answer
        RESUMING:  answer received, new coroutine starting

    Max concurrent active tasks: configurable (default: 3 per node)
    Suspended tasks: unlimited (they don't consume CPU)
    """

    def __init__(
        self,
        max_active: int = 3,
        checkpoint_store: CheckpointStore = None,
        event_bus: EventBus = None,
    ) -> None:
        self._max_active = max_active
        self._checkpoints = checkpoint_store
        self._event_bus = event_bus
        self._active: dict[str, asyncio.Task] = {}    # task_id → Task
        self._suspended: dict[str, str] = {}           # task_id → checkpoint_id

    async def start_task(self, task_id: str, prompt: str, session_id: str) -> None:
        """Start executing a new task."""

    async def suspend_task(
        self,
        task_id: str,
        question: str,
        ask_node: str,
        timeout_seconds: int = 86400,
        timeout_action: str = "use_assumption",
    ) -> str:
        """
        Suspend a task, save checkpoint, emit clarification event.
        Returns question_id (= correlation_id for answer event).
        
        Called from WITHIN the running IntentHandler coroutine
        via a special mesh_action: suspend_and_ask(question, ask_node).
        """

    async def resume_task(self, question_id: str, answer: str) -> None:
        """
        Resume a suspended task after answer received.
        Loads checkpoint, injects answer, starts new coroutine.
        
        Triggered by: answer event subscriber in Scheduler.
        """

    async def get_status(self) -> dict:
        """Return active and suspended task counts."""
```

### 7.3 IntentHandler changes

```python
# runtime/intent_handler.py — v6.2 additions

# New special action: suspend_and_ask
# When LLM calls mesh_action(action="suspend_and_ask", params={...})
# IntentHandler saves checkpoint and exits gracefully

SUSPEND_TOOL_PARAMS = {
    "question": "The clarification question to ask",
    "ask_node": "Which node to ask (manager, external-adapter, etc.)",
    "timeout_seconds": "How long to wait before using assumption (default 86400)",
    "assumption": "What assumption to use if no answer within timeout",
}

async def _handle_suspend_request(
    self,
    task_id: str,
    session: Session,
    turn: int,
    suspend_params: dict,
) -> IntentResponse:
    """
    Called when LLM decides to suspend current task.
    
    1. Save full checkpoint (messages + turn + params)
    2. Update JobStatus to SUSPENDED
    3. Emit clarification.needed event with correlation_id
    4. Return partial IntentResponse indicating suspension
    """
    question_id = str(uuid.uuid4())

    # Save checkpoint
    checkpoint = TaskCheckpoint(
        checkpoint_id=str(uuid.uuid4()),
        task_id=task_id,
        session_id=session.session_id,
        node_id=self._config.node_id,
        original_prompt=session.messages[0]["content"],
        messages=list(session.messages),      # deep copy
        turn_count=turn,
        suspended_at=time.time(),
        suspension_reason="clarification_needed",
        pending_question=suspend_params["question"],
        pending_question_id=question_id,
        asked_node=suspend_params.get("ask_node", ""),
        asked_at=time.time(),
        timeout_seconds=suspend_params.get("timeout_seconds", 86400),
        timeout_action=suspend_params.get("timeout_action", "use_assumption"),
        assumption=suspend_params.get("assumption", ""),
    )
    await self._checkpoint_store.save(checkpoint)

    # Emit event
    await self._event_bus.emit(Event(
        event_id=str(uuid.uuid4()),
        event_type="clarification.needed",
        channel_id=self._config.node_id,   # emit on own channel
        source_node=self._config.node_id,
        correlation_id=question_id,
        payload={
            "task_id": task_id,
            "question": suspend_params["question"],
            "ask_node": suspend_params.get("ask_node"),
            "question_id": question_id,
            "context_summary": session.messages[-3:],   # last 3 messages for context
        },
    ))

    return IntentResponse(
        reply=f"[SUSPENDED] Task suspended pending clarification. question_id={question_id}",
        session_id=session.session_id,
        truncated=False,
        suspended=True,          # NEW field
        question_id=question_id, # NEW field
    )
```

### 7.4 Answer routing

```python
# How answers route back to suspended tasks

# Step 1: Scheduler subscribes to clarification.answered events
# (configured in node.yaml of every dev/worker node)
schedule:
  - trigger_type: event
    on_event_type: "clarification.answered"
    run_action: handle_clarification_answer
    description: Resume suspended task when answer arrives

# Step 2: handle_clarification_answer action
# actions/handle_clarification_answer.py

async def run(params: dict, context: dict) -> dict:
    """
    Triggered when clarification.answered event arrives.
    Finds the suspended task and resumes it.
    """
    question_id = params.get("payload", {}).get("question_id")
    answer = params.get("payload", {}).get("answer")

    if not question_id or not answer:
        return {"error": "missing question_id or answer"}

    task_pool = context.get("task_pool")
    if task_pool:
        await task_pool.resume_task(question_id=question_id, answer=answer)
        return {"status": "resumed", "question_id": question_id}

    return {"error": "no task_pool in context"}
```

### 7.5 ExternalAdapter node

```python
# ExternalAdapter node — bridge between agent network and real user

# node.yaml for external-adapter-node
node_id: external-adapter
listen: 0.0.0.0:8099

gateway_node_id: gateway-A    # primary team
additional_gateways:
  - address: https://gateway-b.vietml.com  # also watches cluster B
    auth_token: tok-hi-on-b

# Subscribe to participant.input_required from all channels
schedule:
  - trigger_type: event
    on_event_type: "participant.input_required"
    on_channel: "global"   # watch ALL gateways
    run_action: forward_to_participant
    description: Forward agent questions to human user

# actions/forward_to_participant.py
# Options:
#   A) Write to file → user reads via GET /pending-questions
#   B) Send to Telegram bot
#   C) POST to webhook
#   D) Queue for next /intent session with external-adapter

# Human submits answer → POST /action answer_human_question
# → emit participant.answered with correlation_id
# → Scheduler on originating node picks up → resume task
```

---

## 8. JobStatus extension

```python
# runtime/models.py — v6.2 addition

class JobStatus(str, Enum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    SUSPENDED = "suspended"     # NEW v6.2: blocked waiting for external input
    RESUMING = "resuming"       # NEW v6.2: answer received, about to resume
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"     # NEW v6.2: suspension timeout exceeded

# JobStatusResponse extension
class JobStatusResponse(BaseModel):
    task_id: str
    job_id: str
    status: str
    progress: int | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    # NEW v6.2 fields:
    suspended_at: float | None = None
    suspension_reason: str | None = None
    pending_question: str | None = None
    pending_question_id: str | None = None
    resume_eta: float | None = None
```

---

## 9. End-to-end flow — v6.2 complete scenario

```
Setup:
  gateway-A: cluster-A channel (frontend)
    members: pm-A, analyst-A, dev-A (home), test-A
  gateway-B: cluster-B channel (backend)
    members: pm-B, dev-B (home), test-B
  dev-A: additional_gateways → gateway-B (cross-team)
  external-adapter: additional_gateways → gateway-A, gateway-B

─────────────────────────────────────────────────────────────

t=0: PM-A emits "cluster.started" on gateway-A
     → analyst-A (subscribed) starts analysis

t=1: analyst-A completes → emits "stories.ready"
     → architect-A (subscribed) starts design
     → dev-A (subscribed) receives heads-up

t=2: architect-A proposes design → emits "design.proposed"
     → dev-A (subscribed) starts coding task X

t=3: dev-A's LLM encounters ambiguity:
     "User null handling not specified in design"
     LLM calls: mesh_action(action="suspend_and_ask", params={
         "question": "Should null user throw UserNotFoundException or return EmptyResult?",
         "ask_node": "architect-A",
         "timeout_seconds": 7200,
         "assumption": "throw UserNotFoundException if no answer in 2h"
     })
     
     IntentHandler:
     → saves checkpoint (messages[0..15], turn=8)
     → sets job SUSPENDED
     → emits "clarification.needed" on gateway-A
       {correlation_id: "q-abc123", ask_node: "architect-A"}

t=4: dev-A is now SUSPENDED on task X
     gateway-A detects SUSPENDED → marks dev-A available
     gateway-A assigns task Y to dev-A (different feature)
     dev-A starts task Y normally

t=5: architect-A receives "clarification.needed"
     (subscribed: trigger_type=event, on_event_type="clarification.needed")
     architect-A's LLM evaluates the question
     LLM not confident → decides to escalate to human
     architect-A emits "participant.input_required" on gateway-A
       {correlation_id: "q-abc123", question: "...", source: "architect-A"}

t=6: external-adapter receives "participant.input_required"
     (subscribed to all channels)
     external-adapter formats message → sends to user via Telegram:
     "architect-A asks: Should null user throw UserNotFoundException or return EmptyResult?"
     
t=7: User reads message, replies in Telegram: "throw UserNotFoundException"
     Telegram bot → POST /action to external-adapter:
       answer_human_question(question_id="q-abc123", answer="throw UserNotFoundException")
     external-adapter emits "participant.answered" on gateway-A
       {correlation_id: "q-abc123", answer: "throw UserNotFoundException"}

t=8: architect-A receives "participant.answered" (subscribed)
     architect-A emits "clarification.answered" on gateway-A
       {correlation_id: "q-abc123", answer: "throw UserNotFoundException",
        resolved_by: "human via architect-A"}

t=9: dev-A's handle_clarification_answer action fires (Scheduler event trigger)
     question_id="q-abc123" → lookup CheckpointStore → find task X checkpoint
     TaskPool.resume_task("q-abc123", "throw UserNotFoundException")
     
     Resume:
     → Load messages[0..15]
     → Inject: {role: "user", content: "Clarification received: throw UserNotFoundException"}
     → Create new IntentHandler coroutine from turn=8
     → Job status: RESUMING → RUNNING
     
t=10: dev-A continues task X with answer injected
      dev-A also has task Y running (or completed)
      dev-A completes task X → emits "code.ready"

t=11: test-A (subscribed: "code.ready") starts testing
      ...flow continues
```

---

## 10. New event types — standard vocabulary

```
# Clarification chain
clarification.needed       — agent blocked, needs answer
                             payload: {question, ask_node, question_id, context_summary}
clarification.answered     — answer ready, route to blocked agent
                             payload: {question_id, answer, resolved_by, confidence}
clarification.timeout      — no answer within timeout, using assumption
                             payload: {question_id, assumption_used}

# Human interface
participant.input_required       — question needs human judgment
                             payload: {question, choices?, timeout_seconds, context}
participant.answered             — human has replied
                             payload: {question_id, answer, resolved_by: "human"}
participant.timeout              — human did not respond in time
                             payload: {question_id, fallback_action}

# Task lifecycle
task.suspended             — task paused pending external input
                             payload: {task_id, reason, question_id}
task.resumed               — task continuing after suspension
                             payload: {task_id, question_id, answer_summary}
task.completed             — task fully done
                             payload: {task_id, summary, artifacts}
task.failed                — task failed unrecoverably
                             payload: {task_id, reason, error}
```

---

## 11. node.yaml — v6.2 additions

```yaml
# ── v6.2: Task pool config ────────────────────────────────────────────────
task_pool:
  enabled: true
  max_active_tasks: 3          # max concurrent running tasks
  # suspended tasks: unlimited

# ── v6.2: Checkpoint store config ─────────────────────────────────────────
checkpoint_store:
  enabled: true
  path: /tmp/gnot-checkpoints/ # directory for checkpoint JSONL files
  default_timeout_seconds: 86400  # 24h
  sweep_interval_seconds: 3600    # cleanup expired checkpoints hourly

# ── v6.2: Standard subscriptions for dev/worker nodes ─────────────────────
schedule:
  # Resume when answer arrives
  - trigger_type: event
    on_event_type: "clarification.answered"
    run_action: handle_clarification_answer
    description: Resume suspended task when clarification received

  # Handle timeout
  - trigger_type: event
    on_event_type: "clarification.timeout"
    run_action: handle_clarification_timeout
    description: Resume with assumption when clarification times out
```

---

## 12. Files thay đổi — v6.2

| File | Type | Description |
|------|------|-------------|
| `runtime/checkpoint_store.py` | NEW | TaskCheckpoint dataclass + file-based store |
| `runtime/task_pool.py` | NEW | TaskPool with suspend/resume/concurrent management |
| `runtime/intent_handler.py` | MODIFY | Handle suspend_and_ask tool call; checkpoint save |
| `runtime/models.py` | MODIFY | SUSPENDED/RESUMING/TIMED_OUT JobStatus; checkpoint fields on JobStatusResponse |
| `runtime/job_manager.py` | MODIFY | Support SUSPENDED status; allow new jobs for suspended node |
| `runtime/config.py` | MODIFY | task_pool + checkpoint_store config sections |
| `runtime/server.py` | MODIFY | Wire TaskPool + CheckpointStore; new endpoints |
| `seed/actions/suspend_and_ask.py` | NEW | Built-in action for LLM to call when suspending |
| `seed/actions/handle_clarification_answer.py` | NEW | Answer routing to TaskPool |
| `seed/actions/handle_clarification_timeout.py` | NEW | Timeout fallback handler |
| `docs/examples/24-ai-node-cluster-v2/README.md` | NEW | Full v6.2 dev team example |
| `docs/examples/25-autonomous-agents/README.md` | NEW | Autonomy levels walkthrough |
| `docs/worklog/SPECS_V6.2.md` | NEW | This document |
| `docs/worklog/WORKLOG_V6.2.md` | NEW | Implementation worklog |

---

## 13. New HTTP endpoints — v6.2

#### `GET /tasks`
List active and suspended tasks on this node.

```
Response 200:
{
  "active": [
    {"task_id": "task-Y", "status": "running", "started_at": ...}
  ],
  "suspended": [
    {
      "task_id": "task-X",
      "status": "suspended",
      "suspended_at": ...,
      "pending_question": "Should null user throw...",
      "asked_node": "architect-A",
      "timeout_at": ...
    }
  ]
}
```

#### `GET /tasks/{task_id}/checkpoint`
Read saved checkpoint for a suspended task.

#### `POST /tasks/{task_id}/answer`
Manually inject answer to resume a suspended task (for testing/admin).

```
Request:
{
  "question_id": "q-abc123",
  "answer": "throw UserNotFoundException"
}
```

#### `DELETE /tasks/{task_id}`
Cancel a suspended task.

---

## 14. Backward compatibility

v6.2 là additive:

| Scenario | Behavior |
|----------|----------|
| Node không có `task_pool:` config | Single-task mode, no suspension — v6.1 behavior |
| Node không có `checkpoint_store:` config | Checkpoints disabled; suspend_and_ask returns error |
| LLM không call suspend_and_ask | Normal linear execution — unchanged |
| Job queue không nhận SUSPENDED status | Backward compat: treat as RUNNING (degraded, no task switch) |
| No external-adapter node deployed | participant.input_required emitted but unhandled — timeout fires |

---

## 15. Open questions — để quyết định khi design chi tiết

**Q1: Checkpoint serialization depth**
Message history có thể rất dài (100+ messages với tool results).
Serialize toàn bộ hay chỉ summary? Full fidelity vs. storage cost.

**Q2: Max suspended tasks per node**
Unlimited suspended tasks nghĩa là storage tăng unbounded.
Cần max? Hoặc LRU eviction policy?

**Q3: Resumption concurrency**
Nếu task X suspended và task Y running, khi answer đến:
- Resume X ngay (preempt Y)?
- Queue X để chạy sau khi Y xong?
- Chạy song song X và Y?
Cần explicit config hay LLM tự quyết?

**Q4: ExternalAdapter node — built-in hay external?**
Option A: external-adapter là một node pattern (example config + actions)
Option B: external-adapter là built-in capability của mọi node
          (mọi node có thể expose /pending-questions endpoint)
Option B đơn giản hơn nhưng tạo confusion (mọi node = human bridge?)

**Q5: Clarification chain depth limit**
dev → manager → senior → user = depth 3
Nếu không giới hạn, có thể tạo infinite loop (A hỏi B, B hỏi C, C hỏi A).
Dùng `correlation_id` để detect loop? Hay explicit hop_count trên question?

**Q6: Answer confidence**
Manager trả lời với confidence thấp ("tôi đoán là...") vs. confidence cao.
Dev có nên biết confidence để quyết định suspend lại hay không?
`clarification.answered` payload nên có `confidence: float`?

---

## 16. Readiness summary

```
v6.1 covers:
  ✅ Multi-team topology
  ✅ Channel-scoped communication
  ✅ Full member equality
  ✅ Event-driven agent coordination
  ✅ Self-starting agents (all 3 autonomy levels)
  ✅ Agent-to-agent question escalation (pattern exists)
  ✅ Correlation of questions and answers

v6.2 needs to add:
  ❌ → ✅ Task suspension (CheckpointStore + SUSPENDED status)
  ❌ → ✅ Task resumption with injected context (TaskPool)
  ❌ → ✅ Concurrent task execution (active + suspended pool)
  ❌ → ✅ Human-in-the-loop bridge (ExternalAdapter node pattern)
  ❌ → ✅ Suspension timeout + fallback assumption

After v6.2: ~90% of pro real dev team behavior achievable.

Remaining 10%:
  - Implicit peer knowledge ("I know Alice forgets error handling")
  - True emergent social dynamics
  - Domain intuition for ambiguity resolution
  These are fundamental LLM limitations, not GNOT infrastructure gaps.
```

---

*Spec: SPECS_V6.2.md | Mesh Runtime v6.2 | Repository: ai-infra-runtime-v2*
*Builds on: SPECS_V6.1.md, SPECS_V6.0.md*
*Problem statement: product owner review session, 2026-03-08*
