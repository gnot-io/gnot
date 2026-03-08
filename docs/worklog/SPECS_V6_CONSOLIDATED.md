# Generative Node Orchestration Technology (GNOT)
## Consolidated Architecture Specification v6.0
### Event-Driven · Multi-Cluster · Autonomous Agents · Human-in-the-Loop · MCP Native

**Organization:** gnot-io  
**Repository:** github.com/gnot-io/gnot  
**Base version:** v5.13b (current implementation)  
**Target version:** v6.0 (consolidated from design specs v6.0–v6.7)  
**Status:** Final design — ready for implementation  
**Date:** 2026-03-08

---

## Mục lục

1. [Executive Summary](#1-executive-summary)
2. [Phạm vi và mục tiêu](#2-phạm-vi-và-mục-tiêu)
3. [Kiến trúc tổng quan v6](#3-kiến-trúc-tổng-quan-v6)
4. [Feature Area A: EventBus & Pub/Sub](#4-feature-area-a-eventbus--pubsub)
5. [Feature Area B: Scheduler & Autonomy Levels](#5-feature-area-b-scheduler--autonomy-levels)
6. [Feature Area C: Multi-Gateway Membership](#6-feature-area-c-multi-gateway-membership)
7. [Feature Area D: Task Suspension & Resumption](#7-feature-area-d-task-suspension--resumption)
8. [Feature Area E: Self-Provisioning Clusters](#8-feature-area-e-self-provisioning-clusters)
9. [Feature Area F: External Participant Interaction](#9-feature-area-f-external-participant-interaction)
10. [Feature Area G: Multi-Channel Transport](#10-feature-area-g-multi-channel-transport)
11. [Feature Area H: Persistent Agent & Memory](#11-feature-area-h-persistent-agent--memory)
12. [Feature Area I: MCP Native Support](#12-feature-area-i-mcp-native-support)
13. [Design Review & Optimizations](#13-design-review--optimizations)
14. [Consolidated node.yaml Schema](#14-consolidated-nodeyaml-schema)
15. [Consolidated HTTP Endpoints](#15-consolidated-http-endpoints)
16. [File Impact Matrix](#16-file-impact-matrix)
17. [Implementation Roadmap](#17-implementation-roadmap)
18. [Risk Assessment](#18-risk-assessment)
19. [Terminology Canon](#19-terminology-canon)

---

## 1. Executive Summary

GNOT v6 chuyển đổi hệ thống từ một **synchronous request/response mesh** sang một **event-driven autonomous agent platform** hỗ trợ:

- Agents tự phối hợp qua events thay vì qua PM trung gian
- Nodes tự khởi động công việc (3 cấp autonomy)
- Task suspension/resumption khi cần clarification
- Dynamic cluster provisioning từ natural language
- Multi-human participation với role-based routing
- Multi-channel transport (Web, Telegram, Webhook, API)
- Persistent memory xuyên session và restart
- Native MCP tool consumption

**Nguyên tắc thiết kế xuyên suốt:** Additive only — mọi feature mới đều opt-in, backward compatible 100% với v5.13b.

---

## 2. Phạm vi và mục tiêu

### 2.1 Từ v5.13b đến v6.0

| Dimension | v5.13b | v6.0 |
|-----------|--------|------|
| Communication | Point-to-point HTTP only | + Event pub/sub, channel-scoped broadcast |
| Coordination | PM orchestrates everything | Peer-to-peer via events, PM watches |
| Autonomy | Passive — only respond when called | 3 levels: self-check, adaptive poll, event-reactive |
| Task lifecycle | Linear (start → finish) | Suspend, resume, concurrent tasks |
| Team topology | Single gateway per node | Multi-gateway membership |
| Provisioning | Manual config per node | Automated cluster provisioning from blueprints |
| Human interface | None | Multi-participant, role-based, multi-channel |
| Memory | In-memory, lost on restart | Persistent sessions + long-term agent memory |
| External tools | GNOT actions only | + Native MCP tool consumption |

### 2.2 Những gì KHÔNG đổi

Toàn bộ v5.13b core hoạt động nguyên vẹn:

- `POST /action`, `POST /intent`, `GET /result/{id}` — unchanged
- Push/Pull delivery model — unchanged
- BGP-style route advertisement — unchanged
- Seed actions (execute_command, read_file, write_file) — unchanged
- Auth middleware, CallerPolicy, CredentialStore — unchanged
- UploadManager, SchemaValidator — unchanged

---

## 3. Kiến trúc tổng quan v6

```
┌─────────────────────────────────────────────────────────────────────┐
│                      GNOT Node Runtime v6.0                         │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    v5.13b Core (unchanged)                     │  │
│  │  GatewayRouter · NodeRegistry · JobQueue · JobManager          │  │
│  │  WorkerAgent · IntentHandler · ConversationStore                │  │
│  │  BootstrapEngine · UploadManager · CredentialStore              │  │
│  │  ActionExecutor · LLMClient · SchemaValidator                   │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│        ┌─────────────────────┼───────────────────────┐             │
│        │                     │                       │             │
│        ▼                     ▼                       ▼             │
│  ┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐   │
│  │  EventBus    │   │  Scheduler       │   │  MCPRegistry     │   │
│  │  (pub/sub)   │   │  (3-level auto)  │   │  (MCP client)    │   │
│  └──────────────┘   └──────────────────┘   └──────────────────┘   │
│        │                     │                                     │
│  ┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐   │
│  │  TaskPool    │   │  CheckpointStore │   │  AgentMemory     │   │
│  │  (suspend/   │   │  (task state     │   │  (persistent     │   │
│  │   resume)    │   │   persistence)   │   │   facts store)   │   │
│  └──────────────┘   └──────────────────┘   └──────────────────┘   │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  Extended Infrastructure                                       │  │
│  │  GatewayConnection (multi-gw) · PersistentSessionStore         │  │
│  │  ClusterOrchestrator · BlueprintStore                           │  │
│  │  ExternalParticipantRegistry · ChannelLog · InteractionRouter   │  │
│  │  TelegramTransport                                              │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. Feature Area A: EventBus & Pub/Sub

**Nguồn gốc:** v6.0 spec, điều chỉnh bởi v6.1

### 4.1 Mô hình conceptual

**v6.1 insight (thay thế v6.0):** Gateway IS the channel. Không cần ChannelRegistry riêng — NodeRegistry đã chứa member list. Channel scope = registered node scope.

EventBus là in-process append-only event log với subscription-based fan-out. Khi emit event, chỉ nodes đã register với gateway nhận được.

### 4.2 Event model

```python
@dataclass
class Event:
    event_id: str               # uuid4
    event_type: str             # dot-notation: "artifact.written", "test.failed"
    source_node: str            # emitter node_id
    payload: dict               # arbitrary data
    timestamp: float
    correlation_id: str | None = None
    reply_to: str | None = None
```

**Event type conventions:**
- `artifact.written`, `artifact.updated` — file artifacts
- `task.started`, `task.completed`, `task.failed`, `task.suspended`, `task.resumed`
- `test.passed`, `test.failed`
- `review.approved`, `review.changes_requested`
- `clarification.needed`, `clarification.answered`, `clarification.timeout`
- `participant.input_required`, `participant.answered`, `participant.commented`, `participant.tagged`
- `cluster.started`, `cluster.completed`
- `node.idle`, `node.busy`

### 4.3 Subscription model

```python
@dataclass
class Subscription:
    sub_id: str
    subscriber_node: str
    callback_action: str            # action to invoke on event match
    callback_params_template: dict  # merge with event payload
    event_type_pattern: str = "*"   # "test.failed", "test.*", "*.failed", "*"
    source_node: str | None = None
    payload_filter: dict | None = None
    debounce_seconds: float = 0.0
    max_deliveries: int | None = None
    description: str = ""
```

**Pattern matching:** Exact (`test.failed`), prefix wildcard (`test.*`), suffix wildcard (`*.failed`), all (`*`).

### 4.4 EventBus class

```python
class EventBus:
    MAX_EVENT_LOG_SIZE: int = 10_000
    DELIVERY_RETRY_COUNT: int = 3
    DELIVERY_TIMEOUT_SECONDS: float = 10.0

    async def start() -> None       # start background delivery worker
    async def stop() -> None
    async def emit(event) -> int    # publish event, return matched count
    async def subscribe(sub) -> str # returns sub_id
    async def unsubscribe(sub_id) -> bool
    async def get_events(filters) -> list[Event]
```

**Delivery mechanism:** Khi event match subscription, EventBus constructs ActionRequest targeting subscriber_node's callback_action và dispatch qua GatewayRouter. Failed deliveries retry 3x với exponential backoff.

### 4.5 HTTP Endpoints

- `POST /emit` — publish event
- `POST /subscribe` — register subscription
- `DELETE /subscriptions/{sub_id}` — cancel subscription
- `GET /subscriptions` — list subscriptions
- `GET /events` — replay event log

### 4.6 Config

```yaml
event_bus:
  enabled: true
  max_log_size: 10000
  delivery_timeout_seconds: 10
  delivery_retry_count: 3
  delivery_retry_backoff: 2.0
```

---

## 5. Feature Area B: Scheduler & Autonomy Levels

**Nguồn gốc:** v6.0 spec

### 5.1 Ba cấp autonomy

| Level | Tên | Mô tả |
|-------|-----|-------|
| 1 | Condition self-check | Node định kỳ tự kiểm tra điều kiện → tự trigger action |
| 2 | Adaptive poll (enhanced) | Pull polling với backoff khi idle, reset khi có job |
| 3 | Event-triggered | Node subscribe EventBus → reactive callback khi event match |

### 5.2 ScheduleEntry model

```python
@dataclass
class ScheduleEntry:
    schedule_id: str
    trigger_type: Literal["condition", "cron", "event", "once"]
    target_node: str
    run_action: str
    run_params: dict
    enabled: bool = True
    description: str = ""

    # condition trigger
    check_action: str | None = None
    check_params: dict
    check_interval_seconds: int = 60

    # cron trigger
    cron_expression: str | None = None  # 5-field cron

    # event trigger
    on_event_type: str | None = None
    on_channel: str = "global"
    on_payload_filter: dict | None = None

    # once trigger
    run_at: float | None = None

    # shared options
    max_concurrent: int = 1
    skip_if_running: bool = True
    timeout_seconds: int = 300
    retry_on_failure: int = 0
```

### 5.3 Adaptive polling (Level 2 enhancement)

```
Base interval: poll_interval_seconds (default 5s)
Empty poll → interval × backoff_multiplier (default 1.5)
Maximum: poll_interval_max_seconds (default 60s)
Job received → reset to base
```

Giảm ~70% unnecessary HTTP calls khi cluster idle.

### 5.4 HTTP Endpoints

- `POST /schedule` — register trigger
- `DELETE /schedule/{id}` — cancel trigger
- `GET /schedule` — list entries
- `POST /schedule/{id}/trigger` — manual trigger (debug)
- `PATCH /schedule/{id}` — enable/disable, update

### 5.5 Config

```yaml
scheduler:
  enabled: true

schedule:  # static schedule entries (loaded at startup)
  - trigger_type: condition
    run_action: start_analysis
    check_action: analyst_self_check
    check_interval_seconds: 60

poll_interval_seconds: 5
poll_interval_max_seconds: 60
poll_backoff_multiplier: 1.5
```

---

## 6. Feature Area C: Multi-Gateway Membership

**Nguồn gốc:** v6.1 spec (thay thế v6.0's separate channel model)

### 6.1 Core concept

**Gateway = Channel Authority.** Register with gateway = join that gateway's channel as full member. Không có guest/native distinction — full member equality.

### 6.2 Multi-gateway node

Node có thể register với nhiều gateways đồng thời:

```yaml
# dev-B node.yaml
gateway_node_id: gateway-B      # primary
gateway_address: https://gateway-b.vietml.com
auth_token: tok-dev-b-on-b

additional_gateways:
  - address: https://gateway-a.vietml.com
    auth_token: tok-dev-b-on-a
```

### 6.3 GatewayConnection (refactor từ WorkerAgent)

Extract per-gateway connection logic thành `GatewayConnection` class. WorkerAgent trở thành orchestrator quản lý N connections.

```python
class GatewayConnection:
    """Manages register + heartbeat + poll lifecycle for ONE gateway."""
    async def start() -> None
    async def stop() -> None

class WorkerAgent:
    """Manages multiple GatewayConnection instances."""
    def __init__(self, config, executor, ...):
        self._connections: list[GatewayConnection] = []
        # Build primary + additional connections
```

### 6.4 Registration policy

```yaml
registration_policy: open  # open | whitelist | invite_only
```

- `whitelist` (default): only `trusted_nodes` accepted — v5.x behavior
- `open`: any authenticated node accepted
- `invite_only`: requires invite token (future)

### 6.5 ChannelRegistry — thin wrapper

```python
class ChannelRegistry:
    """Thin view of NodeRegistry from channel perspective. ~30 LOC."""
    def get_members(self) -> list[str]     # delegates to NodeRegistry
    def is_member(self, node_id) -> bool
    def get_channel_id(self) -> str        # = gateway's node_id
```

---

## 7. Feature Area D: Task Suspension & Resumption

**Nguồn gốc:** v6.2 spec

### 7.1 Problem

IntentHandler hiện tại là linear coroutine — không thể pause, park state, resume. Node chỉ chạy 1 task tại một thời điểm.

### 7.2 New components

**CheckpointStore** — Persistent store cho suspended task state:

```python
@dataclass
class TaskCheckpoint:
    checkpoint_id: str
    task_id: str
    session_id: str
    node_id: str
    original_prompt: str
    messages: list[dict]        # full LLM message history tại điểm suspend
    turn_count: int
    suspended_at: float
    suspension_reason: str
    pending_question: str
    pending_question_id: str    # = correlation_id cho answer event
    asked_node: str
    timeout_seconds: int = 86400
    timeout_action: str = "use_assumption"
    assumption: str = ""
    status: str = "suspended"   # suspended | answered | resumed | timed_out
```

Storage: JSONL file per node.

**TaskPool** — Concurrent task execution manager:

```python
class TaskPool:
    """max_active concurrent + unlimited suspended tasks."""
    async def start_task(task_id, prompt, session_id) -> None
    async def suspend_task(task_id, question, ask_node, ...) -> str  # returns question_id
    async def resume_task(question_id, answer) -> None
    async def get_status() -> dict
```

### 7.3 JobStatus extension

```python
class JobStatus(str, Enum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    SUSPENDED = "suspended"     # NEW: blocked waiting for input
    RESUMING = "resuming"       # NEW: answer received, resuming
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"     # NEW: suspension timeout
```

### 7.4 IntentHandler changes

New special action `suspend_and_ask`:

```python
# LLM calls:
mesh_action("self", "suspend_and_ask", {
    "question": "Should null user throw exception or return empty?",
    "ask_node": "architect-A",     # agent-to-agent
    # OR "target_role": "pm",      # agent-to-human (v6.4 addition)
    "timeout_seconds": 7200,
    "assumption": "throw UserNotFoundException"
})
```

IntentHandler saves checkpoint, sets SUSPENDED, emits `clarification.needed`.

### 7.5 Answer routing

Scheduler subscribes `clarification.answered` → `handle_clarification_answer` action → `TaskPool.resume_task()`.

### 7.6 Standard event vocabulary

```
clarification.needed       — agent blocked, needs answer
clarification.answered     — answer ready
clarification.timeout      — no answer, using assumption
participant.input_required — needs human judgment
participant.answered       — human replied
```

### 7.7 Config

```yaml
task_pool:
  enabled: true
  max_active_tasks: 3

checkpoint_store:
  enabled: true
  path: /tmp/gnot-checkpoints/
  default_timeout_seconds: 86400
  sweep_interval_seconds: 3600
```

### 7.8 HTTP Endpoints

- `GET /tasks` — list active + suspended tasks
- `GET /tasks/{task_id}/checkpoint` — view checkpoint
- `POST /tasks/{task_id}/answer` — manually inject answer
- `DELETE /tasks/{task_id}` — cancel suspended task

---

## 8. Feature Area E: Self-Provisioning Clusters

**Nguồn gốc:** v6.3 spec

### 8.1 Core insight

Seed node + LLM config + `/intent` + `/bootstrap` = orchestrator đã hoàn chỉnh. Không cần module provisioner mới — chỉ cần convention, data structures, và BootstrapRequest mở rộng.

### 8.2 Blueprint system

```
node-0/blueprints/
├── INDEX.yaml                 # blueprint catalog
├── roles/
│   ├── pm.md
│   ├── analyst.md
│   ├── architect.md
│   ├── developer.md
│   ├── developer-python.md
│   ├── tester.md
│   └── reviewer.md
├── teams/
│   ├── standard-cluster.yaml  # 6-role team composition
│   └── minimal-cluster.yaml   # lean 4-role team
└── generated/                 # LLM-generated, saved for reuse
```

### 8.3 BootstrapRequest v6.0 (extended)

BootstrapRequest mở rộng để support ALL v6.x config fields: gateway topology, LLM config, EventBus, Scheduler, TaskPool, CheckpointStore, Session, Memory, MCP servers.

### 8.4 ClusterOrchestrator

```python
class ClusterOrchestrator:
    """Orchestrates cluster lifecycle: provision, wire subscriptions, kickoff, teardown."""
    async def provision_cluster(spec: ClusterSpec) -> ClusterProvisionResult
    async def teardown_cluster(cluster_id, archive_logs=True) -> TeardownResult
```

Provision flow: bootstrap gateway → bootstrap workers (parallel) → wire subscriptions → kickoff.

### 8.5 HTTP Endpoints

- `GET /blueprints` — list blueprints
- `GET /blueprints/{type}/{id}` — get blueprint content
- `POST /blueprints/save` — save LLM-generated blueprint
- `POST /clusters/provision` — provision cluster from spec
- `GET /clusters` — list active clusters
- `GET /clusters/{id}` — cluster status
- `POST /clusters/{id}/kickoff` — emit cluster.started
- `POST /clusters/{id}/teardown` — graceful teardown
- `POST /gateways/connect` — runtime gateway join (no restart)
- `POST /shutdown` — graceful node shutdown

---

## 9. Feature Area F: External Participant Interaction

**Nguồn gốc:** v6.4 spec (replaces v6.2's ExternalAdapter sketch)

### 9.1 Core model

**ExternalParticipant** — Human với identity, roles, transport preference:

```python
@dataclass
class ExternalParticipant:
    participant_id: str
    name: str
    roles: list[str]            # ["pm", "product-owner"]
    transport: str              # "webhook" | "polling" | "session"
    transport_target: str
    auth_token: str
    cluster_id: str
    active: bool = True
```

**InteractionThread** — One question from agent, all replies:

```python
@dataclass
class InteractionThread:
    question_id: str            # = correlation_id
    source_agent: str
    required_role: str          # "pm", "devops", etc.
    question_text: str
    status: str                 # "open" | "answered" | "timed_out"
    replies: list[InteractionReply]
    resolution: InteractionAnswer | None
```

**ChannelLog** — Shared conversation space per cluster.

### 9.2 Role-based routing

```
Agent emits participant.input_required {required_role: "pm"}
→ ExternalAdapter looks up participants with role "pm"
→ Notify ALL matching participants
→ First valid answer resolves thread
→ Others can still comment/tag
```

No load balancing, no priority, no availability check. Pure role match. First-wins semantics.

### 9.3 `suspend_and_ask` v6 — dual path

```python
# Agent-to-agent (v6.2)
mesh_action("self", "suspend_and_ask", {
    "question": "...",
    "ask_node": "architect-A",      # target specific agent
})

# Agent-to-human (v6.4)
mesh_action("self", "suspend_and_ask", {
    "question": "...",
    "target_role": "pm",            # target human role
})
```

### 9.4 ExternalAdapter node

A specialized node deployed per cluster that:
- Subscribes `participant.input_required` → routes to participants
- Manages ExternalParticipantRegistry
- Manages ChannelLog
- Supports /intent session bridge (recommended UX)

### 9.5 HTTP Endpoints (on ExternalAdapter)

- `POST /participants/register`, `GET /participants`, `PATCH /participants/{id}`, `DELETE /participants/{id}`
- `GET /room/{cluster_id}` — full ChannelLog
- `GET /room/{cluster_id}/pending` — pending questions for participant
- `GET /room/{cluster_id}/threads/{qid}` — single thread
- `POST /room/{cluster_id}/threads/{qid}/reply` — answer/comment/tag

---

## 10. Feature Area G: Multi-Channel Transport

**Nguồn gốc:** v6.5 spec

### 10.1 TelegramTransport node (Phase 1 — MVP)

A GNOT node that bridges between ChannelLog and Telegram Bot API:
- Outbound: GNOT events → formatted Telegram messages
- Inbound: Telegram replies → GNOT room replies → resolve threads

```yaml
# telegram-transport-cluster-A/node.yaml
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  group_chat_id: "-1001234567890"
  webhook_url: "https://your-domain.com/telegram/update"
  message_format: "full"
  language: "vi"
```

**Telegram UX:** Reply-to-message (natural UX) hoặc /answer command.

### 10.2 Session Push via SSE (Phase 2)

Add SSE endpoint cho /intent sessions để nhận real-time room updates:

```
GET /intent/stream/{session_id}
Accept: text/event-stream

→ Server pushes: room.answer, room.question_new, room.comment_added, heartbeat
```

Client synthesizes SSE events as system messages → LLM responds proactively.

### 10.3 ChannelLog layer (Phase 3)

Unified conversation state across all channels:

```python
class ChannelLog:
    """Central message store. All bridges read/write here."""
    async def post_message(message: ChannelLogEntry) -> None
    async def register_bridge(bridge_id, callback) -> None
```

Bridges: `SessionChannelTransport`, `TelegramChannelTransport`, `APIChannelTransport`.

---

## 11. Feature Area H: Persistent Agent & Memory

**Nguồn gốc:** v6.6 spec

### 11.1 PersistentSessionStore

Drop-in replacement cho ConversationStore:

```python
class PersistentSessionStore:
    """File-based (JSONL per session). Survives restart."""
    async def startup_load() -> int           # restore sessions on startup
    async def get_or_create(session_id, ttl_seconds?) -> PersistedSession
    async def add_message(session_id, message) -> None
    async def clear_messages(session_id) -> bool   # keep session, clear history
    async def delete(session_id) -> bool
```

Key features:
- TTL = 0 → infinite (never expire)
- Per-session TTL override
- Lazy message loading
- Crash-safe (append-only JSONL)

### 11.2 AgentMemoryStore

Writable persistent memory cho cross-session facts:

```python
class AgentMemoryStore:
    async def remember(key, value, entry_type, scope, ...) -> MemoryEntry
    async def recall(query?, scope?, session_id?) -> list[MemoryEntry]
    async def forget(key?, scope?) -> int
    def recall_for_prompt(session_id) -> str  # formatted for system prompt
```

Entry types: `fact` (key-value), `narrative` (free-form).
Scopes: `global`, `session:{id}`.

Memory injected into LLM system prompt automatically.

### 11.3 Three seed actions

- `agent_remember(key, value, type, scope)` — store memory
- `agent_recall(query?, scope?)` — retrieve memories
- `agent_forget(key?, scope?)` — remove memories

### 11.4 Config

```yaml
session:
  backend: "persistent"       # "memory" (v5.x) | "persistent"
  storage_dir: ./sessions
  default_ttl_seconds: 0      # 0 = infinite
  max_messages_per_session: 0  # 0 = unlimited

memory:
  enabled: true
  storage_dir: ./memory
  inject_into_prompt: true
  max_entries: 1000
```

### 11.5 HTTP Endpoints

- `POST /sessions/{id}/clear` — clear messages, keep session + memory
- `GET /memory`, `POST /memory`, `DELETE /memory` — memory CRUD

---

## 12. Feature Area I: MCP Native Support

**Nguồn gốc:** v6.7 spec (Approach B selected)

### 12.1 Approach: MCPClient + Native Tool Injection

Tại startup, connect đến MCP servers, discover tools via `tools/list`, inject tool specs vào LLM's tools array alongside `mesh_action`.

LLM thấy MCP tools với full JSON Schema — gọi trực tiếp, không qua `mesh_action`.

### 12.2 Components

**MCPClient** — Client cho single MCP server (stdio hoặc SSE transport):

```python
class MCPClient:
    async def connect() -> dict        # MCP initialize handshake
    async def list_tools() -> list     # tools/list
    async def call_tool(name, args) -> list[dict]  # tools/call
```

**MCPRegistry** — Manages all MCP connections:

```python
class MCPRegistry:
    async def startup() -> None        # connect all, discover tools
    async def shutdown() -> None
    def get_tool_specs() -> list[dict] # OpenAI-compatible tool specs
    def is_mcp_tool(name) -> bool
    async def call_tool(qualified_name, args) -> str
```

**Tool naming:** `mcp__{server_id}__{tool_name}` (e.g., `mcp__filesystem__read_file`).

### 12.3 IntentHandler integration

```python
tools = [MESH_TOOL_SPEC]
if self._mcp:
    tools = tools + self._mcp.get_tool_specs()

# Route tool calls:
if self._mcp and self._mcp.is_mcp_tool(tc.name):
    return await self._mcp.call_tool(tc.name, tc.arguments)
# else: existing mesh_action routing
```

### 12.4 Config

```yaml
mcp_servers:
  - id: filesystem
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-filesystem /workspace"
  - id: github
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-github"
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
  - id: custom
    transport: sse
    url: "https://my-mcp-server.internal/mcp"
```

### 12.5 HTTP Endpoints

- `GET /mcp/servers` — list connected MCP servers
- `GET /mcp/tools` — list all discovered MCP tools

### 12.6 Future: GNOT as MCP Server (v7 roadmap)

Expose GNOT actions as MCP server cho external clients (Claude Desktop, Cursor, n8n).

---

## 13. Design Review & Optimizations

Phần này ghi lại những điều chỉnh so với design gốc trong v6.0–v6.7.

### 13.1 ChannelRegistry simplification ✅ Applied

**v6.0 original:** ChannelRegistry là module riêng ~150 LOC với separate data model, join/leave API, 5 new endpoints.

**v6.1 optimization (adopted):** ChannelRegistry = thin wrapper ~30 LOC trên NodeRegistry. Gateway IS the channel. Register = join. Loại bỏ redundancy.

**Impact:** −5 endpoints, −120 LOC, simpler mental model.

### 13.2 `channels:` config field removed ✅ Applied

**v6.0 original:** Node khai báo `channels: [cluster-1, ops-alerts]` riêng.

**v6.1 optimization (adopted):** Thay bằng `additional_gateways:`. Register with gateway = join channel.

### 13.3 Event channel_id scoping simplified ✅ Applied

**v6.0 original:** Event có `channel_id` field, subscription filter by channel.

**v6.1 optimization (adopted):** Channel scope enforced bởi NodeRegistry membership. EventBus chỉ deliver đến registered members. Không cần `channel_id` trên event — scope đã implicit.

**Ghi chú implementation:** Giữ `channel_id` field trên Event model cho backward compat nhưng không dùng làm routing criterion. Có thể dùng cho logging/audit.

### 13.4 EventBus persistence — nên implement sớm hơn ⚠️ Recommendation

**v6.0 original:** Event persistence là P3 (production hardening), xếp sau Scheduler và adaptive polling.

**Recommendation:** Đưa event persistence (JSONL file) lên Phase 2 thay vì Phase 5. Lý do: events là audit trail quan trọng. Mất events khi restart = mất context. Implementation rất nhỏ (~100 LOC) nhưng impact lớn.

### 13.5 Cron parsing — dùng `croniter` library ⚠️ Recommendation

**v6.0 original:** "lightweight in-process library (không deps)" cho cron parsing.

**Recommendation:** Dùng `croniter` (2.7KB, pure Python, well-tested) thay vì self-implement. Self-implement cron parsing dễ có bugs với edge cases (leap year, DST, etc.). `croniter` đã production-proven.

### 13.6 CheckpointStore — cân nhắc dùng SQLite thay vì JSONL ⚠️ Recommendation

**v6.2 original:** JSONL file per node cho checkpoint storage.

**Recommendation:** Cân nhắc SQLite cho CheckpointStore vì:
- Cần lookup by `question_id` (index) — JSONL phải scan toàn bộ
- Cần update status (answered, timed_out) — JSONL append-only phải rewrite
- SQLite single-file, zero config, ACID compliant

Nếu muốn giữ JSONL: cần in-memory index (đã thiết kế trong spec) nhưng rebuild index on startup = O(n) scan.

**Trade-off:** JSONL đơn giản hơn và consistent với patterns khác (UploadManager). SQLite mạnh hơn cho queries. Recommend: bắt đầu với JSONL + in-memory index, migrate sang SQLite nếu checkpoint count > 1000.

### 13.7 TaskPool — async.Task không thể pause ✅ Acknowledged

**v6.2 design:** TaskPool ghi rõ "coroutine is NOT paused — task voluntarily exits after saving checkpoint. Resume = new coroutine from checkpoint."

Đây là correct approach. Python asyncio không support pausing coroutines. Checkpoint-based suspend/resume là pattern đúng.

### 13.8 TelegramTransport — Hướng 1 first ✅ Applied

**v6.5 analysis:** Hai hướng — MVP (TelegramTransport node) và Full (Session Push + ChannelLog).

**Decision (adopted from spec):** Hướng 1 first (simple, no core changes), Hướng 2 later (SSE push, unified conversation). Phù hợp với incremental delivery.

### 13.9 MCP — Approach B selected ✅ Applied

**v6.7 analysis:** Ba approaches — A (shallow bridge), B (native injection), C (GNOT as MCP server).

**Decision (adopted from spec):** B now, C later. B cho native tool schemas — LLM gọi MCP tools với full type information. A quá shallow (LLM mù schemas). C là natural extension sau B.

### 13.10 PersistentSessionStore — context window management ⚠️ Recommendation

**v6.6 design:** `max_messages_per_session` truncates messages to LLM but keeps full history on disk.

**Enhancement recommendation:** Thêm automatic summarization khi message count vượt threshold. Thay vì chỉ truncate (mất context), LLM summarize N oldest messages thành 1 summary message. Giữ summary + recent messages.

Implementation: future enhancement, không block v6.0 release.

### 13.11 ExternalAdapter — per-cluster vs shared ⚠️ Recommendation

**v6.4 spec:** One external-adapter node per cluster.

**Recommendation:** Bắt đầu per-cluster (simple). Shared external-adapter cho nhiều clusters là optimization — cần careful routing logic. Per-cluster giữ isolation rõ ràng.

### 13.12 Port allocation cho dynamic provisioning ⚠️ Recommendation

**v6.3 open question Q1:** Port nào cho dynamically provisioned nodes?

**Recommendation:** Thêm `port_range: [8090, 8200]` vào seed config. ClusterOrchestrator auto-allocate từ range, track allocated ports trong state file. Đơn giản, deterministic.

---

## 14. Consolidated node.yaml Schema

```yaml
# ══════════════════════════════════════════════════════════════
# GNOT v6.0 — Complete node.yaml reference
# ══════════════════════════════════════════════════════════════

# ── Core identity (v5.x, unchanged) ──────────────────────────
node_id: analyst-cluster-A
listen: 0.0.0.0:8092
auth_token: tok-analyst-a
allowed_tokens: [tok-gateway-a, tok-seed]

# ── Primary gateway (v5.x, unchanged) ────────────────────────
gateway_node_id: gateway-cluster-A
gateway_address: http://localhost:8091
gateway_auth_token: tok-analyst-on-gw-a

# ── v6.0 NEW: Additional gateways ────────────────────────────
additional_gateways:
  - address: https://gateway-b.vietml.com
    auth_token: tok-analyst-on-gw-b

# ── v6.0 NEW: Registration policy (gateway nodes only) ───────
registration_policy: open            # open | whitelist | invite_only
channel_description: "Frontend dev team"
trusted_nodes: [analyst-A, dev-A]    # used when policy = whitelist

# ── LLM (v5.9, unchanged) ────────────────────────────────────
llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
  api_key: "${ANTHROPIC_API_KEY}"

skills_file: ./skills.md

# ── v6.0 NEW: EventBus ───────────────────────────────────────
event_bus:
  enabled: true
  max_log_size: 10000
  delivery_timeout_seconds: 10
  delivery_retry_count: 3
  delivery_retry_backoff: 2.0
  persist_path: ./events/            # JSONL event persistence

# ── v6.0 NEW: Scheduler ──────────────────────────────────────
scheduler:
  enabled: true

schedule:
  - trigger_type: event
    on_event_type: "clarification.answered"
    run_action: handle_clarification_answer
  - trigger_type: condition
    check_action: analyst_self_check
    run_action: start_analysis
    check_interval_seconds: 60

# ── v6.0 NEW: Adaptive polling ────────────────────────────────
poll_interval_seconds: 5
poll_interval_max_seconds: 60
poll_backoff_multiplier: 1.5

# ── v6.0 NEW: Task lifecycle ─────────────────────────────────
task_pool:
  enabled: true
  max_active_tasks: 3

checkpoint_store:
  enabled: true
  path: ./checkpoints/
  default_timeout_seconds: 86400

# ── v6.0 NEW: Persistent sessions ────────────────────────────
session:
  backend: persistent                # "memory" | "persistent"
  storage_dir: ./sessions
  default_ttl_seconds: 0             # 0 = infinite
  max_messages_per_session: 0        # 0 = unlimited

# ── v6.0 NEW: Agent memory ───────────────────────────────────
memory:
  enabled: true
  storage_dir: ./memory
  inject_into_prompt: true
  max_entries: 1000

# ── v6.0 NEW: MCP servers ────────────────────────────────────
mcp_servers:
  - id: filesystem
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-filesystem /workspace"
  - id: github
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-github"
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"

# ── v6.0 NEW: Telegram transport (transport nodes only) ──────
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  group_chat_id: "-1001234567890"
  webhook_url: "https://your-domain.com/telegram/update"
  language: "vi"

# ── v6.0 NEW: External participant config (adapter nodes) ────
participant_registry:
  storage_dir: ./participants

channel_log:
  storage_dir: ./room
```

---

## 15. Consolidated HTTP Endpoints

### Endpoints mới (v6.0)

| Endpoint | Feature Area | Description |
|----------|-------------|-------------|
| `POST /emit` | A: EventBus | Publish event |
| `POST /subscribe` | A: EventBus | Register subscription |
| `DELETE /subscriptions/{id}` | A: EventBus | Cancel subscription |
| `GET /subscriptions` | A: EventBus | List subscriptions |
| `GET /events` | A: EventBus | Replay event log |
| `POST /schedule` | B: Scheduler | Register trigger |
| `DELETE /schedule/{id}` | B: Scheduler | Cancel trigger |
| `GET /schedule` | B: Scheduler | List entries |
| `POST /schedule/{id}/trigger` | B: Scheduler | Manual trigger |
| `PATCH /schedule/{id}` | B: Scheduler | Update trigger |
| `GET /tasks` | D: TaskPool | List active/suspended |
| `GET /tasks/{id}/checkpoint` | D: TaskPool | View checkpoint |
| `POST /tasks/{id}/answer` | D: TaskPool | Inject answer |
| `DELETE /tasks/{id}` | D: TaskPool | Cancel task |
| `GET /blueprints` | E: Provision | List blueprints |
| `GET /blueprints/{type}/{id}` | E: Provision | Get blueprint |
| `POST /blueprints/save` | E: Provision | Save blueprint |
| `POST /clusters/provision` | E: Provision | Provision cluster |
| `GET /clusters` | E: Provision | List clusters |
| `GET /clusters/{id}` | E: Provision | Cluster status |
| `POST /clusters/{id}/kickoff` | E: Provision | Start cluster |
| `POST /clusters/{id}/teardown` | E: Provision | Teardown cluster |
| `POST /gateways/connect` | E: Provision | Runtime gateway join |
| `POST /shutdown` | E: Provision | Graceful shutdown |
| `POST /participants/register` | F: Participants | Register human |
| `GET /participants` | F: Participants | List participants |
| `PATCH /participants/{id}` | F: Participants | Update roles |
| `DELETE /participants/{id}` | F: Participants | Deactivate |
| `GET /room/{cluster_id}` | F: ChannelLog | View room |
| `GET /room/{cluster_id}/pending` | F: ChannelLog | Pending questions |
| `POST /room/.../reply` | F: ChannelLog | Answer/comment |
| `POST /sessions/{id}/clear` | H: Memory | Clear messages |
| `GET /memory` | H: Memory | Read memories |
| `POST /memory` | H: Memory | Write memory |
| `DELETE /memory` | H: Memory | Delete memory |
| `GET /mcp/servers` | I: MCP | List MCP servers |
| `GET /mcp/tools` | I: MCP | List MCP tools |

### Endpoints modified (v6.0)

| Endpoint | Change |
|----------|--------|
| `GET /health` | Add v6 extensions info |
| `GET /nodes` | Add channel_id, registration_policy |
| `POST /nodes/register` | Registration policy enforcement |
| `GET /capabilities` | Include MCP tools |
| `GET /sessions/{id}` | Add ttl, memory_count |

---

## 16. File Impact Matrix

### NEW files

| File | Phase | LOC est. | Feature Area |
|------|-------|----------|-------------|
| `runtime/event_bus.py` | 1 | ~400 | A: EventBus |
| `runtime/scheduler.py` | 2 | ~350 | B: Scheduler |
| `runtime/gateway_connection.py` | 2 | ~250 | C: Multi-gw |
| `runtime/channel_registry.py` | 1 | ~30 | A: EventBus |
| `runtime/persistent_session_store.py` | 3 | ~300 | H: Persistence |
| `runtime/agent_memory_store.py` | 3 | ~300 | H: Memory |
| `runtime/mcp_client.py` | 3 | ~250 | I: MCP |
| `runtime/mcp_registry.py` | 3 | ~200 | I: MCP |
| `runtime/checkpoint_store.py` | 4 | ~250 | D: TaskPool |
| `runtime/task_pool.py` | 4 | ~300 | D: TaskPool |
| `runtime/cluster_orchestrator.py` | 5 | ~400 | E: Provision |
| `runtime/blueprint_store.py` | 5 | ~200 | E: Provision |
| `runtime/external_participant_registry.py` | 6 | ~250 | F: Participants |
| `runtime/channel_log.py` | 6 | ~300 | F: ChannelLog |
| `runtime/interaction_router.py` | 6 | ~200 | F: Routing |
| `runtime/telegram_transport.py` | 7 | ~350 | G: Telegram |
| `seed/actions/agent_remember.py` | 3 | ~30 | H: Memory |
| `seed/actions/agent_recall.py` | 3 | ~30 | H: Memory |
| `seed/actions/agent_forget.py` | 3 | ~20 | H: Memory |
| `seed/actions/suspend_and_ask.py` | 4 | ~50 | D: TaskPool |
| `seed/actions/handle_clarification_answer.py` | 4 | ~30 | D: TaskPool |

### MODIFIED files

| File | Phases | Changes |
|------|--------|---------|
| `runtime/config.py` | 1–7 | All new config fields |
| `runtime/models.py` | 1–7 | All new Pydantic models |
| `runtime/server.py` | 1–7 | All new endpoints, lifespan hooks |
| `runtime/worker_agent.py` | 2 | Multi-gateway refactor, adaptive poll |
| `runtime/node_registry.py` | 2 | Registration policy |
| `runtime/intent_handler.py` | 3,4 | Memory injection, MCP tools, suspend |
| `runtime/job_manager.py` | 4 | SUSPENDED status |
| `runtime/bootstrap.py` | 5 | Extended BootstrapRequest |

---

## 17. Implementation Roadmap

### Phân chia phases

Roadmap được tổ chức từ **simple → complex**, mỗi phase tự hoàn chỉnh (có thể ship độc lập), và phase sau build on phase trước.

```
Phase 1: Event Foundation          ─── 1–2 weeks ───  Foundation
Phase 2: Autonomy & Multi-Gateway  ─── 1–2 weeks ───  Foundation
Phase 3: Persistence & MCP         ─── 1–2 weeks ───  Independent modules
Phase 4: Task Suspension            ─── 2–3 weeks ───  Complex orchestration
Phase 5: Cluster Provisioning       ─── 2–3 weeks ───  Complex orchestration
Phase 6: External Participants      ─── 2–3 weeks ───  ExternalAdapter node
Phase 7: Telegram Transport         ─── 1–2 weeks ───  Transport layer
Phase 8: Polish & Production        ─── 1–2 weeks ───  Hardening
```

---

### Phase 1: Event Foundation (1–2 weeks)

**Goal:** EventBus core working — emit, subscribe, deliver, event log.

**Dependencies:** None (additive to v5.13b)

**Deliverables:**

| # | Task | LOC | File(s) | Priority |
|---|------|-----|---------|----------|
| 1.1 | Event, Subscription data models | ~50 | `models.py` | P0 |
| 1.2 | EventBus class (emit, subscribe, match, deliver) | ~350 | `event_bus.py` | P0 |
| 1.3 | ChannelRegistry thin wrapper | ~30 | `channel_registry.py` | P0 |
| 1.4 | EventBus config parsing | ~30 | `config.py` | P0 |
| 1.5 | HTTP endpoints: POST /emit, POST /subscribe, DELETE /subscriptions/{id}, GET /subscriptions, GET /events | ~120 | `server.py` | P0 |
| 1.6 | EventBus lifespan (start/stop delivery worker) | ~20 | `server.py` | P0 |
| 1.7 | Event persistence (JSONL) | ~100 | `event_bus.py` | P1 |
| 1.8 | Health endpoint extension (v6 info) | ~20 | `server.py` | P1 |
| 1.9 | Unit tests | ~200 | `tests/test_event_bus.py` | P0 |

**Acceptance criteria:**
- Node A emits event → subscriber on Node B receives callback action
- Event log queryable via GET /events
- Events persisted to JSONL file
- Zero impact on existing endpoints

---

### Phase 2: Autonomy & Multi-Gateway (1–2 weeks)

**Goal:** Scheduler (3 levels), adaptive polling, multi-gateway membership.

**Dependencies:** Phase 1 (event triggers in Scheduler depend on EventBus)

**Deliverables:**

| # | Task | LOC | File(s) | Priority |
|---|------|-----|---------|----------|
| 2.1 | ScheduleEntry model | ~40 | `models.py` | P0 |
| 2.2 | Scheduler class (condition, cron, event, once loops) | ~350 | `scheduler.py` | P0 |
| 2.3 | Cron parsing (install `croniter`) | ~20 | `scheduler.py` | P0 |
| 2.4 | Scheduler HTTP endpoints | ~80 | `server.py` | P0 |
| 2.5 | Scheduler config + static schedule from node.yaml | ~40 | `config.py` | P0 |
| 2.6 | Adaptive polling in WorkerAgent | ~50 | `worker_agent.py` | P1 |
| 2.7 | GatewayConnection extracted from WorkerAgent | ~250 | `gateway_connection.py` | P1 |
| 2.8 | WorkerAgent multi-connection refactor | ~80 | `worker_agent.py` | P1 |
| 2.9 | `additional_gateways` config | ~30 | `config.py` | P1 |
| 2.10 | Registration policy (open/whitelist) in NodeRegistry | ~80 | `node_registry.py` | P1 |
| 2.11 | Unit tests | ~250 | `tests/test_scheduler.py`, `tests/test_multi_gw.py` | P0 |

**Acceptance criteria:**
- Cron trigger fires at scheduled times
- Condition trigger detects state and fires action
- Event trigger reacts to EventBus events
- Node registers with 2 gateways, receives events from both
- Polling backs off when idle, resets on job

---

### Phase 3: Persistence & MCP (1–2 weeks)

**Goal:** Persistent sessions, agent memory, MCP tool consumption. Three independent modules — can be parallelized.

**Dependencies:** None hard (Phase 1 recommended for EventBus context). MCP only needs IntentHandler.

**Deliverables:**

| # | Task | LOC | File(s) | Priority |
|---|------|-----|---------|----------|
| 3.1 | PersistentSessionStore | ~300 | `persistent_session_store.py` | P0 |
| 3.2 | Session config + backend selection | ~40 | `config.py`, `server.py` | P0 |
| 3.3 | POST /sessions/{id}/clear endpoint | ~20 | `server.py` | P0 |
| 3.4 | AgentMemoryStore | ~300 | `agent_memory_store.py` | P0 |
| 3.5 | Memory config + injection into IntentHandler | ~50 | `config.py`, `intent_handler.py` | P0 |
| 3.6 | Seed actions: agent_remember, agent_recall, agent_forget | ~80 | `seed/actions/` | P0 |
| 3.7 | GET/POST/DELETE /memory endpoints | ~60 | `server.py` | P0 |
| 3.8 | MCPClient (StdioTransport, SSETransport) | ~250 | `mcp_client.py` | P1 |
| 3.9 | MCPRegistry (tool discovery, spec conversion) | ~200 | `mcp_registry.py` | P1 |
| 3.10 | MCP config parsing | ~40 | `config.py` | P1 |
| 3.11 | IntentHandler: inject MCP tools, route MCP calls | ~60 | `intent_handler.py` | P1 |
| 3.12 | GET /mcp/servers, GET /mcp/tools endpoints | ~40 | `server.py` | P1 |
| 3.13 | Unit tests | ~300 | `tests/test_persistence.py`, `tests/test_mcp.py` | P0 |

**Acceptance criteria:**
- Session survives node restart
- Agent remembers facts across sessions
- `[Memory]` block appears in LLM system prompt
- MCP filesystem server tools discoverable via GET /mcp/tools
- LLM calls `mcp__filesystem__read_file` successfully

---

### Phase 4: Task Suspension & Resumption (2–3 weeks)

**Goal:** Agent can suspend task, ask question, switch to other task, resume when answer arrives.

**Dependencies:** Phase 1 (EventBus for clarification events), Phase 3 (PersistentSessionStore for checkpoint context)

**Deliverables:**

| # | Task | LOC | File(s) | Priority |
|---|------|-----|---------|----------|
| 4.1 | CheckpointStore (TaskCheckpoint model + JSONL store) | ~250 | `checkpoint_store.py` | P0 |
| 4.2 | TaskPool (start/suspend/resume) | ~300 | `task_pool.py` | P0 |
| 4.3 | SUSPENDED, RESUMING, TIMED_OUT job statuses | ~30 | `models.py`, `job_manager.py` | P0 |
| 4.4 | IntentHandler: handle suspend_and_ask | ~100 | `intent_handler.py` | P0 |
| 4.5 | suspend_and_ask seed action | ~50 | `seed/actions/suspend_and_ask.py` | P0 |
| 4.6 | handle_clarification_answer action | ~30 | `seed/actions/` | P0 |
| 4.7 | handle_clarification_timeout action | ~30 | `seed/actions/` | P0 |
| 4.8 | CheckpointStore config | ~20 | `config.py` | P0 |
| 4.9 | GET /tasks, POST /tasks/{id}/answer, DELETE /tasks/{id} | ~80 | `server.py` | P0 |
| 4.10 | Timeout sweep background task | ~50 | `checkpoint_store.py` | P1 |
| 4.11 | Unit + integration tests | ~300 | `tests/test_task_pool.py` | P0 |

**Acceptance criteria:**
- dev-A suspends task X, checkpoint saved
- dev-A receives task Y while X suspended
- Answer arrives → task X resumed from checkpoint with answer injected
- Timeout fires → assumption used
- Full agent-to-agent clarification chain works

---

### Phase 5: Cluster Provisioning (2–3 weeks)

**Goal:** Seed node provisions complete clusters from natural language or blueprints.

**Dependencies:** Phase 2 (multi-gateway), Phase 1 (EventBus for kickoff), Phase 4 (subscription wiring)

**Deliverables:**

| # | Task | LOC | File(s) | Priority |
|---|------|-----|---------|----------|
| 5.1 | BootstrapRequest v6 (all config fields) | ~200 | `bootstrap.py` | P0 |
| 5.2 | `_step_write_config` full rewrite | ~150 | `bootstrap.py` | P0 |
| 5.3 | BlueprintStore + INDEX.yaml | ~200 | `blueprint_store.py` | P0 |
| 5.4 | Blueprint HTTP endpoints | ~80 | `server.py` | P0 |
| 5.5 | Role blueprints (pm, analyst, architect, dev, tester, reviewer) | ~300 | `blueprints/roles/*.md` | P0 |
| 5.6 | Team blueprints (standard, minimal) | ~100 | `blueprints/teams/*.yaml` | P0 |
| 5.7 | ClusterOrchestrator (provision + teardown) | ~400 | `cluster_orchestrator.py` | P0 |
| 5.8 | POST /clusters/provision, GET /clusters, POST /teardown | ~120 | `server.py` | P0 |
| 5.9 | POST /gateways/connect (runtime join) | ~50 | `server.py`, `worker_agent.py` | P1 |
| 5.10 | POST /shutdown (graceful) | ~30 | `server.py` | P1 |
| 5.11 | Port auto-allocation | ~50 | `cluster_orchestrator.py` | P1 |
| 5.12 | Integration test: provision 2-node cluster | ~200 | `tests/test_provision.py` | P0 |

**Acceptance criteria:**
- POST /clusters/provision creates gateway + 3 workers
- Workers auto-register with gateway
- Subscriptions wired correctly
- POST /clusters/{id}/kickoff triggers cluster.started
- POST /clusters/{id}/teardown cleanly stops all nodes

---

### Phase 6: External Participants (2–3 weeks)

**Goal:** Multiple humans participate with role-based routing.

**Dependencies:** Phase 4 (task suspension → participant.input_required), Phase 5 (ExternalAdapter node provisioned)

**Deliverables:**

| # | Task | LOC | File(s) | Priority |
|---|------|-----|---------|----------|
| 6.1 | ExternalParticipant model | ~40 | `models.py` | P0 |
| 6.2 | ExternalParticipantRegistry | ~250 | `external_participant_registry.py` | P0 |
| 6.3 | InteractionThread, InteractionReply models | ~80 | `models.py` | P0 |
| 6.4 | ChannelLog (room state + thread management) | ~300 | `channel_log.py` | P0 |
| 6.5 | InteractionRouter (role-based routing) | ~200 | `interaction_router.py` | P0 |
| 6.6 | Participant HTTP endpoints | ~120 | `server.py` | P0 |
| 6.7 | Room HTTP endpoints | ~120 | `server.py` | P0 |
| 6.8 | suspend_and_ask: target_role path | ~40 | `intent_handler.py` | P0 |
| 6.9 | ExternalAdapter role blueprint | ~50 | `blueprints/roles/external-adapter.md` | P0 |
| 6.10 | route_interaction_to_participants action | ~80 | `seed/actions/` | P0 |
| 6.11 | Integration test: register participant, answer question | ~200 | `tests/test_participants.py` | P0 |

**Acceptance criteria:**
- Human registers with roles: pm, product-owner
- Agent asks question targeting role "pm"
- Human with PM role receives notification
- Human submits answer → agent resumes
- ChannelLog shows full thread history

---

### Phase 7: Telegram Transport (1–2 weeks)

**Goal:** Humans interact via Telegram.

**Dependencies:** Phase 6 (participant registry, room endpoints)

**Deliverables:**

| # | Task | LOC | File(s) | Priority |
|---|------|-----|---------|----------|
| 7.1 | TelegramTransport (Bot API client, message formatting) | ~350 | `telegram_transport.py` | P0 |
| 7.2 | Telegram webhook handler (inbound) | ~150 | `telegram_transport.py` | P0 |
| 7.3 | Telegram participant mapping | ~80 | `telegram_transport.py` | P0 |
| 7.4 | Telegram config parsing | ~20 | `config.py` | P0 |
| 7.5 | telegram_forward_question action | ~50 | `seed/actions/` | P0 |
| 7.6 | telegram_notify_resolved action | ~30 | `seed/actions/` | P0 |
| 7.7 | /register command via Telegram | ~40 | `telegram_transport.py` | P1 |
| 7.8 | Reply-to-message mapping | ~50 | `telegram_transport.py` | P1 |
| 7.9 | Integration test | ~150 | `tests/test_telegram.py` | P0 |

**Acceptance criteria:**
- Question from agent → Telegram group message
- Human replies in Telegram → thread resolved → agent resumes
- Registration via Telegram /register command

---

### Phase 8: Polish & Production Hardening (1–2 weeks)

**Goal:** Production readiness — edge cases, error handling, documentation.

**Deliverables:**

| # | Task | Priority |
|---|------|----------|
| 8.1 | Dead-letter queue for failed event deliveries | P1 |
| 8.2 | MCP server reconnect on failure | P1 |
| 8.3 | Checkpoint compaction (rewrite on threshold) | P2 |
| 8.4 | Session file compaction | P2 |
| 8.5 | Memory LRU eviction when max_entries exceeded | P2 |
| 8.6 | SSE push for /intent sessions (v6.5 Hướng 2) | P2 |
| 8.7 | Invite-only registration policy | P3 |
| 8.8 | Cross-gateway event propagation (federation) | P3 |
| 8.9 | Full documentation + examples | P0 |
| 8.10 | End-to-end integration test: provision cluster, run workflow, teardown | P0 |

---

### Timeline Summary

```
Week  1–2:  Phase 1 — Event Foundation
Week  3–4:  Phase 2 — Autonomy & Multi-Gateway
Week  5–6:  Phase 3 — Persistence & MCP (parallelizable)
Week  7–9:  Phase 4 — Task Suspension
Week 10–12: Phase 5 — Cluster Provisioning
Week 13–15: Phase 6 — External Participants
Week 16–17: Phase 7 — Telegram Transport
Week 18–19: Phase 8 — Polish & Production

Total: ~19 weeks (4.5 months) for full v6.0
MVP (Phases 1–4): ~9 weeks (2 months) — event-driven autonomous agents
```

**Parallel tracks possible:**
- Phase 3 (Persistence + MCP) can run parallel with Phase 2
- Phase 7 (Telegram) can start as soon as Phase 6 endpoints are stable
- Phase 8 items can be sprinkled throughout

---

## 18. Risk Assessment

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|-----------|
| IntentHandler suspend/resume complexity | High — core flow change | Medium | Extensive unit tests, checkpoint-based (not coroutine-pause) |
| MCP stdio transport stability | Medium — process crashes | Medium | reconnect_on_failure with backoff |
| Event delivery reliability | High — missed events = broken workflow | Low | Retry 3x + dead-letter queue + event persistence |
| ClusterOrchestrator partial failure | High — half-provisioned cluster | Medium | Rollback on failure (already in BootstrapEngine) |
| Context window overflow (many MCP tools) | Medium — LLM confused | Low | Limit MCP tools per node; tool description truncation |
| JSONL performance with many checkpoints | Low | Low | In-memory index; migrate to SQLite if needed |

---

## 19. Terminology Canon

Canonical GNOT terms used consistently throughout v6.0:

| Term | Meaning | NOT |
|------|---------|-----|
| Cluster | Set of nodes provisioned for shared purpose | ~~Team~~ |
| ClusterSpec | Blueprint for a cluster | ~~TeamSpec~~ |
| ClusterOrchestrator | Provisions/teardowns clusters | ~~TeamWirer~~ |
| NodeSpec | Specification for one node in cluster | ~~MemberSpec~~ |
| cluster_id | Cluster identifier | ~~team_id~~ |
| cluster.started | Kickoff event | ~~project.started~~ |
| ExternalParticipant | Human/system outside mesh | ~~HumanParticipant~~ |
| ExternalAdapter | Bridge between mesh and external channels | ~~HumanInterface~~ |
| ChannelLog | Log of interactions in a channel | ~~ConversationRoom~~ |
| InteractionThread | Thread of question + replies | ~~QuestionThread~~ |
| transport | Delivery mechanism (webhook, session, telegram) | ~~notify_via~~ |
| target_role | Role needed to answer | ~~ask_role~~ |
| participant.* | Event namespace for external interactions | ~~human.*~~ |

---

*Document: GNOT v6.0 Consolidated Specification*  
*Consolidated from: SPECS_V6.0.md through SPECS_V6.7.md*  
*Base implementation: v5.13b (~6,500 LOC)*  
*Estimated v6.0 additions: ~7,000 LOC new + ~1,500 LOC modifications*  
*Organization: gnot-io*  
*Date: 2026-03-08*
