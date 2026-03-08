# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v6.4
### Multi-Participant External Interaction · Role-Based Routing · Channel Log

**Base version:** v6.3
**Target version:** v6.4
**Status:** Analysis complete — design pending
**Authors:** Architecture review session, 2026-03-08

---

## 1. Bài toán gốc

### 1.1 Yêu cầu từ product owner

> "Team phải cho nhiều human tham gia vào để có thể trả lời những câu hỏi mà
> team có thể cần hỏi. Ví dụ: cho phép 1 human PM ở Việt Nam tham gia và 1
> human deployment engineer ở Mỹ tham gia để có thể trả lời những câu hỏi có
> liên quan. Tức có cơ chế cho nhiều human tham gia vào 1 conversation/chat session."

Sau khi phân tích sơ bộ, product owner clarify ba nguyên tắc thiết kế:

> "1. Khi agent hỏi human thì không nhất thiết phải target ông A, ông B cụ thể
>    mà chỉ cần target đến role. Human nào có role đó thì đều có thể trả lời được.
>
>  2. Không cần aware đến timezone. Chỉ cần hỏi, và nếu câu hỏi được giải đáp
>     bởi người có role tương ứng là OK.
>
>  3. 1 human tham gia có thể có nhiều role."

### 1.2 Nguyên tắc thiết kế (extracted)

Ba nguyên tắc này là **simplifications quan trọng** — chúng loại bỏ complexity
không cần thiết:

| Concern | Quyết định | Impact |
|---------|------------|--------|
| Targeting | Role-based, không phải person-based | Không cần routing logic phức tạp |
| Availability | Async-first, không cần online check | Không cần presence/timezone tracking |
| Multi-role | Một human có nhiều roles | Richer participation, simpler registration |

### 1.3 Vision đầy đủ

```
Cluster A đang chạy. Hai humans đã registered:
  - Nguyen (roles: pm, product-owner)       — Hà Nội
  - John   (roles: devops, cloud-architect)  — San Francisco

Scenario 1 — Business question:
  dev-A: "Khi user bị deactivated, nên hide data hay delete?"
  → required_role: "pm"
  → Cả Nguyen nhận thông báo (là pm)
  → Nguyen trả lời: "Hide, không delete — cần audit trail"
  → dev-A nhận answer, resume task

Scenario 2 — Infrastructure question:
  architect-A: "Nên deploy trên AWS hay GCP?"
  → required_role: "devops"
  → John nhận thông báo (là devops)
  → John trả lời: "AWS, chúng ta đã có existing contract"
  → architect-A nhận answer, resume task

Scenario 3 — Nguyen thấy câu hỏi của John và muốn add context:
  → Shared room: Nguyen xem thread → thêm comment
  → John xem comment → update answer
  → Multi-human collaboration trong cùng thread

Scenario 4 — Câu hỏi không ai có role phù hợp:
  → Timeout → agent dùng assumption (v6.2 mechanism)
  → Room log ghi nhận: "answered by assumption"
```

---

## 2. v6.2 ExternalAdapter — baseline và limitations

### 2.1 Những gì v6.2 đã thiết kế

v6.2 thiết kế `ExternalAdapter` node như một **pattern** (chưa phải spec chi tiết):

```
external-adapter-node:
  - Subscribe: participant.input_required (all channels)
  - Khi nhận: forward đến user via webhook/polling/session
  - User reply: POST /action answer_question(question_id, answer)
  - Emit: participant.answered → agent resume
```

Ba delivery options: Polling, Webhook, /intent session bridge.

`correlation_id` được dùng để route answer về đúng suspended task.

### 2.2 Limitations của v6.2 design

**Limitation 1: Single anonymous user**
v6.2 ngầm assume một user duy nhất nhận tất cả câu hỏi. Không có concept
"nhiều humans với roles khác nhau."

**Limitation 2: No participant registry**
Không có nơi lưu "ai đang tham gia team này với role gì." ExternalAdapter
không biết ai cần được notify cho câu hỏi nào.

**Limitation 3: No role-based routing**
`participant.input_required` event không có `required_role` field. ExternalAdapter
không biết câu hỏi này thuộc domain nào.

**Limitation 4: No shared conversation**
Nếu hai humans cùng nhận một câu hỏi và cùng trả lời, không có mechanism
nào handle conflicts. Không có shared view cho cả hai.

**Limitation 5: No conversation room**
Không có shared thread/room nơi tất cả humans có thể xem toàn bộ questions
và answers của team, kể cả những câu hỏi không thuộc role của mình.

---

## 3. Core model — v6.4

### 3.1 Hai concepts mới

**ExternalParticipant:** Một người thực với identity, một hoặc nhiều roles,
và một kênh để nhận notifications.

**ChannelLog:** Shared space cho một team — tất cả questions từ agents
và tất cả answers từ humans đều visible ở đây. Là "project room" của team.

### 3.2 ExternalParticipant model

```python
@dataclass
class ExternalParticipant:
    """
    A registered human participant in a team.

    One human can hold multiple roles — they receive notifications
    for questions targeting any of their roles.

    No timezone, no availability tracking — async-first by design.
    Any participant with the required role can answer at any time.
    """

    participant_id: str           # uuid4 — assigned at registration
    name: str                     # display name: "Nguyen PM", "John DevOps"
    roles: list[str]              # ["pm", "product-owner"] or ["devops", "cloud-architect"]
                                  # one human, many roles
    transport: str               # "webhook" | "polling" | "session"
    transport_target: str            # webhook URL | polling token | session_id
    auth_token: str               # token used when submitting answers (verify identity)
    cluster_id: str                  # which team this participant belongs to
    registered_at: float          # unix timestamp
    active: bool = True           # can be deactivated without removing
```

**Role taxonomy (built-in, extensible):**

```yaml
# Standard roles — agents use these when asking
built_in_roles:
  - pm                 # project management, priorities, scope
  - product-owner      # product decisions, requirements
  - devops             # deployment, infrastructure, CI/CD
  - cloud-architect    # cloud platform decisions
  - security           # security policies, compliance
  - legal              # legal/compliance questions
  - stakeholder        # general business questions
  - tech-lead          # technical decisions, architecture review
  - domain-expert      # domain-specific knowledge (extensible)
```

Roles không hard-coded — operator có thể define custom roles.
Agent và human phải dùng cùng role string để match.

### 3.3 InteractionThread model

```python
@dataclass
class InteractionThread:
    """
    One question from an agent, with all human replies.

    Visible to ALL participants in the room (transparency).
    Resolved by FIRST valid reply from a participant holding the required role.
    """

    question_id: str              # = correlation_id used in events
    cluster_id: str

    # Source
    source_agent: str             # node_id of asking agent
    source_task: str              # task_id from CheckpointStore
    created_at: float

    # Question content
    required_role: str            # "pm" | "devops" | ...
    question_text: str            # the actual question
    context_summary: list[dict]   # last N messages from agent's conversation (from v6.2)
    timeout_seconds: int          # from suspend_and_ask params
    assumption: str               # assumption if timeout

    # Status
    status: str                   # "open" | "answered" | "timed_out" | "assumed"
    resolved_at: float | None = None

    # Resolution — the answer that was used to resume the agent
    resolution: "InteractionAnswer | None" = None

    # All replies — visible to all even if only first valid one is used
    replies: list["InteractionReply"] = field(default_factory=list)

    # Participants notified
    notified: list[str] = field(default_factory=list)  # participant_ids


@dataclass
class InteractionAnswer:
    """The accepted answer that resolved the question."""
    participant_id: str
    participant_name: str
    role_used: str                # which of their roles they're answering as
    text: str
    submitted_at: float


@dataclass
class InteractionReply:
    """
    Any reply to a thread — may or may not be the resolution.

    Types:
      "answer"  — participant with required role answered → resolves question
      "comment" — participant added context without answering (any role)
      "tag"     — participant tagged another participant to look at this
      "system"  — system event (timeout, assumed, etc.)
    """
    reply_id: str
    participant_id: str | None     # None for system replies
    participant_name: str
    reply_type: str                # "answer" | "comment" | "tag" | "system"
    text: str
    submitted_at: float
    tagged_participant: str | None = None   # for "tag" type
```

### 3.4 ChannelLog model

```python
@dataclass
class ChannelLog:
    """
    Shared conversation space for a team.

    All agent questions + all human replies are visible here.
    Think: a Slack channel where AI agents post questions and humans answer.

    Persistent: survives node restarts (file-based storage).
    Append-only: questions and replies are never deleted (audit trail).
    """

    room_id: str                  # = cluster_id
    cluster_id: str
    project_name: str
    created_at: float
    participants: list[ExternalParticipant]
    threads: list[InteractionThread]  # append-only, ordered by created_at
```

---

## 4. Role-based routing

### 4.1 Routing algorithm (simple by design)

```
Agent emits: participant.input_required {required_role: "pm", question: "..."}
                    ↓
ExternalAdapter receives event
                    ↓
lookup: participants where roles contains "pm"
                    ↓
notify ALL matching participants (regardless of timezone/availability)
                    ↓
first participant with role "pm" to submit answer → resolves thread
other participants can still comment/add context after resolution
```

No load balancing. No priority. No availability check. Pure role match.

**Why first-wins?**

In real teams, if two PMs see the same question, typically:
- One answers promptly
- The other sees it's resolved and moves on
- Occasionally the second adds a clarifying comment

This is natural behavior, no special handling needed. The room shows all replies,
so if the first answer is incomplete, the second human can add context.

### 4.2 `required_role` field trên event

```python
# v6.4 addition to participant.input_required event payload

{
    "event_type": "participant.input_required",
    "correlation_id": "q-abc123",
    "payload": {
        "question_id": "q-abc123",
        "question": "Should deactivated users' data be hidden or deleted?",
        "required_role": "pm",           # NEW v6.4 — role needed to answer
        "source_agent": "dev-A",
        "source_task": "task-feature-x",
        "context_summary": [...],        # last 3 messages for context (v6.2)
        "timeout_seconds": 86400,
        "assumption": "Hide data, add is_active flag",
    }
}
```

**Agent side — `suspend_and_ask` action v6.4:**

```python
SUSPEND_TOOL_PARAMS = {
    "question": "The clarification question",
    "target_role": "Which human role to ask (pm, devops, tech-lead, etc.)",  # NEW
    # v6.2 had "ask_node" — replaced by "target_role" for human questions
    # "ask_node" still supported for agent-to-agent questions
    "timeout_seconds": "How long to wait",
    "assumption": "What assumption to use if no answer",
}
```

When LLM calls `suspend_and_ask` with `target_role`, ExternalAdapter handles it.
When LLM calls `suspend_and_ask` with `ask_node`, agent-to-agent path (v6.2).

### 4.3 Multi-role matching

Human với `roles: ["pm", "product-owner"]` sẽ nhận questions với:
- `required_role: "pm"` ✅
- `required_role: "product-owner"` ✅
- `required_role: "devops"` ❌

Khi submit answer, participant specify `role_used` — để room log biết
"answered as PM" hay "answered as product-owner".

---

## 5. Conversation room — shared view

### 5.1 Room as shared inbox

Room là nơi tất cả participants xem và interact với team's questions:

```
ROOM: Python Backend API — Cluster A
══════════════════════════════════════════════════════════

[OPEN] Q: Should deactivated users' data be hidden or deleted?
  Asked by: dev-A (task: implement-user-model)
  Required role: pm
  Timeout: 24h | Assumption: Hide data with is_active flag
  Notified: Nguyen PM
  ──────────────────────────────────────────────────────
  💬 Nguyen PM [as pm, 10:15]: Hide — never delete, we need audit trail
     → ✅ RESOLVED — dev-A resumed

[OPEN] Q: Deploy on AWS or GCP?
  Asked by: architect-A (task: design-infra)
  Required role: devops
  Timeout: 8h | Assumption: AWS us-east-1
  Notified: John DevOps
  ──────────────────────────────────────────────────────
  ⏳ Waiting for reply...

[ANSWERED] Q: API versioning strategy — v1 only or v1+v2 from start?
  Asked by: analyst-A
  Required role: product-owner
  ──────────────────────────────────────────────────────
  💬 Nguyen PM [as product-owner, yesterday 14:30]: v1 only for now
  💬 John DevOps [comment, yesterday 15:00]: Note: v2 will need breaking
     changes if we add versioning later — might want to plan now
  💬 Nguyen PM [as product-owner, yesterday 15:20]: Good point, let's
     add /api/v1/ prefix from day 1 even if only v1 exists
     → ✅ RESOLVED (updated answer used)
```

### 5.2 Room operations

**View room:**
```
GET /room/{cluster_id}
→ Full ChannelLog with all threads and replies
→ Filterable: ?status=open | answered | all
→ Pagination: ?page=1&per_page=20
```

**View single thread:**
```
GET /room/{cluster_id}/threads/{question_id}
→ Full InteractionThread with all replies in order
```

**Submit answer (resolves thread if role matches):**
```
POST /room/{cluster_id}/threads/{question_id}/reply
Authorization: Bearer {participant_auth_token}
{
  "reply_type": "answer",
  "role_used": "pm",
  "text": "Hide data, never delete — we need audit trail"
}

→ ExternalAdapter validates: does participant have role_used?
→ If yes + thread is open: set as resolution, emit participant.answered
→ If thread already resolved: reply recorded as comment
→ Returns: {reply_id, status: "resolved" | "commented"}
```

**Add comment (does not resolve):**
```
POST /room/{cluster_id}/threads/{question_id}/reply
{
  "reply_type": "comment",
  "text": "Note: this also affects the reporting module"
}
→ Recorded, visible to all, does NOT trigger participant.answered event
```

**Tag another participant:**
```
POST /room/{cluster_id}/threads/{question_id}/reply
{
  "reply_type": "tag",
  "text": "@John — this touches infra, can you confirm?",
  "tagged_participant": "participant-id-john"
}
→ John receives notification of tag
→ Recorded in thread for everyone to see
```

---

## 6. Participant registration and management

### 6.1 Register participant

```
POST /participants/register
{
  "name": "Nguyen PM",
  "roles": ["pm", "product-owner"],
  "transport": "webhook",
  "transport_target": "https://your-app.com/webhook/gnot",
  "cluster_id": "cluster-A"
}

Response 201:
{
  "participant_id": "p-uuid-xxx",
  "auth_token": "human-tok-xxx",   ← dùng khi submit answers
  "name": "Nguyen PM",
  "roles": ["pm", "product-owner"],
  "cluster_id": "cluster-A"
}
```

**Auth token:** Seed node (external-adapter) issue token khi register.
Token này verified khi participant submit answer — đảm bảo chỉ
registered participants có thể answer.

### 6.2 Update participant

```
PATCH /participants/{participant_id}
Authorization: Bearer {participant_auth_token}
{
  "roles": ["pm", "product-owner", "stakeholder"],   ← thêm role mới
  "transport": "polling"                             ← đổi kênh
}
```

### 6.3 Deactivate participant

```
DELETE /participants/{participant_id}
Authorization: Bearer {participant_auth_token}
→ Sets active: false
→ Participant no longer receives notifications
→ Historical replies preserved in room (không xóa)
```

### 6.4 List participants

```
GET /participants?cluster_id=cluster-A
→ List all active participants với roles
→ Used by room UI to show "who's in this team"
```

---

## 7. Notification delivery — three modes

### 7.1 Mode A: Webhook push

```yaml
# Participant registration
transport: webhook
transport_target: https://your-app.com/webhook/gnot-team-a
```

ExternalAdapter POST đến webhook khi:
- New question targeting participant's role
- Participant tagged in a thread
- (Optional) Thread resolved

```json
POST https://your-app.com/webhook/gnot-team-a
{
  "event": "question.new",
  "cluster_id": "cluster-A",
  "question_id": "q-abc123",
  "required_role": "pm",
  "question": "Should deactivated users' data be hidden or deleted?",
  "asked_by": "dev-A",
  "timeout_at": 1741478400,
  "assumption": "Hide data with is_active flag",
  "reply_url": "https://gnot.local/room/cluster-A/threads/q-abc123/reply",
  "room_url": "https://gnot.local/room/cluster-A"
}
```

Webhook receiver tự quyết làm gì với payload: send Telegram message,
post to Slack, trigger email, show in custom UI, etc.

### 7.2 Mode B: Polling

```yaml
transport: polling
transport_target: ""    # no target needed for polling
```

Participant checks for new questions:

```
GET /room/{cluster_id}/pending?participant_id={pid}&since={timestamp}
Authorization: Bearer {participant_auth_token}

Response:
{
  "pending": [
    {
      "question_id": "q-abc123",
      "required_role": "pm",
      "question": "...",
      "created_at": 1741392000,
      "timeout_at": 1741478400
    }
  ],
  "count": 1
}
```

Participant polls at their convenience. No push required.
Suitable for integrations where push is not available.

### 7.3 Mode C: /intent session bridge ← Recommended

**Đây là mode tự nhiên nhất cho human participation.**

Participant mở một `/intent` session với `external-adapter` node.
Session này acts as their **personal team room inbox** — conversational,
natural language, no separate UI needed.

```
Participant: "Tôi là Nguyen, tôi muốn join cluster-A với role pm và product-owner"

ExternalAdapter LLM:
→ POST /participants/register (internal)
→ "Đã register. Bạn sẽ nhận câu hỏi cho role: pm, product-owner.
   Tôi sẽ gửi câu hỏi mới vào chat này ngay khi có."

[15 phút sau — agent hỏi]

ExternalAdapter LLM:
→ "📋 Câu hỏi mới từ dev-A:
   'Should deactivated users' data be hidden or deleted?'
   Context: đang implement user model, cần biết behavior khi user bị deactivate.
   Assumption nếu không trả lời trong 24h: hide with is_active flag"

Participant: "Hide, không delete — chúng ta cần audit trail cho compliance"

ExternalAdapter LLM:
→ POST /room/cluster-A/threads/q-abc123/reply (internal)
→ emit participant.answered
→ "Đã ghi nhận và gửi answer cho dev-A. Dev-A sẽ tiếp tục task."

[John cũng trong session riêng, thấy thread khác]
```

```yaml
# external-adapter node.yaml
node_id: external-adapter-cluster-A
llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
skills_file: ./skills.md   # describes role as human interface coordinator
```

```markdown
<!-- external-adapter skills.md -->
# Human Interface Coordinator

You are the bridge between the AI dev team and human participants.

## Your responsibilities
- Help humans register as participants with appropriate roles
- Present new questions clearly with full context
- Collect answers and submit them to the agent network
- Show room activity when asked
- Remind participants of open questions with approaching timeouts

## When presenting a question:
- State clearly which agent asked and why
- Show the context (what they were working on)
- State the timeout and assumption
- Ask for a clear answer

## When receiving an answer:
- Confirm which role they're answering as (if they have multiple)
- Submit via POST /room/{cluster_id}/threads/{question_id}/reply
- Confirm submission to participant
```

**Multi-participant /intent sessions (future):**

Trong tương lai, nhiều humans có thể tham gia cùng một `/intent` session —
giống group chat. Session LLM manage turn-taking và submissions.
Đây là P4 — không trong scope v6.4.

---

## 8. Event changes — v6.4

### 8.1 `participant.input_required` — updated

```python
# v6.4: add required_role field
{
    "event_type": "participant.input_required",
    "correlation_id": "q-abc123",
    "source_node": "dev-A",
    "payload": {
        "question_id": "q-abc123",
        "question": "Should deactivated users' data be hidden or deleted?",
        "required_role": "pm",               # NEW v6.4
        "source_agent": "dev-A",
        "source_task": "task-feature-x",
        "context_summary": [...],
        "timeout_seconds": 86400,
        "assumption": "Hide data with is_active flag",
    }
}
```

### 8.2 `participant.answered` — updated

```python
{
    "event_type": "participant.answered",
    "correlation_id": "q-abc123",            # same as question
    "source_node": "external-adapter-cluster-A",
    "payload": {
        "question_id": "q-abc123",
        "answer": "Hide data, never delete — audit trail required",
        "resolved_by": "Nguyen PM",           # human display name
        "resolved_as_role": "pm",             # which role they used
        "participant_id": "p-uuid-xxx",       # for internal tracking
        "resolved_at": 1741395600.0,
    }
}
```

### 8.3 New event: `participant.commented`

```python
# When participant adds context without answering
{
    "event_type": "participant.commented",
    "correlation_id": "q-abc123",
    "payload": {
        "question_id": "q-abc123",
        "comment": "Note: this also affects the reporting module",
        "commented_by": "John DevOps",
        "participant_id": "p-uuid-yyy",
    }
}
# Does NOT trigger task resumption — informational only
# Agent's task remains suspended
```

### 8.4 New event: `participant.tagged`

```python
# When participant tags another
{
    "event_type": "participant.tagged",
    "correlation_id": "q-abc123",
    "payload": {
        "question_id": "q-abc123",
        "tagged_by": "Nguyen PM",
        "tagged_participant_id": "p-uuid-john",
        "message": "@John — this touches infra, can you confirm?",
    }
}
# Triggers notification to tagged participant via their preferred mode
```

---

## 9. ExternalAdapter node — v6.4 complete design

### 9.1 New components

```python
# runtime/participant_registry.py

class ExternalParticipantRegistry:
    """
    Stores and manages human participants for each team.

    Storage: JSON file per team (participants-{cluster_id}.json).
    In-memory index: role → list[ExternalParticipant] for fast lookup.

    Public API:
        register(participant_spec) → ExternalParticipant
        get(participant_id) → ExternalParticipant | None
        get_by_role(role, cluster_id) → list[ExternalParticipant]
        update(participant_id, updates) → ExternalParticipant
        deactivate(participant_id) → None
        list_team(cluster_id) → list[ExternalParticipant]
        validate_token(participant_id, auth_token) → bool
    """


# runtime/conversation_room.py

class ChannelLog:
    """
    Manages the shared conversation room for a team.

    Storage: JSONL file per team (room-{cluster_id}.jsonl) — append-only.
    In-memory: open threads indexed by question_id.

    Public API:
        open_thread(question) → InteractionThread
        add_reply(question_id, reply) → InteractionReply
        resolve_thread(question_id, answer) → InteractionThread
        get_thread(question_id) → InteractionThread | None
        list_threads(cluster_id, status) → list[InteractionThread]
        list_pending(participant_id) → list[InteractionThread]
    """


# runtime/question_router.py

class QuestionRouter:
    """
    Routes incoming participant.input_required events to appropriate participants.

    Algorithm:
    1. Parse required_role from event payload
    2. Lookup participants with that role in ExternalParticipantRegistry
    3. Open thread in ChannelLog
    4. Notify all matching participants via their preferred mode
    5. Track notified participants in thread

    Fallback: if no participants have required role, log warning.
    Thread still opened — will time out and use assumption.
    """

    async def route(self, event: Event) -> InteractionThread: ...

    async def _notify_webhook(self, participant: ExternalParticipant, thread: InteractionThread) -> None: ...

    async def _notify_polling(self, participant: ExternalParticipant, thread: InteractionThread) -> None:
        # No-op: polling participants check /room/pending themselves
        ...
```

### 9.2 node.yaml cho external-adapter node

```yaml
# external-adapter-cluster-A/node.yaml

node_id: external-adapter-cluster-A
listen: 0.0.0.0:8099

# Join team as member (v6.1 gateway membership)
gateway_node_id: gateway-cluster-A
gateway_address: http://localhost:8091
gateway_auth_token: tok-hi-on-team-a

# Multi-team: if PM watches both teams
additional_gateways: []   # seed adds cluster-B here if needed

# LLM for /intent session bridge
llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
  api_key: "${ANTHROPIC_API_KEY}"

skills_file: ./skills.md

# EventBus — subscribe to participant.input_required from team
event_bus:
  enabled: true

# Scheduler — react to participant.input_required events
scheduler:
  enabled: true

schedule:
  - trigger_type: event
    on_event_type: "participant.input_required"
    run_action: route_interaction_to_participants
    description: Route agent question to humans with matching role

  - trigger_type: event
    on_event_type: "participant.tagged"
    run_action: notify_tagged_participant
    description: Notify participant when tagged

  # Check for approaching timeouts every 15 minutes
  - trigger_type: cron
    cron_expression: "*/15 * * * *"
    run_action: check_interaction_timeouts
    description: Remind participants of questions nearing timeout

# Storage
participant_registry:
  storage_dir: ./participants

conversation_room:
  storage_dir: ./room
```

### 9.3 Seed provisions external-adapter node

Human-interface node được tạo bởi `ClusterOrchestrator` như các nodes khác, nhưng
sau tất cả worker nodes (vì nó cần join team gateway):

```python
# Trong ClusterSpec — v6.4 addition
members: [
    NodeSpec(role="gateway", ...),
    NodeSpec(role="pm", ...),
    NodeSpec(role="analyst", ...),
    # ... other workers ...
    NodeSpec(role="external-adapter", bootstrap=BootstrapRequest(
        node_id=f"external-adapter-{cluster_id}",
        listen=f"0.0.0.0:{port}",
        gateway_node_id=gateway_node_id,
        gateway_address=gateway_address,
        llm=LLMSpec(...),
        skills_md=load_blueprint("external-adapter"),
        event_bus=EventBusSpec(),
        scheduler=SchedulerSpec(),
        schedule=[
            ScheduleEntrySpec(
                trigger_type="event",
                on_event_type="participant.input_required",
                run_action="route_interaction_to_participants",
            )
        ],
    ))
]
```

---

## 10. `suspend_and_ask` — v6.4 update

Agent LLM gọi `suspend_and_ask` với `target_role` thay vì `ask_node` khi hỏi human:

```python
# Hai usage patterns:

# Pattern 1: Hỏi human (v6.4)
mesh_action(action="suspend_and_ask", params={
    "question": "Should deactivated users' data be hidden or deleted?",
    "target_role": "pm",                      # NEW v6.4 — target role
    "timeout_seconds": 86400,
    "assumption": "Hide data with is_active flag",
})

# Pattern 2: Hỏi agent (v6.2 unchanged)
mesh_action(action="suspend_and_ask", params={
    "question": "What is the null user behavior per design spec?",
    "ask_node": "architect-A",             # v6.2 — target agent node
    "timeout_seconds": 7200,
    "assumption": "Throw UserNotFoundException",
})
```

**IntentHandler routing logic:**

```python
async def _handle_suspend_request(self, task_id, session, turn, suspend_params):
    if "target_role" in suspend_params:
        # Human question path (v6.4)
        await self._event_bus.emit(Event(
            event_type="participant.input_required",
            payload={
                **suspend_params,
                "required_role": suspend_params["target_role"],
                "question_id": question_id,
                ...
            }
        ))
    elif "ask_node" in suspend_params:
        # Agent question path (v6.2)
        await self._event_bus.emit(Event(
            event_type="clarification.needed",
            payload={...}
        ))
```

---

## 11. End-to-end scenario — v6.4 complete

```
═══════════════════════════════════════════════════════════
SETUP
═══════════════════════════════════════════════════════════

Seed provisions cluster-A including external-adapter-cluster-A node.

Two humans register:

  Nguyen → POST /participants/register (to external-adapter-cluster-A)
  {
    name: "Nguyen PM", roles: ["pm", "product-owner"],
    transport: "session"   ← Nguyen uses /intent session
  }
  → receives: participant_id: "p-nguyen", auth_token: "tok-nguyen"

  John → POST /participants/register
  {
    name: "John DevOps", roles: ["devops", "cloud-architect"],
    transport: "webhook",
    transport_target: "https://john-bot.example.com/gnot-hook"
  }
  → receives: participant_id: "p-john", auth_token: "tok-john"

Nguyen opens /intent session with external-adapter-cluster-A:
  Nguyen: "Hi, tôi đã đăng ký là Nguyen PM"
  HI-LLM: "Chào Nguyen! Tôi sẽ forward câu hỏi cho bạn khi có.
            Hiện tại không có câu hỏi nào đang chờ."

═══════════════════════════════════════════════════════════
RUNTIME — QUESTION 1 (PM domain)
═══════════════════════════════════════════════════════════

t=1: dev-A đang implement user model, gặp ambiguity
     LLM calls:
     mesh_action("suspend_and_ask", {
       "question": "Should deactivated users' data be hidden or deleted?",
       "target_role": "pm",
       "timeout_seconds": 86400,
       "assumption": "Hide data with is_active flag"
     })

t=2: IntentHandler:
     → saves CheckpointStore (v6.2)
     → sets job SUSPENDED (v6.2)
     → emits participant.input_required {required_role: "pm", ...}

t=3: external-adapter-cluster-A receives event
     QuestionRouter:
     → open thread in ChannelLog
     → lookup participants with role "pm": [Nguyen]
     → Nguyen transport=session → inject into Nguyen's /intent session

t=4: Nguyen's session:
     HI-LLM: "📋 Câu hỏi mới từ dev-A [đang implement-user-model]:
              'Should deactivated users' data be hidden or deleted?'
              Timeout: 24h | Assumption: Hide data with is_active flag"

     Nguyen: "Hide — không được delete, chúng ta cần audit trail cho compliance"

     HI-LLM:
     → POST /room/cluster-A/threads/q-abc/reply
       {reply_type: "answer", role_used: "pm", text: "Hide..."}
     → validates: Nguyen has role "pm" ✅
     → sets thread resolved
     → emits participant.answered {question_id: "q-abc", answer: "Hide...", resolved_as_role: "pm"}
     → "Đã ghi nhận. Dev-A sẽ tiếp tục task của mình."

t=5: dev-A's Scheduler receives participant.answered
     → handle_clarification_answer (v6.2)
     → TaskPool.resume_task("q-abc", "Hide data — audit trail required")
     → dev-A continues task X with answer injected

═══════════════════════════════════════════════════════════
RUNTIME — QUESTION 2 (DevOps domain, with multi-human interaction)
═══════════════════════════════════════════════════════════

t=6: architect-A: "Deploy to AWS hay GCP?"
     mesh_action("suspend_and_ask", {
       "question": "AWS or GCP for production deployment?",
       "target_role": "devops",
       "timeout_seconds": 28800,   # 8h
       "assumption": "AWS us-east-1"
     })

t=7: QuestionRouter:
     → lookup role "devops": [John]
     → John transport=webhook → POST to John's webhook

     John's Telegram bot sends message to John:
     "📋 New question from architect-A:
      AWS or GCP for production deployment?
      Reply at: https://gnot.local/room/cluster-A/threads/q-def/reply"

t=8: Nguyen (still in session) sees the thread in room:
     Nguyen: "Show me current questions"
     HI-LLM: "[OPEN] Deploy on AWS or GCP? (for John DevOps)"
     Nguyen: "Add comment — chúng ta có existing billing account với AWS"

     HI-LLM:
     → POST /room/cluster-A/threads/q-def/reply
       {reply_type: "comment", text: "Note: we have existing AWS billing..."}
     → thread.replies += Comment(Nguyen, "Note: we have existing AWS...")
     → emits participant.commented (informational, architect-A NOT resumed)
     → "Comment đã được ghi nhận, John sẽ thấy khi trả lời."

t=9: John logs in, sees webhook, clicks room URL
     John sees: question + Nguyen's comment
     John submits via API:
     POST /room/cluster-A/threads/q-def/reply
     Authorization: Bearer tok-john
     {reply_type: "answer", role_used: "devops", text: "AWS. Existing contract + Nguyen's note confirms"}

     → validates: John has role "devops" ✅
     → resolved
     → emits participant.answered
     → architect-A resumes

═══════════════════════════════════════════════════════════
RUNTIME — QUESTION 3 (no one has the role)
═══════════════════════════════════════════════════════════

t=10: dev-A: "What's the company's data retention policy?"
      mesh_action("suspend_and_ask", {
        "target_role": "legal",
        "question": "What is the data retention policy?",
        "timeout_seconds": 7200,
        "assumption": "Retain for 7 years per standard practice"
      })

t=11: QuestionRouter:
      → lookup role "legal": [] (no participants have this role)
      → log warning: "No participants with role 'legal' in cluster-A"
      → thread opened but nobody notified

      → Nguyen in session receives:
        HI-LLM: "⚠️ Câu hỏi mới từ dev-A cần role 'legal', nhưng
                 hiện tại không có ai có role này trong team.
                 Câu hỏi sẽ timeout sau 2h và dùng assumption:
                 'Retain for 7 years per standard practice'
                 Bạn có muốn tự trả lời hoặc thêm participant mới không?"

      Nguyen: "Tôi có thể trả lời câu này"
      HI-LLM: "Bạn hiện có roles: pm, product-owner. Câu này yêu cầu role 'legal'.
               Tôi có thể thêm role 'legal' cho bạn nếu bạn muốn tự answer,
               hoặc bạn có thể answer as comment (không resolve, chờ timeout)."
      Nguyen: "Thêm role legal cho tôi và tôi sẽ trả lời"
      HI-LLM: → PATCH /participants/p-nguyen {roles: [..., "legal"]}
              → "Đã thêm. Bây giờ bạn có thể trả lời câu hỏi này."
      Nguyen: "Retain for 3 years per local regulation"
      HI-LLM: → POST /room/cluster-A/threads/q-ghi/reply
                 {reply_type: "answer", role_used: "legal", text: "3 years..."}
              → resolved
              → dev-A resumed
```

---

## 12. New HTTP endpoints — v6.4

| Endpoint | Node | Description |
|----------|------|-------------|
| `POST /participants/register` | external-adapter | Register new human participant |
| `GET /participants` | external-adapter | List participants (optionally by team) |
| `GET /participants/{id}` | external-adapter | Get participant details |
| `PATCH /participants/{id}` | external-adapter | Update roles or notify settings |
| `DELETE /participants/{id}` | external-adapter | Deactivate participant |
| `GET /room/{cluster_id}` | external-adapter | View full conversation room |
| `GET /room/{cluster_id}/pending` | external-adapter | Pending questions for participant |
| `GET /room/{cluster_id}/threads/{qid}` | external-adapter | Single thread detail |
| `POST /room/{cluster_id}/threads/{qid}/reply` | external-adapter | Submit answer/comment/tag |

---

## 13. Files thay đổi — v6.4

| File | Type | Description |
|------|------|-------------|
| `runtime/external_participant_registry.py` | NEW | ExternalParticipant storage + role-based lookup |
| `runtime/channel_log.py` | NEW | ChannelLog + InteractionThread + Reply |
| `runtime/interaction_router.py` | NEW | Route events to participants by role, notify |
| `runtime/models.py` | MODIFY | ExternalParticipant, InteractionThread, InteractionAnswer, InteractionReply |
| `runtime/server.py` | MODIFY | All /participants and /room endpoints |
| `runtime/intent_handler.py` | MODIFY | `suspend_and_ask`: `target_role` path for human questions |
| `runtime/bootstrap.py` | MODIFY | ExternalParticipantRegistry + ChannelLog config in BootstrapRequest |
| `blueprints/roles/external-adapter.md` | NEW | Skills for external-adapter coordinator node |
| `seed/actions/route_interaction_to_participants.py` | NEW | QuestionRouter trigger action |
| `seed/actions/notify_tagged_participant.py` | NEW | Tag notification action |
| `seed/actions/check_interaction_timeouts.py` | NEW | Timeout reminder cron action |
| `docs/worklog/SPECS_V6.4.md` | NEW | This document |
| `docs/worklog/WORKLOG_V6.4.md` | NEW | Implementation worklog |

---

## 14. Revision: v6.2 ExternalAdapter design

v6.4 replaces v6.2's sketch of ExternalAdapter with complete design.
Previous options (A polling / B webhook / C session) are now all supported
as `transport` modes per participant. Not mutually exclusive per team —
each participant chooses their preferred mode.

| v6.2 sketch | v6.4 realization |
|-------------|-----------------|
| Single anonymous user | ExternalParticipant with identity and roles |
| One delivery mode per node | Per-participant transport preference |
| No routing | Role-based routing via QuestionRouter |
| No shared view | ChannelLog with full thread visibility |
| Single-reply | Multi-reply: answer + comment + tag |
| No registry | ExternalParticipantRegistry (file-based, per cluster) |
| /intent session bridge (optional) | First-class mode, recommended UX |

---

## 15. Open questions — v6.4

**Q1: Cross-team questions**
Nếu dev-A (cluster-A) hỏi role "devops" nhưng cluster-A không có DevOps participant,
có thể escalate lên seed để route đến DevOps participant ở team khác không?

Đây là "cross-team human escalation" — không trong scope v6.4.
Workaround: register John as participant of cả cluster-A và cluster-B.
(John có thể join multiple teams — same as multi-gateway node model)

**Q2: Answer authority conflict**
Nếu hai PMs trong cùng team đều trả lời cùng câu hỏi:
- First-wins: PM A answer accepted, PM B answer visible as comment
- Consensus: cả hai phải đồng ý (complex)
- Override: senior PM có thể override junior PM (requires hierarchy)

v6.4 dùng first-wins. Hierarchy và consensus là P4.

**Q3: Notification reliability**
Webhook có thể fail (target down, timeout). Cần retry?
Nếu webhook fail 3 lần → fallback sang polling?

Recommendation: retry 3x exponential backoff. Nếu fail → log warning,
participant tự check via polling. Không hard-fail question routing.

**Q4: Participant visibility**
Should all participants see ALL questions (including those targeting other roles)?
v6.4 answer: YES — full transparency. PM có thể xem infra questions để
maintain context về project. Visibility ≠ responsibility to answer.

**Q5: Human-interface node per team hay shared?**
Option A: Một external-adapter node cho mỗi team
Option B: Một shared external-adapter node cho tất cả teams

v6.4 spec: one per team (consistent với gateway-as-channel model).
Shared external-adapter là P4 optimization.

---

## 16. Readiness sau v6.4

```
v6.0: Event-driven agents
v6.1: Multi-team topology
v6.2: Task suspension + human-in-the-loop (single user)
v6.3: Self-provisioning teams
v6.4: Multi-human participation with roles

After v6.4:

  Human participation:
    ✅ Multiple humans per team
    ✅ Role-based (not person-based) question routing
    ✅ Multi-role humans
    ✅ Async — no timezone/availability tracking
    ✅ Shared conversation room — full transparency
    ✅ Three notification modes: webhook, polling, /intent session
    ✅ Comments and tags — collaborative without resolving
    ✅ Dynamic role addition (escalate to human without right role)
    ✅ First-valid-answer semantics
    ✅ Thread history preserved (audit trail)

  Still not covered:
    ❌ Cross-team human escalation (P4)
    ❌ Answer authority hierarchy / consensus (P4)
    ❌ Group /intent session (multiple humans in one chat) (P4)
    ❌ Shared external-adapter node across teams (P4 optimization)
```

---

*Spec: SPECS_V6.4.md | Mesh Runtime v6.4 | Repository: ai-infra-runtime-v2*
*Builds on: SPECS_V6.3.md, SPECS_V6.2.md, SPECS_V6.1.md, SPECS_V6.0.md*
*Problem statement: product owner review session, 2026-03-08*
