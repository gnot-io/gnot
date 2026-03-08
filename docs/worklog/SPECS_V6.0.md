# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v6.0
### Event-Driven Infrastructure · Autonomous Agent Primitives · Channel-Scoped Communication

**Base version:** v5.13b  
**Target version:** v6.0  
**Status:** Design / Pre-implementation  
**Authors:** Architecture review session, 2026-03-08

---

## 1. Bối cảnh và động lực

### 1.1 Hạn chế của mô hình hiện tại (v5.13b)

Runtime hiện tại (v5.13b) là một **synchronous request/response mesh**: mọi giao tiếp
giữa các node đều là point-to-point HTTP call do LLM (hoặc human) chủ động điều phối.
Mô hình này hoạt động tốt cho các workflow đơn giản nhưng có hai hạn chế cơ bản:

**Hạn chế 1 — PM bottleneck:**
```
Mọi bước đều qua PM:
  PM → gọi Analyst → chờ → PM → gọi Architect → chờ → PM → ...
```
PM trở thành single point of coordination. Nếu PM bận hoặc chậm, toàn bộ pipeline
đình trệ. Các node không thể tự phối hợp trực tiếp với nhau.

**Hạn chế 2 — Nodes hoàn toàn passive:**
Không có node nào tự khởi động công việc. Mọi hành động đều chờ được gọi từ bên ngoài.
Không có cơ chế để một node nói: "Tôi thấy có việc cần làm — tôi sẽ tự làm."

### 1.2 Use case thúc đẩy: AI Dev Team

Phân tích chi tiết trong `Guide 23 — AI Dev Team` cho thấy để đạt được hành vi của
một pro dev team thực sự, cần:

1. **Agents tự phối hợp** không qua PM trung gian
2. **Human checkpoints có cấu trúc** — dừng lại hỏi user ở các decision points
3. **Scoped communication** — nhiều dev team trên cùng mesh, mỗi team có channel riêng
4. **Self-aware nodes** — node tự biết khi nào cần hành động mà không cần được gọi

### 1.3 Phạm vi của v6.0

v6.0 bổ sung **3 modules mới** vào runtime hiện tại, không breaking change với v5.x:

| Module | Mục đích |
|--------|----------|
| `ChannelRegistry` | Scoped group communication — namespace cho broadcast |
| `EventBus` | Pub/sub infrastructure — phát và nhận business events |
| `Scheduler` | 3-level autonomy triggers — condition, poll, event |

Cộng với 7 HTTP endpoints mới và các config fields tương ứng.

---

## 2. Phân tích hiện trạng — Coverage map

### 2.1 Những gì v5.13b đã có (solid)

| Feature | Component | Notes |
|---------|-----------|-------|
| Point-to-point action routing | `GatewayRouter` | Push/pull, loop protection |
| Pull-mode polling (level 2) | `WorkerAgent._poll_loop` | Fixed 5s interval |
| Heartbeat / liveness tracking | `NodeRegistry` | Lazy staleness check |
| BGP-style route advertisement | `NodeRegistry` + `WorkerAgent` | Multi-hop |
| Async job queue + result polling | `JobQueue` + `JobManager` | Push/pull modes |
| File staging (upload/download) | `UploadManager` | TTL, size limit |
| Session memory per /intent | `ConversationStore` | In-memory, TTL |
| Bootstrap new nodes dynamically | `BootstrapEngine` | skills_md, pip_packages |
| Node persona via skills_file | `config.skills_file` | GET /skills endpoint |
| Per-node auth tokens | `AuthMiddleware` | v5.13b |
| Caller authorization policies | `CallerPolicy` | v5.11 |
| Credential store (encrypted) | `CredentialStore` | v5.11 |

### 2.2 Những gì còn thiếu (gap analysis)

**Gap A — Communication scope:**

| Feature | Status | Impact |
|---------|--------|--------|
| Channel / group namespace | ❌ thiếu | Không thể isolate nhiều teams trên cùng mesh |
| Scoped broadcast to channel | ❌ thiếu | Broadcast sẽ hit toàn bộ mesh |
| Event type / topic namespace | ❌ thiếu | Không thể filter event theo domain |

**Gap B — Event infrastructure:**

| Feature | Status | Impact |
|---------|--------|--------|
| Event bus / pub-sub | ❌ thiếu | Agents không thể react to events |
| Subscribe to event type | ❌ thiếu | Không có listener mechanism |
| Emit arbitrary business event | ❌ thiếu | Không có POST /emit |
| Event log / audit trail | ❌ thiếu | Không trace được flow |
| Wildcard topic subscription | ❌ thiếu | Không thể subscribe "test.*" |

**Gap C — Autonomy levels:**

| Feature | Status | Impact |
|---------|--------|--------|
| Level 1: self-check condition trigger | ❌ thiếu | Node không tự khởi động |
| Level 2: adaptive poll interval | ⚠️ partial | Fixed 5s, không backoff khi idle |
| Level 3: event-triggered action | ❌ thiếu | Không có subscription-to-callback |
| Cron / time-based trigger | ❌ thiếu | Không có scheduled jobs |
| Priority lanes trong job queue | ❌ thiếu | Tất cả jobs bình đẳng |

**Gap D — Coordination:**

| Feature | Status | Impact |
|---------|--------|--------|
| Human checkpoint pause/resume | ❌ thiếu | Pipeline không dừng chờ input |
| Shared project conversation log | ❌ thiếu | Agents không có common context |
| Persistent session cross-restart | ❌ thiếu | ConversationStore in-memory only |
| Agent-to-agent direct messaging | ❌ thiếu | Phải qua PM relay |

---

## 3. Kiến trúc v6.0

### 3.1 Tổng quan

```
┌─────────────────────────────────────────────────────────────────┐
│                        GNOT Node Runtime v6.0                   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Existing v5.13b Core                  │   │
│  │  GatewayRouter · NodeRegistry · JobQueue · JobManager    │   │
│  │  WorkerAgent · IntentHandler · ConversationStore         │   │
│  │  BootstrapEngine · UploadManager · CredentialStore       │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│            ┌─────────────────┼────────────────┐                 │
│            │                 │                │                 │
│            ▼                 ▼                ▼                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │  Channel     │  │  EventBus    │  │  Scheduler   │          │
│  │  Registry    │  │              │  │              │          │
│  │  v6.0 NEW    │  │  v6.0 NEW    │  │  v6.0 NEW    │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│                                                                  │
│  New HTTP endpoints:                                             │
│  POST /channels/{id}/emit    POST /emit                          │
│  GET  /channels              POST /subscribe                     │
│  GET  /events                DELETE /subscriptions/{id}          │
│  POST /schedule              DELETE /schedule/{id}               │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Interaction model — AI Dev Team (event-driven)

Với v6.0, flow thay đổi từ PM-orchestrated sang peer-to-peer:

```
BEFORE (v5.13b — PM orchestrates everything):
  PM → call analyst → wait → PM → call architect → wait → PM → ...

AFTER (v6.0 — event-driven, PM only watches):
  PM: emit("cluster.started", channel="node-cluster-1")
       │
       ├─► analyst-node (subscribed: cluster.started)
       │     → analyze → emit("stories.ready")
       │           │
       │           └─► architect-node (subscribed: stories.ready)
       │                 → design → emit("design.proposed")
       │                       │
       │                       ├─► human interface (subscribed: human.*)
       │                       │     → ask user → emit("participant.answered")
       │                       │           │
       │                       │           └─► architect-node (subscribed: participant.answered)
       │                       │                 → finalize → emit("design.locked")
       │                       │                       │
       │                       └─► dev-node (subscribed: design.locked)
       │                             → code → emit("code.ready")
       │                                   │
       │                                   └─► test-node (subscribed: code.ready)
       │                                         → test
       │                                         → PASS: emit("test.passed")
       │                                         → FAIL: emit("test.failed")
       │                                               │
       │                                               └─► dev-node (retry)
       │
       └─► PM (subscribed: ALL events) — watches, escalates human.* events, writes audit log
```

PM không call bất kỳ ai — chỉ emit event khởi đầu và watch full flow.

---

## 4. Module A: ChannelRegistry

### 4.1 Mục đích

Channel là **namespace cho communication**. Khi một mesh có nhiều dev teams (node-cluster-1,
node-cluster-2, ops-team), broadcast/emit của team này không ảnh hưởng team kia.

Channel cũng là unit of **access control**: node chỉ nhận events từ channel mà nó là member.

### 4.2 Data model

```python
@dataclass
class ChannelMembership:
    node_id: str
    channel_id: str
    joined_at: float
    role: str = "member"    # "member" | "owner" | "observer"
    # observer: nhận events nhưng không thể emit
    # owner: có thể kick members, đổi channel config

@dataclass
class Channel:
    channel_id: str          # e.g. "node-cluster-1", "ops-team"
    display_name: str
    created_at: float
    created_by: str          # node_id của creator
    members: dict[str, ChannelMembership]   # node_id → membership
    description: str = ""
    # event_types_allowed: list[str] | None = None  # None = all types allowed
```

### 4.3 Configuration — `node.yaml`

```yaml
# v6.0 — Channel membership declaration
channels:
  - node-cluster-1          # string form: join as "member"
  - id: ops-alerts      # object form: với role
    role: observer
  - id: all-nodes
    role: member
```

Channels trong `node.yaml` tạo **static membership** — không cần API call để join, loaded
khi node startup. Dynamic membership (join/leave at runtime) cũng được hỗ trợ qua API.

### 4.4 HTTP Endpoints

#### `GET /channels`
List tất cả channels mà node này là member.

```
Response 200:
{
  "channels": [
    {
      "channel_id": "node-cluster-1",
      "display_name": "Dev Team 1",
      "member_count": 6,
      "my_role": "member"
    }
  ]
}
```

#### `POST /channels/{channel_id}/join`
Dynamic membership — node này join channel.

```
Request:
{
  "role": "member"    // optional, default "member"
}

Response 200:
{
  "channel_id": "node-cluster-1",
  "node_id": "analyst-node",
  "role": "member",
  "joined_at": 1741392000.0
}
```

#### `POST /channels/{channel_id}/leave`
Rời channel. Subscriptions của node trong channel này bị xóa.

#### `GET /channels/{channel_id}/members`
List members của một channel (chỉ visible với members).

```
Response 200:
{
  "channel_id": "node-cluster-1",
  "members": [
    {"node_id": "pm-node", "role": "owner", "joined_at": ...},
    {"node_id": "analyst-node", "role": "member", "joined_at": ...},
    ...
  ]
}
```

#### `POST /channels/{channel_id}/emit`
Emit event đến tất cả subscribers trong channel. Xem Section 5 (EventBus).

### 4.5 ChannelRegistry class

```python
# runtime/channel_registry.py

class ChannelRegistry:
    """
    Manages channel membership and scoped broadcast.
    
    Thread-safe via asyncio.Lock (same pattern as NodeRegistry).
    Static membership loaded from node.yaml on startup.
    Dynamic membership via join/leave API.
    """

    def __init__(
        self,
        node_id: str,
        static_channels: list[dict | str] | None = None,
    ) -> None:
        self._node_id = node_id
        self._channels: dict[str, Channel] = {}
        self._lock = asyncio.Lock()
        
        # Load static channels from node.yaml
        for ch in (static_channels or []):
            if isinstance(ch, str):
                self._ensure_channel(ch)
                self._add_member(ch, node_id, role="member")
            elif isinstance(ch, dict):
                channel_id = ch["id"]
                self._ensure_channel(channel_id)
                self._add_member(channel_id, node_id, role=ch.get("role", "member"))

    def get_members(self, channel_id: str) -> list[str]:
        """Return list of node_ids in channel."""

    def get_channels_for_node(self, node_id: str) -> list[str]:
        """Return channels a node belongs to."""

    async def join(self, channel_id: str, node_id: str, role: str = "member") -> ChannelMembership:
        """Add node to channel."""

    async def leave(self, channel_id: str, node_id: str) -> bool:
        """Remove node from channel."""

    def is_member(self, channel_id: str, node_id: str) -> bool:
        """Check membership."""
```

---

## 5. Module B: EventBus

### 5.1 Mục đích

EventBus là **pub/sub infrastructure** cho phép nodes phát và nhận business events
mà không cần biết ai đang lắng nghe. Thay thế cho PM-as-coordinator pattern.

### 5.2 Event data model

```python
@dataclass
class Event:
    event_id: str           # uuid4
    event_type: str         # dot-notation: "artifact.written", "test.failed"
                            # convention: "<domain>.<verb>" hoặc "<domain>.<noun>.<state>"
    channel_id: str         # "node-cluster-1", "global", hoặc "direct:<node_id>"
    source_node: str        # emitter node_id
    payload: dict           # arbitrary data — không có schema enforcement
    timestamp: float        # unix timestamp
    correlation_id: str | None = None   # link events trong cùng workflow
    reply_to: str | None = None         # node_id muốn nhận reply

# Event type conventions:
# artifact.written      — một file artifact được tạo ra
# artifact.updated      — artifact được cập nhật
# task.started          — một task bắt đầu
# task.completed        — task hoàn thành
# task.failed           — task thất bại
# test.passed           — test suite pass
# test.failed           — test suite fail (payload: errors, attempt)
# review.approved       — code review approved
# review.changes_requested — cần fix trước khi approve
# participant.input_required  — cần user input (payload: question, choices, timeout_action)
# participant.answered        — user đã trả lời
# cluster.started       — project mới bắt đầu
# cluster.completed     — project done
# node.idle             — node không có việc làm (level-1 self-report)
# node.busy             — node đang xử lý job
```

### 5.3 Subscription data model

```python
@dataclass
class Subscription:
    sub_id: str                  # uuid4, returned to subscriber
    subscriber_node: str         # node_id sẽ được notify
    callback_action: str         # action invoke trên subscriber_node khi event match
    callback_params_template: dict = field(default_factory=dict)
                                 # params merge với event payload. Use {event.*} placeholders
    
    # Matching rules — ALL conditions must match
    channel_id: str = "global"   # channel filter; "global" matches all
    event_type_pattern: str = "*"  # exact: "test.failed"; wildcard: "test.*"; all: "*"
    source_node: str | None = None  # None = accept from any source
    payload_filter: dict | None = None  # {key: value} — payload must contain these
    
    # Delivery config
    debounce_seconds: float = 0.0  # ignore duplicate events within N seconds
    max_deliveries: int | None = None   # None = unlimited; N = unsubscribe after N deliveries
    
    # Metadata
    created_at: float = field(default_factory=time.time)
    created_by: str = ""
    description: str = ""         # human-readable purpose
```

**Pattern matching rules:**
- `"test.failed"` — exact match
- `"test.*"` — prefix match: matches `test.failed`, `test.passed`, `test.timeout`
- `"*.failed"` — suffix match: matches `test.failed`, `deploy.failed`, `build.failed`
- `"*"` — matches everything in channel

### 5.4 EventBus class

```python
# runtime/event_bus.py

class EventBus:
    """
    In-process append-only event log with subscription-based fan-out.
    
    Design decisions:
    - In-process (no external broker): zero external dependencies, consistent
      với GNOT philosophy. Trade-off: không durable across restart.
    - Append-only log: full audit trail, replay capability, debugging.
    - Fan-out via HTTP callback: deliver event → call POST /action on subscriber node.
      Subscriber node nhận event như một normal action execution.
    - Async fan-out: emit() returns immediately, deliveries happen in background.
    - Failed delivery: retry 3x with exponential backoff, then dead-letter.
    
    Thread-safety: asyncio.Lock on subscriptions dict; event log uses asyncio.Queue
    for fan-out worker.
    """

    MAX_EVENT_LOG_SIZE: int = 10_000   # configurable via node.yaml
    DELIVERY_RETRY_COUNT: int = 3
    DELIVERY_TIMEOUT_SECONDS: float = 10.0

    def __init__(
        self,
        node_id: str,
        channel_registry: ChannelRegistry,
        gateway_router: GatewayRouter,
        max_log_size: int = MAX_EVENT_LOG_SIZE,
    ) -> None:
        self._node_id = node_id
        self._channels = channel_registry
        self._router = gateway_router
        self._event_log: deque[Event] = deque(maxlen=max_log_size)
        self._subscriptions: dict[str, Subscription] = {}  # sub_id → Subscription
        self._lock = asyncio.Lock()
        self._delivery_queue: asyncio.Queue[tuple[Event, Subscription]] = asyncio.Queue()
        self._delivery_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start background delivery worker."""
        self._delivery_task = asyncio.create_task(self._delivery_worker())

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._delivery_task:
            self._delivery_task.cancel()

    async def emit(self, event: Event) -> int:
        """
        Publish event to bus.
        
        1. Append to event log.
        2. Find matching subscriptions (channel + pattern + filter).
        3. Enqueue deliveries for background worker.
        4. Return count of matched subscriptions.
        """

    async def subscribe(self, sub: Subscription) -> str:
        """Register subscription. Returns sub_id."""

    async def unsubscribe(self, sub_id: str) -> bool:
        """Remove subscription. Returns True if found."""

    async def get_events(
        self,
        channel_id: str | None = None,
        event_type: str | None = None,
        source_node: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[Event]:
        """Replay events from log with optional filters."""

    def _match_subscription(self, event: Event, sub: Subscription) -> bool:
        """Check if event matches subscription filters."""
        # 1. channel: event.channel_id must match sub.channel_id (or sub = "global")
        # 2. event_type: pattern matching
        # 3. source_node: exact match or None
        # 4. payload_filter: all k/v must be present in event.payload

    async def _delivery_worker(self) -> None:
        """
        Background task: dequeue (event, sub) pairs and deliver.
        Delivery = POST /action to subscriber_node with callback_action.
        Retry 3x with backoff on failure.
        """

    async def _deliver(self, event: Event, sub: Subscription) -> bool:
        """
        Deliver one event to one subscriber.
        
        Constructs ActionRequest:
          target_node_id = sub.subscriber_node
          action = sub.callback_action
          params = merge(sub.callback_params_template, {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "channel_id": event.channel_id,
            "source_node": event.source_node,
            "payload": event.payload,
            "timestamp": event.timestamp,
            "correlation_id": event.correlation_id,
          })
        """
```

### 5.5 HTTP Endpoints

#### `POST /emit`
Publish một event lên bus.

```
Request:
{
  "event_type": "test.failed",
  "channel_id": "node-cluster-1",
  "payload": {
    "project_id": "proj-abc123",
    "failures": 3,
    "errors": ["AssertionError in test_create_todo", "..."],
    "attempt": 2
  },
  "correlation_id": "run-xyz789",    // optional
  "reply_to": "pm-node"              // optional
}

Response 200:
{
  "event_id": "evt-550e8400-e29b...",
  "matched_subscriptions": 2,
  "deliveries_queued": 2
}
```

#### `POST /channels/{channel_id}/emit`
Shorthand: emit với `channel_id` đã xác định.

```
Request: same as POST /emit nhưng không cần channel_id trong body
```

#### `POST /subscribe`
Đăng ký nhận events.

```
Request:
{
  "subscriber_node": "architect-node",
  "callback_action": "on_stories_ready",
  "callback_params_template": {
    "workspace": "/tmp/devteam/{payload.project_id}"
  },
  "channel_id": "node-cluster-1",
  "event_type_pattern": "artifact.written",
  "payload_filter": {"artifact": "user-stories"},
  "debounce_seconds": 2.0,
  "description": "Architect wakes up when analyst writes user-stories.md"
}

Response 200:
{
  "sub_id": "sub-7c3f8a...",
  "subscriber_node": "architect-node",
  "event_type_pattern": "artifact.written",
  "channel_id": "node-cluster-1"
}
```

#### `DELETE /subscriptions/{sub_id}`
Hủy subscription.

#### `GET /subscriptions`
List subscriptions. Optional filters: `?channel_id=node-cluster-1&node_id=architect-node`

```
Response 200:
{
  "subscriptions": [
    {
      "sub_id": "sub-7c3f8a...",
      "subscriber_node": "architect-node",
      "callback_action": "on_stories_ready",
      "event_type_pattern": "artifact.written",
      "channel_id": "node-cluster-1",
      "created_at": 1741392000.0
    }
  ]
}
```

#### `GET /events`
Replay event log với filters.

```
Query params:
  channel_id=node-cluster-1
  event_type=test.*         (pattern)
  source_node=test-node
  since=1741392000.0        (unix timestamp)
  limit=50                  (default 100, max 1000)

Response 200:
{
  "events": [...],
  "count": 42,
  "oldest_available": 1741380000.0
}
```

---

## 6. Module C: Scheduler

### 6.1 Ba cấp độ autonomy

v6.0 định nghĩa ba cấp độ autonomy cho mỗi node, từ thấp đến cao:

```
Level 1 — Condition-based self-check:
  Node định kỳ tự kiểm tra trạng thái nội bộ.
  Nếu thỏa điều kiện → tự trigger action.
  Không cần external trigger.
  
  Ví dụ: analyst-node tự check mỗi 60s:
  "Có project nào có requirement.txt nhưng chưa có user-stories.md?"
  → Nếu có: tự bắt đầu phân tích.

Level 2 — Pull polling (đã có, được tăng cường):
  Node định kỳ poll gateway queue.
  Nếu có job → claim và execute.
  v6.0 enhancement: adaptive interval, priority lanes.

Level 3 — Event-triggered (mới hoàn toàn):
  Node đăng ký subscription trên EventBus.
  Khi event match → scheduler trigger action tương ứng.
  Hoàn toàn reactive, không cần polling.
```

### 6.2 ScheduleEntry data model

```python
@dataclass
class ScheduleEntry:
    schedule_id: str              # uuid4
    trigger_type: Literal["condition", "cron", "event", "once"]
    target_node: str              # node_id để chạy run_action (thường là self)
    run_action: str               # action invoke khi trigger fires
    run_params: dict = field(default_factory=dict)  # static params cho run_action
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    description: str = ""

    # --- trigger_type == "condition" ---
    check_action: str | None = None     # action trả về {should_run: bool}
    check_params: dict = field(default_factory=dict)
    check_interval_seconds: int = 60    # poll condition every N seconds
    # Nếu check_action return {should_run: True, ...extra}
    # thì extra được merge vào run_params

    # --- trigger_type == "cron" ---
    cron_expression: str | None = None  # "0 9 * * 1-5" (standard 5-field cron)
    # Timezone: UTC. Cron parsing via lightweight in-process library (không deps).

    # --- trigger_type == "event" ---
    on_event_type: str | None = None    # pattern: "test.failed", "test.*", "*"
    on_channel: str = "global"
    on_source_node: str | None = None   # None = any
    on_payload_filter: dict | None = None
    # Event trigger tự động tạo Subscription trong EventBus
    # subscription_id được lưu để cleanup khi entry bị xóa

    # --- trigger_type == "once" ---
    run_at: float | None = None         # unix timestamp để chạy 1 lần

    # --- shared options ---
    max_concurrent: int = 1             # max concurrent executions của entry này
    skip_if_running: bool = True        # bỏ qua trigger nếu đang có execution running
    timeout_seconds: int = 300          # max time cho một execution
    retry_on_failure: int = 0           # số lần retry nếu run_action fail
    
    # Runtime state (không persist)
    last_triggered_at: float | None = None
    last_result: dict | None = None
    execution_count: int = 0
    error_count: int = 0
```

### 6.3 Condition trigger — Level 1 deep dive

Level 1 là cơ chế quan trọng nhất và tinh tế nhất. Node tự định nghĩa một
`check_action` — một lightweight action chỉ kiểm tra điều kiện:

```python
# Ví dụ: analyst-node/actions/analyst_self_check.py
async def run(params: dict, context: dict) -> dict:
    import os, glob
    
    workspace_base = params.get("workspace_base", "/tmp/devteam")
    
    # Find projects where requirement.txt exists but user-stories.md doesn't
    req_files = glob.glob(f"{workspace_base}/*/requirement.txt")
    for req_path in req_files:
        project_dir = os.path.dirname(req_path)
        stories_path = os.path.join(project_dir, "user-stories.md")
        
        if not os.path.exists(stories_path):
            # Check if someone else is already working on it
            lock_path = os.path.join(project_dir, ".analyst_lock")
            if not os.path.exists(lock_path):
                return {
                    "should_run": True,
                    "project_id": os.path.basename(project_dir),
                    "workspace": project_dir,
                    "reason": "requirement.txt exists but user-stories.md missing"
                }
    
    return {"should_run": False}
```

Scheduler gọi `check_action` định kỳ. Nếu `should_run: True`:
- Extra fields từ response (`project_id`, `workspace`) được merge vào `run_params`
- `run_action` được trigger với merged params
- Lock file được tạo để prevent double-execution

**Anti-thundering-herd:** Khi có nhiều projects cần xử lý, `check_action` chỉ
trả về một project tại một thời điểm. Scheduler sẽ trigger lại check sau khi
execution xong — chạy từng cái một, không batch.

### 6.4 Level 2 enhancement: Adaptive polling

WorkerAgent hiện tại poll fixed 5 giây. v6.0 thêm adaptive behavior:

```
Adaptive poll interval algorithm:
  - Base interval: poll_interval_seconds (từ config, default 5s)
  - Nếu poll trả về job: interval → base (stay responsive)
  - Nếu poll trả về empty: interval × backoff_multiplier (default 1.5)
  - Maximum interval: poll_interval_max_seconds (default 60s)
  - Reset về base khi nhận được job bất kỳ
  
  Ví dụ với base=5s, max=60s, multiplier=1.5:
  idle × 1: 5s
  idle × 2: 7.5s
  idle × 3: 11.25s
  ...
  idle × 7: 57s (cap ở 60s)
  → job received: reset về 5s
```

Giảm ~70% unnecessary HTTP calls khi cluster idle.

Priority lanes (P3, xem Section 10):
```yaml
# node.yaml v6.0
poll_interval_seconds: 5
poll_interval_max_seconds: 60
poll_backoff_multiplier: 1.5

# Priority lanes (P3)
job_priority_lanes:
  critical: 1    # poll every 1s regardless of backoff
  normal: 5
  low: 30
```

### 6.5 Scheduler class

```python
# runtime/scheduler.py

class Scheduler:
    """
    Manages scheduled triggers at three autonomy levels.
    
    Each ScheduleEntry runs in its own asyncio task.
    Condition entries: run check_action loop, trigger run_action when condition met.
    Cron entries: sleep until next cron tick, trigger run_action.
    Event entries: register Subscription in EventBus, run_action on delivery.
    Once entries: sleep until run_at, trigger once, then self-deactivate.
    """

    def __init__(
        self,
        node_id: str,
        executor: ActionExecutor,
        event_bus: EventBus,
    ) -> None:
        self._node_id = node_id
        self._executor = executor
        self._event_bus = event_bus
        self._entries: dict[str, ScheduleEntry] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def add(self, entry: ScheduleEntry) -> str:
        """Register entry and start its trigger loop. Returns schedule_id."""

    async def remove(self, schedule_id: str) -> bool:
        """Cancel trigger loop and remove entry."""

    async def list_entries(self) -> list[ScheduleEntry]:
        """Return all registered entries with runtime state."""

    async def _run_condition_loop(self, entry: ScheduleEntry) -> None:
        """Loop: sleep(check_interval) → call check_action → if True: call run_action."""

    async def _run_cron_loop(self, entry: ScheduleEntry) -> None:
        """Loop: compute next tick → sleep → call run_action → repeat."""

    async def _run_event_trigger(self, entry: ScheduleEntry) -> None:
        """Register EventBus subscription → run_action on delivery."""

    async def _execute_run_action(self, entry: ScheduleEntry, extra_params: dict) -> None:
        """Execute run_action with merged params. Handle concurrency, timeout, retry."""
```

### 6.6 HTTP Endpoints

#### `POST /schedule`
Register a scheduled trigger.

```
Request:
{
  "trigger_type": "condition",
  "target_node": "analyst-node",
  "run_action": "start_analysis",
  "run_params": {"workspace_base": "/tmp/devteam"},
  "check_action": "analyst_self_check",
  "check_params": {"workspace_base": "/tmp/devteam"},
  "check_interval_seconds": 60,
  "description": "Analyst auto-start when new project detected"
}

// OR cron:
{
  "trigger_type": "cron",
  "target_node": "pm-node",
  "run_action": "run_standup",
  "cron_expression": "0 9 * * 1-5",
  "description": "Daily standup at 9am weekdays"
}

// OR event:
{
  "trigger_type": "event",
  "target_node": "architect-node",
  "run_action": "on_stories_ready",
  "on_event_type": "artifact.written",
  "on_channel": "node-cluster-1",
  "on_payload_filter": {"artifact": "user-stories"},
  "description": "Architect wakes up when analyst finishes"
}

Response 200:
{
  "schedule_id": "sched-abc123",
  "trigger_type": "condition",
  "enabled": true
}
```

#### `DELETE /schedule/{schedule_id}`
Cancel và remove một schedule entry.

#### `GET /schedule`
List tất cả schedule entries với runtime state.

```
Response 200:
{
  "entries": [
    {
      "schedule_id": "sched-abc123",
      "trigger_type": "condition",
      "run_action": "start_analysis",
      "enabled": true,
      "execution_count": 3,
      "last_triggered_at": 1741392000.0,
      "last_result": {"should_run": false}
    }
  ]
}
```

#### `POST /schedule/{schedule_id}/trigger`
Manually trigger một schedule entry (for testing/debugging).

#### `PATCH /schedule/{schedule_id}`
Enable/disable hoặc update params.

---

## 7. `node.yaml` schema — v6.0 additions

```yaml
# ── v6.0: Channel membership ─────────────────────────────────────────────
channels:
  - node-cluster-1                   # string form: join as "member"
  - id: ops-alerts               # object form
    role: observer
  - id: all-hands
    role: member

# ── v6.0: EventBus config ────────────────────────────────────────────────
event_bus:
  enabled: true                  # default: true
  max_log_size: 10000            # events kept in memory
  delivery_timeout_seconds: 10   # per-delivery HTTP timeout
  delivery_retry_count: 3        # retry failed deliveries
  delivery_retry_backoff: 2.0    # exponential backoff multiplier

# ── v6.0: Scheduler config ───────────────────────────────────────────────
scheduler:
  enabled: true                  # default: true

# Static schedule entries (equivalent to POST /schedule on startup)
schedule:
  - trigger_type: condition
    run_action: start_analysis
    check_action: analyst_self_check
    check_interval_seconds: 60
    description: Auto-start analysis when new project detected

  - trigger_type: cron
    run_action: run_standup
    cron_expression: "0 9 * * 1-5"
    description: Daily standup

# ── v6.0: Adaptive polling enhancement ──────────────────────────────────
poll_interval_seconds: 5         # existing field (default 5)
poll_interval_max_seconds: 60    # NEW: backoff ceiling
poll_backoff_multiplier: 1.5     # NEW: backoff multiplier

# ── v6.0: Event bus persistence (P3) ────────────────────────────────────
# event_bus_persist_path: /tmp/gnot-events/   # if set, events written to JSONL
```

---

## 8. New models (Pydantic)

Thêm vào `runtime/models.py`:

```python
# ── Channel models ────────────────────────────────────────────────────────

class ChannelMemberInfo(BaseModel):
    node_id: str
    role: str = "member"
    joined_at: float

class ChannelInfo(BaseModel):
    channel_id: str
    display_name: str = ""
    member_count: int
    my_role: str
    created_at: float

class ChannelListResponse(BaseModel):
    channels: list[ChannelInfo]

class ChannelMembersResponse(BaseModel):
    channel_id: str
    members: list[ChannelMemberInfo]

class JoinChannelRequest(BaseModel):
    role: str = "member"

class JoinChannelResponse(BaseModel):
    channel_id: str
    node_id: str
    role: str
    joined_at: float

# ── Event models ──────────────────────────────────────────────────────────

class EmitRequest(BaseModel):
    event_type: str
    channel_id: str = "global"
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    reply_to: str | None = None

class EmitResponse(BaseModel):
    event_id: str
    matched_subscriptions: int
    deliveries_queued: int

class EventRecord(BaseModel):
    event_id: str
    event_type: str
    channel_id: str
    source_node: str
    payload: dict[str, Any]
    timestamp: float
    correlation_id: str | None = None

class EventLogResponse(BaseModel):
    events: list[EventRecord]
    count: int
    oldest_available: float | None

class SubscribeRequest(BaseModel):
    subscriber_node: str
    callback_action: str
    callback_params_template: dict[str, Any] = Field(default_factory=dict)
    channel_id: str = "global"
    event_type_pattern: str = "*"
    source_node: str | None = None
    payload_filter: dict[str, Any] | None = None
    debounce_seconds: float = 0.0
    max_deliveries: int | None = None
    description: str = ""

class SubscribeResponse(BaseModel):
    sub_id: str
    subscriber_node: str
    event_type_pattern: str
    channel_id: str

class SubscriptionInfo(BaseModel):
    sub_id: str
    subscriber_node: str
    callback_action: str
    event_type_pattern: str
    channel_id: str
    debounce_seconds: float
    created_at: float
    description: str

class SubscriptionListResponse(BaseModel):
    subscriptions: list[SubscriptionInfo]

# ── Scheduler models ──────────────────────────────────────────────────────

class ScheduleRequest(BaseModel):
    trigger_type: Literal["condition", "cron", "event", "once"]
    target_node: str
    run_action: str
    run_params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    enabled: bool = True
    
    # condition
    check_action: str | None = None
    check_params: dict[str, Any] = Field(default_factory=dict)
    check_interval_seconds: int = 60
    
    # cron
    cron_expression: str | None = None
    
    # event
    on_event_type: str | None = None
    on_channel: str = "global"
    on_source_node: str | None = None
    on_payload_filter: dict[str, Any] | None = None
    
    # once
    run_at: float | None = None
    
    # shared
    max_concurrent: int = 1
    skip_if_running: bool = True
    timeout_seconds: int = 300
    retry_on_failure: int = 0

class ScheduleResponse(BaseModel):
    schedule_id: str
    trigger_type: str
    enabled: bool

class ScheduleEntryInfo(BaseModel):
    schedule_id: str
    trigger_type: str
    run_action: str
    target_node: str
    enabled: bool
    execution_count: int
    error_count: int
    last_triggered_at: float | None
    description: str

class ScheduleListResponse(BaseModel):
    entries: list[ScheduleEntryInfo]
```

---

## 9. Health endpoint — v6.0 additions

`GET /health` response được mở rộng:

```json
{
  "node_id": "pm-node",
  "status": "healthy",
  "uptime_seconds": 3600,
  "actions_loaded": 8,
  "jobs_active": 2,
  "queue_depths": {"analyst-node": 0, "dev-node": 1},
  
  "v6_extensions": {
    "channels": ["node-cluster-1", "all-hands"],
    "active_subscriptions": 3,
    "event_log_size": 142,
    "schedule_entries": 2,
    "schedule_active": 2
  }
}
```

---

## 10. Implementation plan — Priority tiers

### P0 — Unblock AI Dev Team (smallest viable increment)

**Scope:** Channel membership + channel-scoped emit + minimal event log

**New files:** `runtime/channel_registry.py`

**Modified files:** `runtime/config.py`, `runtime/server.py`, `runtime/models.py`

**New endpoints:** `GET /channels`, `POST /channels/{id}/emit`, `GET /events` (no subscription, just log)

**Config additions:** `channels:` list in node.yaml

**Không cần:** Full subscription system, Scheduler, adaptive polling

**Outcome:** Agents có thể emit events scoped to channel. PM có thể read event log.
Không có reactive callbacks yet — LLM vẫn coordinates nhưng có audit trail.

**Estimated scope:** ~300 LOC, 1 new file, 2 modified files

---

### P1 — Event-Driven Coordination

**Scope:** Full EventBus với subscribe/emit/deliver + event-triggered Scheduler

**New files:** `runtime/event_bus.py`, `runtime/scheduler.py` (event trigger type only)

**Modified files:** `runtime/server.py`, `runtime/models.py`, `runtime/config.py`

**New endpoints:** `POST /emit`, `POST /subscribe`, `DELETE /subscriptions/{id}`, `GET /subscriptions`, `POST /schedule` (event type), `DELETE /schedule/{id}`, `GET /schedule`

**Outcome:** Agents tự phối hợp qua events. Architect tự wake up khi analyst xong.
Dev tự wake up khi design locked. PM chỉ watch.

**Dependency:** Requires P0 (ChannelRegistry)

**Estimated scope:** ~600 LOC, 2 new files

---

### P2 — Level 1 Autonomy + Cron

**Scope:** Condition-based self-check + cron trigger + adaptive polling

**New files:** Không có (extends Scheduler từ P1)

**Modified files:** `runtime/scheduler.py`, `runtime/worker_agent.py`, `runtime/config.py`

**New endpoints:** `POST /schedule` (condition + cron types), `POST /schedule/{id}/trigger`, `PATCH /schedule/{id}`

**Outcome:** Nodes hoàn toàn self-starting. Không cần bất kỳ external kickoff nào.
Analyst tự phát hiện project mới và bắt đầu. PM tự chạy standup mỗi sáng.

**Dependency:** Requires P1

**Estimated scope:** ~400 LOC

---

### P3 — Production Hardening

**Scope:** Persistence, priority lanes, human checkpoint, session persistence

**Sub-items:**

| Sub-item | Description | Scope |
|----------|-------------|-------|
| P3-a: Event persistence | Write event log to JSONL file, survive restart | ~100 LOC |
| P3-b: Session persistence | SQLite backend cho ConversationStore | ~150 LOC |
| P3-c: Priority lanes | JobQueue priority field + poll by priority | ~200 LOC |
| P3-d: Human checkpoint | `participant.input_required` event + pause/resume mechanism | ~300 LOC |
| P3-e: Dead-letter queue | Failed event deliveries → separate queue + retry API | ~150 LOC |

**P3-d Human checkpoint detail:**

```python
# runtime/checkpoint_manager.py (P3-d)

class CheckpointManager:
    """
    Pause/resume mechanism for human-in-the-loop checkpoints.
    
    When a node emits participant.input_required:
    1. CheckpointManager creates a Checkpoint record with timeout
    2. The originating workflow is suspended (coroutine parked)
    3. Human interface (Claude Web / Telegram / REST) receives notification
    4. Human answers → POST /checkpoints/{id}/respond
    5. Workflow resumes with the answer injected as context
    
    Timeout handling: if no response within timeout_seconds,
    execute timeout_action (default: "use_default" or "cancel")
    """

class Checkpoint(BaseModel):
    checkpoint_id: str
    project_id: str
    source_node: str
    question: str
    context: str = ""
    choices: list[str] | None = None  # if multiple choice
    default_answer: str | None = None
    timeout_seconds: int = 3600
    timeout_action: Literal["use_default", "cancel", "escalate"] = "use_default"
    created_at: float
    status: Literal["pending", "answered", "timed_out", "cancelled"] = "pending"
    answer: str | None = None
    resolved_at: float | None = None
    resolved_by: str | None = None   # node_id or "human"
```

---

## 11. Files thay đổi tổng hợp (all tiers)

| File | Tier | Type | Description |
|------|------|------|-------------|
| `runtime/channel_registry.py` | P0 | NEW | ChannelRegistry class |
| `runtime/event_bus.py` | P1 | NEW | EventBus class |
| `runtime/scheduler.py` | P1/P2 | NEW | Scheduler class (event + condition + cron) |
| `runtime/checkpoint_manager.py` | P3-d | NEW | Human checkpoint pause/resume |
| `runtime/config.py` | P0 | MODIFY | channels, event_bus, scheduler, poll adaptive fields |
| `runtime/models.py` | P0 | MODIFY | Channel/Event/Subscribe/Schedule Pydantic models |
| `runtime/server.py` | P0 | MODIFY | Wire ChannelRegistry; add new endpoints |
| `runtime/worker_agent.py` | P2 | MODIFY | Adaptive poll interval |
| `runtime/job_manager.py` | P3-c | MODIFY | Priority field on jobs |
| `runtime/job_queue.py` | P3-c | MODIFY | Priority lanes + poll-by-priority |
| `runtime/conversation_store.py` | P3-b | MODIFY | SQLite backend option |
| `tests/test_v60_channels.py` | P0 | NEW | Channel registry tests |
| `tests/test_v60_eventbus.py` | P1 | NEW | EventBus emit/subscribe/deliver tests |
| `tests/test_v60_scheduler.py` | P1/P2 | NEW | Scheduler trigger tests |
| `docs/worklog/SPECS_V6.0.md` | — | NEW | This document |
| `docs/worklog/WORKLOG_V6.0.md` | — | NEW | Implementation worklog |

---

## 12. Backward compatibility

v6.0 là **additive only** — không có breaking changes với v5.13b:

- Tất cả existing endpoints (`/action`, `/intent`, `/capabilities`, etc.) không thay đổi
- `node.yaml` fields mới đều optional với sensible defaults
- Nodes không có `channels:` config vẫn hoạt động bình thường (no channel membership)
- EventBus disabled nếu `event_bus.enabled: false`
- Scheduler disabled nếu `scheduler.enabled: false`
- WorkerAgent vẫn hoạt động với fixed poll interval nếu `poll_interval_max_seconds` không set

---

## 13. Trạng thái hệ thống sau v6.0 (full)

```
Communication:
  ✅ Point-to-point action (v5.x)
  ✅ Channel-scoped broadcast (v6.0 P0)
  ✅ Event pub/sub (v6.0 P1)
  ✅ Human checkpoint pause/resume (v6.0 P3-d)

Autonomy:
  ✅ Level 2: Pull polling (v5.3, enhanced P2)
  ✅ Level 1: Self-check condition trigger (v6.0 P2)
  ✅ Level 3: Event-triggered reactive (v6.0 P1)
  ✅ Cron / time-based trigger (v6.0 P2)

Observability:
  ✅ Event audit log (v6.0 P0/P1)
  ✅ Event persistence across restart (v6.0 P3-a)
  ✅ Schedule entry runtime state (v6.0 P1/P2)

Reliability:
  ✅ Event delivery retry (v6.0 P1)
  ✅ Dead-letter queue (v6.0 P3-e)
  ✅ Priority job lanes (v6.0 P3-c)
  ✅ Persistent session memory (v6.0 P3-b)
```

---

*Spec: SPECS_V6.0.md | Mesh Runtime v6.0 | Repository: ai-infra-runtime-v2*
