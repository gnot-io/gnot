# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v6.3
### Self-Provisioning Clusters · Seed-as-Orchestrator · Blueprint System · Dynamic Cluster Provisioning

**Base version:** v6.2
**Target version:** v6.3
**Status:** Analysis complete — design pending
**Authors:** Architecture review session, 2026-03-08

---

## 1. Bài toán gốc

### 1.1 Yêu cầu từ product owner

> "Anh kỳ vọng sau khi hệ thống này đã được implement hoàn chỉnh thì user có thể
> gởi yêu cầu như 'hãy chuẩn bị 2 dev team để làm 2 dự án dưới đây...'. Hệ thống
> có thể hỏi để làm rõ thêm thông tin. Khi đó các team sẽ tự động được tạo ra,
> trong quá trình hoạt động có thể hỏi tiếp thông tin từ user..."

> "Đối với Gap A: Theo thiết kế, các node nếu có cấu hình LLM thì đều có thể hỗ
> trợ /intent endpoint; nếu chúng ta cho node đó quyền provision cluster thì nó
> có thể tạo ra các node khác. Thông thường seed node sẽ có quyền này."

> "Khi tạo node mới, nếu hệ thống đã có sẵn blueprint/template thì sử dụng chúng,
> otherwise thì LLM có thể tự thiết kế ra."

### 1.2 Vision đầy đủ

```
User: "Hãy chuẩn bị 2 dev team để làm 2 dự án dưới đây:
       - Dự án A: Backend API Python, 4 developers, deadline 3 tháng
       - Dự án B: Mobile app React Native, 3 developers, deadline 2 tháng"

System (seed node):
  → Clarify: "Cluster A cần senior tech lead không? Cluster B cần QA riêng?"
  → User: "Cluster A có 1 senior, Cluster B QA do developer kiêm"
  
  → [Provision phase]
  → Tạo gateway-cluster-A + 6 nodes (pm, analyst, architect, dev×2, reviewer)
  → Tạo gateway-cluster-B + 5 nodes (pm, analyst, architect, dev, dev-mobile)
  → Wire subscriptions, inject project context
  → Kickoff cả hai teams
  
  → [Runtime phase]
  → Teams làm việc autonomously
  → dev-cluster-A hỏi: "Null user behavior?" → escalates → hỏi user
  → User trả lời → team tiếp tục
  
  → [Completion phase]
  → Teams report done
  → Seed teardown nodes
```

---

## 2. Phân tích: Seed node là Orchestrator tự nhiên

### 2.1 Confirmation: Gap A không phải gap thực sự

Phân tích kỹ `bootstrap.py` và `server.py` cho thấy:

**`POST /bootstrap` endpoint đã có sẵn trên mọi node có `BootstrapEngine`:**

```python
# server.py
bootstrap_engine = BootstrapEngine(seed_config_path=config_path)

@app.post("/bootstrap")
async def bootstrap_endpoint(req: BootstrapRequest) -> JSONResponse:
    result = await bootstrap_engine.bootstrap(req)
    return JSONResponse(content={
        "node_id": result.node_id,
        "status": result.status.value,
        ...
    })
```

`BootstrapEngine.bootstrap()` thực hiện đầy đủ 8 bước:
1. Create directory structure
2. Write `node.yaml`
3. Write `skills.md`
4. Write action modules
5. Install pip packages
6. Start node process
7. Health-check
8. Register node vào seed config

**Seed node có `/intent` endpoint nếu có LLM config:**

```yaml
# node-0/node.yaml (seed)
llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
  api_key: ...
```

→ `/intent` available → LLM có thể reason và call `mesh_action` tool →
có thể gọi `POST /bootstrap` trên chính mình.

**Seed actions đã có:**
- `execute_command`: chạy shell commands
- `write_file` / `read_file`: file I/O
- `llm_chat`: gọi LLM với custom prompt

**Kết luận:** Seed node + LLM config + `/intent` endpoint + `/bootstrap` endpoint
= **Orchestrator đã hoàn chỉnh về capability**. Không cần node mới hay module mới
cho Gap A. Chỉ cần quyền (permission) và convention.

### 2.2 Seed node là "always-on" orchestrator

```
┌─────────────────────────────────────────────────────────────────┐
│  Seed Node (node-0) — Always Running                            │
│                                                                 │
│  /intent ──► LLM (ReAct) ──► mesh_action tool                  │
│                                    │                            │
│                    ┌───────────────┼────────────────┐           │
│                    ▼               ▼                ▼           │
│              POST /bootstrap  write_file      execute_command   │
│              (create nodes)   (blueprints)    (config, git)     │
│                                                                 │
│  Knows about: all active teams (multi-gateway member v6.1)      │
│  Receives: participant.input_required from all teams (v6.2)           │
│  Can teardown: stop processes, cleanup dirs                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Gap analysis thực sự

Sau khi xác nhận Seed = Orchestrator, 6 gaps thực sự cần address:

### Gap B1: Blueprint storage — không có convention

**Hiện trạng:**
LLM trong /intent có thể gọi `write_file` để lưu blueprint vừa generate, và
`read_file` để đọc lại. Nhưng:

- Không có **standard path convention**: lưu vào đâu?
- Không có **blueprint index**: có những blueprints nào?
- Không có **GET /blueprints** endpoint: LLM phải `execute_command ls` để discover
- Không có **versioning**: blueprint cũ vs. mới, compatible với v6.x hay không
- Không có **blueprint type taxonomy**: role blueprint vs. team blueprint vs. action blueprint

**Consequence:**
LLM phải redesign mọi node từ đầu mỗi lần provision. Không nhất quán giữa
các lần provision. Không có reuse. Hệ thống không "học" từ experience.

### Gap B2: BootstrapRequest thiếu v6.x config fields (CRITICAL)

**Hiện trạng — `_step_write_config` trong `bootstrap.py`:**

```python
config_data: dict[str, Any] = {
    "node_id": request.node_id,
    "listen": request.listen,
    "nodes": {request.node_id: f"http://127.0.0.1:{port}"},
    "default_resolver": request.default_resolver,
    "max_hop": request.max_hop,
    "cache_ttl_seconds": request.cache_ttl_seconds,
}
if request.extra_nodes:
    config_data["nodes"].update(request.extra_nodes)
if request.auth_token:
    config_data["auth_token"] = request.auth_token
```

`BootstrapRequest` dataclass:
```python
@dataclass
class BootstrapRequest:
    node_id: str
    listen: str
    base_dir: str = "."
    actions: list[ActionFile] = field(default_factory=list)
    skills_md: str = ""
    extra_nodes: dict[str, str] = field(default_factory=dict)
    default_resolver: str = "node-0"
    max_hop: int = 10
    cache_ttl_seconds: int = 300
    pip_packages: list[str] = field(default_factory=list)
    runtime_entry: str = "node_runtime.py"
    auth_token: str | None = None
    # MISSING — tất cả v6.x fields:
    # gateway_node_id
    # gateway_address
    # gateway_auth_token
    # additional_gateways
    # registration_policy
    # trusted_nodes (for gateway nodes)
    # heartbeat_timeout_seconds / ping_timeout_seconds
    # heartbeat_interval_seconds / poll_interval_seconds
    # poll_interval_max_seconds / poll_backoff_multiplier
    # event_bus config
    # scheduler config
    # schedule entries
    # task_pool config
    # checkpoint_store config
    # llm config (provider, model, api_key)
    # skills_file path
    # allowed_tokens
```

**Consequence (critical):**
Node được tạo bởi `/bootstrap` sẽ:
- Không biết gateway của mình → không register → không nhận jobs
- Không có EventBus → không nhận/gửi events
- Không có Scheduler → không có subscriptions → không reactive
- Không có LLM config → không thể chạy `/intent` hay `llm_chat`
- Không có auth → không thể communicate với secured nodes

Node được tạo ra nhưng **functionally useless** trong v6.x context.

### Gap B3: Post-bootstrap wiring — không có orchestration

**Sau khi N nodes được bootstrap, cần:**

```
Step 1: Gateway node cần biết về worker nodes
  → gateway-cluster-A cần `registration_policy: open` HOẶC
    worker nodes phải có token trong gateway's `allowed_tokens`
  
Step 2: Worker nodes cần register với gateway
  → Thông thường worker tự register khi start (WorkerAgent)
  → Nhưng gateway phải sẵn sàng accept trước
  
Step 3: Nodes cần setup subscriptions
  → analyst-A subscribe `cluster.started` trên gateway-cluster-A
  → architect-A subscribe `stories.ready` trên gateway-cluster-A
  → dev-A subscribe `design.locked` trên gateway-cluster-A
  → PM subscribe tất cả events
  
Step 4: Seed node join cả hai gateways
  → Seed cần additional_gateways pointing đến team gateways
  → Seed subscribe `participant.input_required` từ cả hai teams
  
Step 5: Kickoff
  → PM-cluster-A nhận signal để start project A
  → PM-cluster-B nhận signal để start project B
```

**Hiện trạng:**
Không có orchestration logic nào thực hiện Steps 1-5. Bootstrap tạo node
và health-check. Sau đó không có gì. Nodes không tự-wire với nhau.

### Gap B4: Clarification-to-provision transition — không có structure

**Hiện trạng:**
/intent session là open-ended conversation. Không có:
- Schema cho "đủ thông tin để provision"
- Structured output format từ clarification phase
- Guardrails để prevent premature provision
- Rollback nếu provision fails midway (partial team created)
- State tracking: "cluster A provisioned, cluster B failed"

**Consequence:**
LLM có thể decide provision với insufficient information, tạo ra
misconfigured teams. Hoặc ngược lại, keep clarifying mãi không provision.

### Gap B5: Project context isolation — không có convention

**Vấn đề:**
Khi LLM generate `skills.md` cho `analyst-cluster-A` và `analyst-cluster-B`,
làm thế nào đảm bảo:
- `analyst-cluster-A` biết về Python backend, không biết về mobile
- `analyst-cluster-B` biết về React Native, không biết về Python backend
- Các business rules của project A không leak vào project B

**Hiện trạng:**
LLM generate content trong /intent session có full context của cả hai projects.
Không có mechanism nào enforce context isolation khi generate configs.

**Consequence:**
Nodes có thể có mixed context, leading to confused behavior.

### Gap B6: Teardown — không có endpoint

**Hiện trạng:**
`BootstrapEngine` có rollback (undo failed bootstrap). Không có teardown
(clean up successful bootstrap khi project done).

Teardown cần:
- Stop node process (kill PID)
- Remove node directory
- Remove từ seed config (`trusted_nodes` list)
- Archive event logs
- Update gateway's member list
- Notify other nodes

Không có `POST /teardown` endpoint. Không có teardown workflow.

---

## 4. Proposed solutions

### Solution B1: Blueprint Storage Convention

**Standard directory structure:**

```
node-0/
└── blueprints/
    ├── INDEX.yaml                    ← blueprint catalog
    ├── roles/
    │   ├── pm.md                     ← PM role blueprint (skills.md template)
    │   ├── analyst.md
    │   ├── architect.md
    │   ├── developer.md
    │   ├── developer-mobile.md
    │   ├── tester.md
    │   └── reviewer.md
    ├── actions/
    │   ├── pm/
    │   │   ├── run_standup.py
    │   │   └── track_progress.py
    │   ├── analyst/
    │   │   ├── analyze_requirements.py
    │   │   └── write_user_stories.py
    │   └── ...
    ├── teams/
    │   ├── standard-cluster.yaml    ← team composition blueprint
    │   ├── minimal-cluster.yaml
    │   └── fullstack-cluster.yaml
    └── generated/
        ├── cluster-A-2026-03-08/        ← LLM-generated, saved for reuse
        └── cluster-B-2026-03-08/
```

**`INDEX.yaml` format:**

```yaml
blueprints:
  roles:
    - id: pm
      path: roles/pm.md
      description: "Project manager — coordinates team, tracks progress"
      version: "1.0"
      tags: [management, coordination]
      suitable_for: [any]

    - id: developer-python
      path: roles/developer-python.md
      description: "Python backend developer"
      version: "1.0"
      tags: [development, python, backend]
      suitable_for: [python, fastapi, django, flask]

    - id: developer-mobile
      path: roles/developer-mobile.md
      description: "React Native mobile developer"
      version: "1.0"
      tags: [development, mobile, react-native]
      suitable_for: [mobile, ios, android, react-native]

  teams:
    - id: standard-cluster
      path: teams/standard-cluster.yaml
      description: "Standard 6-role dev team: pm, analyst, architect, dev×2, reviewer"
      roles: [pm, analyst, architect, developer, developer, reviewer]
      min_nodes: 6
      max_nodes: 10

    - id: minimal-cluster
      path: teams/minimal-cluster.yaml
      description: "Lean 4-role dev team: pm, dev×2, tester"
      roles: [pm, developer, developer, tester]
      min_nodes: 4
      max_nodes: 6
```

**`GET /blueprints` endpoint (new):**

```
Response 200:
{
  "roles": [...],       // from INDEX.yaml roles section
  "teams": [...],       // from INDEX.yaml teams section
  "generated": [...]    // LLM-generated blueprints, reusable
}
```

**`GET /blueprints/{type}/{id}` endpoint (new):**

```
GET /blueprints/roles/developer-python
→ Returns full blueprint content (skills.md template)

GET /blueprints/teams/standard-cluster
→ Returns team composition spec
```

**LLM workflow khi provision:**

```
1. GET /blueprints → list available blueprints
2. Match request requirements với available blueprints
3a. If match found → load blueprint, customize với project context
3b. If no match → LLM designs new blueprint → POST /blueprints/save → reuse later
4. Use blueprint content để populate BootstrapRequest
```

**`POST /blueprints/save` endpoint (new):**

```
Request:
{
  "type": "role",               // "role" | "team" | "action"
  "id": "developer-fintech",
  "content": "# Fintech Developer\n...",
  "description": "Python developer with fintech domain knowledge",
  "tags": ["python", "fintech", "backend"],
  "suitable_for": ["fintech", "payment", "banking"],
  "generated_by": "llm",
  "generated_at": 1741392000.0
}
```

---

### Solution B2: BootstrapRequest v6.3

Extend `BootstrapRequest` và `_step_write_config` để support tất cả v6.x fields:

```python
# runtime/bootstrap.py — v6.3

@dataclass
class AdditionalGatewaySpec:
    address: str
    auth_token: str

@dataclass
class EventBusSpec:
    enabled: bool = True
    max_log_size: int = 10000
    delivery_timeout_seconds: float = 10.0
    delivery_retry_count: int = 3

@dataclass
class SchedulerSpec:
    enabled: bool = True

@dataclass
class ScheduleEntrySpec:
    trigger_type: str                    # "condition" | "cron" | "event" | "once"
    run_action: str
    run_params: dict = field(default_factory=dict)
    # condition
    check_action: str | None = None
    check_interval_seconds: int = 60
    # cron
    cron_expression: str | None = None
    # event
    on_event_type: str | None = None
    on_channel: str = "global"
    on_payload_filter: dict | None = None
    description: str = ""

@dataclass
class LLMSpec:
    provider: str                         # "anthropic" | "openai" | ...
    model: str
    api_key: str
    base_url: str | None = None

@dataclass
class TaskPoolSpec:
    enabled: bool = True
    max_active_tasks: int = 3

@dataclass
class CheckpointStoreSpec:
    enabled: bool = True
    path: str = "/tmp/gnot-checkpoints"
    default_timeout_seconds: int = 86400

@dataclass
class BootstrapRequest:
    """v6.3 — Full node specification including all v6.x features."""

    # ── Core (v5.x) ──────────────────────────────────────────────────────
    node_id: str
    listen: str
    base_dir: str = "."
    actions: list[ActionFile] = field(default_factory=list)
    skills_md: str = ""
    extra_nodes: dict[str, str] = field(default_factory=dict)
    default_resolver: str = "node-0"
    max_hop: int = 10
    cache_ttl_seconds: int = 300
    pip_packages: list[str] = field(default_factory=list)
    runtime_entry: str = "node_runtime.py"
    auth_token: str | None = None

    # ── Auth (v5.13b) ─────────────────────────────────────────────────────
    allowed_tokens: list[str] = field(default_factory=list)
    gateway_auth_token: str | None = None

    # ── Gateway / worker topology (v5.3 + v6.1) ──────────────────────────
    gateway_node_id: str | None = None        # set if this is a worker node
    gateway_address: str | None = None        # HTTP address of gateway
    trusted_nodes: list[str] = field(default_factory=list)  # set if this is a gateway
    heartbeat_timeout_seconds: int = 30
    ping_timeout_seconds: float = 3.0
    heartbeat_interval_seconds: int = 10
    poll_interval_seconds: int = 5
    poll_interval_max_seconds: int = 60
    poll_backoff_multiplier: float = 1.5
    registration_policy: str = "whitelist"    # v6.1
    additional_gateways: list[AdditionalGatewaySpec] = field(default_factory=list)  # v6.1

    # ── LLM (v5.9) ────────────────────────────────────────────────────────
    llm: LLMSpec | None = None                # if set, node gets /intent endpoint
    skills_file: str | None = None            # path to skills.md for GET /skills

    # ── EventBus (v6.0) ───────────────────────────────────────────────────
    event_bus: EventBusSpec | None = None

    # ── Scheduler + schedule entries (v6.0) ──────────────────────────────
    scheduler: SchedulerSpec | None = None
    schedule: list[ScheduleEntrySpec] = field(default_factory=list)

    # ── Task lifecycle (v6.2) ─────────────────────────────────────────────
    task_pool: TaskPoolSpec | None = None
    checkpoint_store: CheckpointStoreSpec | None = None

    # ── Channel description (v6.1, gateway nodes only) ───────────────────
    channel_description: str = ""
```

**`_step_write_config` v6.3:**

```python
async def _step_write_config(self, node_dir, request, tracker, result):
    """Write node.yaml — now includes all v6.x fields."""

    config_data: dict[str, Any] = {
        "node_id": request.node_id,
        "listen": request.listen,
        "nodes": {request.node_id: f"http://127.0.0.1:{port}"},
        "default_resolver": request.default_resolver,
        "max_hop": request.max_hop,
        "cache_ttl_seconds": request.cache_ttl_seconds,
    }

    # Auth
    if request.auth_token:
        config_data["auth_token"] = request.auth_token
    if request.allowed_tokens:
        config_data["allowed_tokens"] = request.allowed_tokens
    if request.gateway_auth_token:
        config_data["gateway_auth_token"] = request.gateway_auth_token

    # Gateway topology
    if request.gateway_node_id:
        config_data["gateway_node_id"] = request.gateway_node_id
        config_data["gateway_address"] = request.gateway_address
        config_data["heartbeat_interval_seconds"] = request.heartbeat_interval_seconds
        config_data["poll_interval_seconds"] = request.poll_interval_seconds
        config_data["poll_interval_max_seconds"] = request.poll_interval_max_seconds
        config_data["poll_backoff_multiplier"] = request.poll_backoff_multiplier
    if request.trusted_nodes:
        config_data["trusted_nodes"] = request.trusted_nodes
        config_data["heartbeat_timeout_seconds"] = request.heartbeat_timeout_seconds
        config_data["ping_timeout_seconds"] = request.ping_timeout_seconds
        config_data["registration_policy"] = request.registration_policy
    if request.additional_gateways:
        config_data["additional_gateways"] = [
            {"address": gw.address, "auth_token": gw.auth_token}
            for gw in request.additional_gateways
        ]

    # LLM
    if request.llm:
        config_data["llm"] = {
            "provider": request.llm.provider,
            "model": request.llm.model,
            "api_key": request.llm.api_key,
        }
        if request.llm.base_url:
            config_data["llm"]["base_url"] = request.llm.base_url
    if request.skills_file:
        config_data["skills_file"] = request.skills_file

    # EventBus
    if request.event_bus:
        config_data["event_bus"] = {
            "enabled": request.event_bus.enabled,
            "max_log_size": request.event_bus.max_log_size,
            "delivery_timeout_seconds": request.event_bus.delivery_timeout_seconds,
            "delivery_retry_count": request.event_bus.delivery_retry_count,
        }

    # Scheduler
    if request.scheduler:
        config_data["scheduler"] = {"enabled": request.scheduler.enabled}
    if request.schedule:
        config_data["schedule"] = [
            {k: v for k, v in {
                "trigger_type": e.trigger_type,
                "run_action": e.run_action,
                "run_params": e.run_params or None,
                "check_action": e.check_action,
                "check_interval_seconds": e.check_interval_seconds if e.check_action else None,
                "cron_expression": e.cron_expression,
                "on_event_type": e.on_event_type,
                "on_channel": e.on_channel if e.on_event_type else None,
                "on_payload_filter": e.on_payload_filter,
                "description": e.description or None,
            }.items() if v is not None}
            for e in request.schedule
        ]

    # Task lifecycle
    if request.task_pool:
        config_data["task_pool"] = {
            "enabled": request.task_pool.enabled,
            "max_active_tasks": request.task_pool.max_active_tasks,
        }
    if request.checkpoint_store:
        config_data["checkpoint_store"] = {
            "enabled": request.checkpoint_store.enabled,
            "path": request.checkpoint_store.path,
            "default_timeout_seconds": request.checkpoint_store.default_timeout_seconds,
        }

    # Channel (gateway nodes)
    if request.channel_description:
        config_data["channel_description"] = request.channel_description

    # Extra nodes
    if request.extra_nodes:
        config_data["nodes"].update(request.extra_nodes)

    # Write file
    config_path = node_dir / "node.yaml"
    await asyncio.to_thread(
        lambda: yaml.dump(config_data, open(config_path, "w"), default_flow_style=False)
    )
    result.steps_completed.append("write_config")
```

---

### Solution B3: Post-bootstrap Wiring — ClusterOrchestrator

**New component:** `ClusterOrchestrator` — orchestrates the wiring phase after all nodes
are bootstrapped.

```python
# runtime/team_wirer.py

@dataclass
class ClusterSpec:
    """Complete specification for a team to be provisioned."""

    cluster_id: str
    gateway_node_id: str
    gateway_address: str
    gateway_auth_token: str
    project_name: str
    project_description: str

    members: list[NodeSpec]    # ordered: gateway first, then workers

    # Seed node wiring
    seed_should_join: bool = True   # seed joins as observer/coordinator
    seed_auth_token: str = ""       # token seed uses to join this team's gateway

@dataclass
class NodeSpec:
    """Spec for one node in the team."""
    node_id: str
    role: str                        # "gateway" | "pm" | "analyst" | "architect"
                                     # | "developer" | "tester" | "reviewer"
    bootstrap_request: BootstrapRequest
    subscriptions: list[SubscriptionSpec] = field(default_factory=list)


@dataclass
class SubscriptionSpec:
    """Event subscription to setup after node is running."""
    event_type_pattern: str
    callback_action: str
    description: str = ""
    on_channel: str = "global"      # "global" = subscribe on own gateway
    payload_filter: dict | None = None


class ClusterOrchestrator:
    """
    Orchestrates provisioning and wiring of a complete team.

    Phases:
      1. Bootstrap gateway node first (it must be up before workers register)
      2. Bootstrap worker nodes in parallel
      3. Wait for all nodes healthy
      4. Setup subscriptions for each node
      5. Wire seed node into team (add team gateway to seed's additional_gateways)
      6. Kickoff (emit cluster.started on team gateway)

    Rollback:
      If any phase fails, teardown all successfully-provisioned nodes.
    """

    async def provision_cluster(self, spec: ClusterSpec) -> ClusterProvisionResult: ...

    async def teardown_cluster(self, cluster_id: str) -> ClusterTeardownResult: ...

    async def _bootstrap_gateway(self, member: NodeSpec) -> BootstrapResult: ...

    async def _bootstrap_workers(self, members: list[NodeSpec]) -> list[BootstrapResult]: ...

    async def _setup_subscriptions(self, member: NodeSpec, gateway_address: str) -> None:
        """POST /subscribe to node's gateway for each subscription in spec."""

    async def _wire_seed(self, spec: ClusterSpec) -> None:
        """
        Add team's gateway to seed node's additional_gateways at runtime.
        This allows seed to receive events from the new team.

        Implementation: seed node's WorkerAgent needs a runtime API to
        add a new GatewayConnection without restart.
        → New endpoint: POST /gateways/connect
        """

    async def _kickoff(self, spec: ClusterSpec) -> None:
        """Emit cluster.started on team's gateway EventBus."""
```

**Standard role subscriptions (wired automatically by ClusterOrchestrator):**

```python
ROLE_SUBSCRIPTIONS: dict[str, list[SubscriptionSpec]] = {
    "pm": [
        SubscriptionSpec("*", "pm_observe_all", "PM watches everything"),
        SubscriptionSpec("human.*", "pm_escalate_human", "PM escalates human questions"),
        SubscriptionSpec("task.failed", "pm_handle_failure", "PM handles task failures"),
    ],
    "analyst": [
        SubscriptionSpec("cluster.started", "start_analysis", "Analyst starts on project kick"),
    ],
    "architect": [
        SubscriptionSpec("artifact.written", "start_design",
                         payload_filter={"artifact": "user-stories"}),
        SubscriptionSpec("clarification.answered", "handle_clarification_answer"),
    ],
    "developer": [
        SubscriptionSpec("artifact.written", "start_coding",
                         payload_filter={"artifact": "design"}),
        SubscriptionSpec("clarification.answered", "handle_clarification_answer"),
        SubscriptionSpec("review.changes_requested", "handle_review_feedback"),
    ],
    "tester": [
        SubscriptionSpec("artifact.written", "start_testing",
                         payload_filter={"artifact": "code"}),
    ],
    "reviewer": [
        SubscriptionSpec("artifact.written", "start_review",
                         payload_filter={"artifact": "code"}),
    ],
}
```

**New endpoint: `POST /gateways/connect` (runtime gateway join):**

```
Request:
{
  "address": "https://gateway-cluster-A.local:8091",
  "auth_token": "tok-seed-on-team-a"
}

Response 200:
{
  "connected": true,
  "gateway_node_id": "gateway-cluster-A",
  "channel_description": "Python Backend Team"
}
```

Cho phép seed node join một team's gateway **at runtime** mà không cần restart.
WorkerAgent adds a new `GatewayConnection` và starts loops.

---

### Solution B4: Provision State Machine

**Structured clarification output:**

```python
# Khi LLM trong /intent session đủ thông tin, nó structured output:

@dataclass
class OrchestrationPlan:
    """Structured plan output từ clarification phase."""

    confirmed: bool                    # user đã confirm plan
    teams: list[ClusterPlanSpec]
    dependencies: list[str]            # "cluster-A depends on cluster-B API"
    estimated_setup_minutes: int
    clarification_notes: list[str]     # outstanding ambiguities với assumptions

@dataclass
class ClusterPlanSpec:
    project_name: str
    project_description: str
    tech_stack: list[str]
    roles: list[NodeRolePlanSpec]
    timeline_weeks: int
    priority: str = "normal"           # "normal" | "high" | "critical"

@dataclass
class NodeRolePlanSpec:
    role: str
    count: int = 1
    specialization: str = ""          # e.g. "python", "mobile", "senior"
    blueprint_id: str | None = None   # if None, LLM will design
```

**Provision state tracking:**

```yaml
# Seed node tracks provision state in a file: provision_state.yaml

provisions:
  - provision_id: "prov-abc123"
    started_at: 1741392000.0
    status: "in_progress"   # "planning" | "in_progress" | "completed" | "failed" | "torn_down"
    teams:
      - cluster_id: "cluster-A"
        gateway_node_id: "gateway-cluster-A"
        status: "completed"
        nodes_provisioned: 6
        provisioned_at: 1741392300.0
      - cluster_id: "cluster-B"
        gateway_node_id: "gateway-cluster-B"
        status: "failed"
        error: "Port 8097 already in use"
        nodes_provisioned: 2
        nodes_rolled_back: 2
```

---

### Solution B5: Project context isolation convention

**Per-team context structure in `skills.md`:**

```markdown
<!-- Standard skills.md template for analyst role -->

# Role: Senior Analyst
## Identity
You are the analyst for **{PROJECT_NAME}**.

## Project Context
**Project:** {PROJECT_NAME}
**Description:** {PROJECT_DESCRIPTION}
**Tech Stack:** {TECH_STACK}
**Team:** {TEAM_ID}
**Timeline:** {TIMELINE}

## Your Responsibilities
- Analyze requirements for THIS project only
- Write user stories in context of {TECH_STACK}
- Do NOT reference other projects or teams

## Constraints
- You work exclusively on {PROJECT_NAME}
- When uncertain, emit clarification.needed — do NOT assume cross-project context
```

**LLM generation prompt (in seed's /intent):**

```
When generating skills.md for a team member, ALWAYS:
1. Include explicit "Project Context" section with project-specific details
2. Include explicit "Constraints" section forbidding cross-project references
3. Use placeholders like {PROJECT_NAME} filled with actual values
4. Keep project-specific facts (APIs, business rules) in this file — not in node.yaml
```

---

### Solution B6: Teardown

**`POST /teardown` endpoint:**

```
Request:
{
  "cluster_id": "cluster-A",        // teardown entire team
  "archive_logs": true,
  "archive_path": "/tmp/gnot-archive/cluster-A-2026-03-08"
}

// OR single node:
{
  "node_id": "analyst-A",
  "cluster_id": "cluster-A"
}
```

**Teardown steps (in `BootstrapEngine.teardown()`):**

```python
async def teardown(self, node_id: str, archive_path: str | None = None) -> TeardownResult:
    """
    Graceful node teardown:
    1. Signal node: POST /shutdown (new endpoint on target node)
       → Node completes in-flight jobs, rejects new
       → Node emits node.shutting_down event
    2. Wait for graceful stop (timeout: 30s)
    3. Force kill if needed (SIGTERM → SIGKILL)
    4. Archive event log if requested
    5. Remove from seed's trusted_nodes
    6. Remove node directory
    """
```

**`POST /shutdown` endpoint (on every node):**

```
Request: {} (no body needed)
Response 200: {"status": "shutting_down", "eta_seconds": 10}

Behavior:
- Stop accepting new jobs (return 503 to new requests)
- Complete current jobs
- Emit node.shutting_down event on all connected gateways
- Exit process cleanly
```

---

## 5. End-to-end flow — v6.3 complete scenario

```
═══════════════════════════════════════════════════════════
PHASE 1: CLARIFICATION (seed /intent session)
═══════════════════════════════════════════════════════════

User → POST /intent (seed node-0):
  "Tạo 2 dev team: Cluster A Python backend API, Cluster B React Native mobile.
   Cluster A cần 2 senior devs, Cluster B cần 1 mobile specialist."

Seed LLM:
  Turn 1: GET /blueprints → list available blueprints
  Turn 2: "Cluster A timeline?" → [CLARIFICATION]
  User: "Cluster A 3 tháng, Cluster B 2 tháng"
  Turn 3: "Cluster A cần reviewer riêng không?" → [CLARIFICATION]
  User: "Có, cả hai teams đều cần reviewer"
  Turn 4: LLM has enough → generate OrchestrationPlan
  Turn 5: Present plan → ask confirmation
  User: "OK proceed"

═══════════════════════════════════════════════════════════
PHASE 2: PROVISIONING
═══════════════════════════════════════════════════════════

Seed LLM orchestrates via mesh_action:

Step 1: Blueprint selection
  GET /blueprints → found: standard-cluster, developer-python
  No "developer-mobile" blueprint → LLM designs → POST /blueprints/save

Step 2: Build ClusterSpec for cluster-A
  {
    cluster_id: "cluster-A",
    gateway_node_id: "gateway-cluster-A",
    gateway_address: "http://localhost:8091",
    project_name: "Python Backend API",
    members: [
      {role: "gateway", bootstrap: BootstrapRequest(
        node_id: "gateway-cluster-A",
        listen: "0.0.0.0:8091",
        trusted_nodes: [],        # open registration
        registration_policy: "open",
        event_bus: EventBusSpec(),
        auth_token: "tok-gw-cluster-a",
        allowed_tokens: ["tok-seed-on-a", "tok-pm-a", ...]
      )},
      {role: "pm", bootstrap: BootstrapRequest(
        node_id: "pm-cluster-A",
        listen: "0.0.0.0:8092",
        gateway_node_id: "gateway-cluster-A",
        gateway_address: "http://localhost:8091",
        gateway_auth_token: "tok-pm-a",
        llm: LLMSpec(...),
        skills_md: <pm blueprint + project context>,
        event_bus: EventBusSpec(),
        scheduler: SchedulerSpec(),
        task_pool: TaskPoolSpec(),
        checkpoint_store: CheckpointStoreSpec(),
      )},
      ... (analyst, architect, dev×2, reviewer)
    ]
  }

Step 3: POST /bootstrap (via ClusterOrchestrator)
  → Bootstrap gateway-cluster-A (Step 6a: start, Step 7: health-check)
  → Bootstrap workers in parallel (pm, analyst, architect, dev1, dev2, reviewer)
  → All 7 nodes healthy

Step 4: Wire subscriptions
  → POST /subscribe on gateway-cluster-A for each member's standard subscriptions
  → analyst: cluster.started → start_analysis
  → architect: artifact.written (user-stories) → start_design
  → dev: artifact.written (design) → start_coding
  → test: artifact.written (code) → start_testing
  → reviewer: artifact.written (code) → start_review
  → pm: * → pm_observe_all

Step 5: Same for cluster-B
  → 6 nodes provisioned and wired

Step 6: Seed joins both teams
  → POST /gateways/connect (gateway-cluster-A) → seed joins cluster-A
  → POST /gateways/connect (gateway-cluster-B) → seed joins cluster-B
  → Seed subscribes "participant.input_required" from both

Step 7: Kickoff
  → POST /emit on gateway-cluster-A: {event_type: "cluster.started", payload: {...}}
  → POST /emit on gateway-cluster-B: {event_type: "cluster.started", payload: {...}}

═══════════════════════════════════════════════════════════
PHASE 3: RUNTIME
═══════════════════════════════════════════════════════════

Teams operate autonomously (v6.0/6.1/6.2 mechanisms):

Cluster A:
  analyst-A: receives cluster.started → starts analysis → writes user-stories.md
  analyst-A: emits artifact.written {artifact: user-stories}
  architect-A: receives event → starts design → writes design.md
  dev1-A + dev2-A: receive event → start coding
  dev1-A: hits ambiguity → suspend_and_ask → emit clarification.needed
  seed: receives participant.input_required → formats → sends to user
  user: answers → emit participant.answered → dev1-A resumes

Cluster B: runs independently, isolated on gateway-cluster-B

═══════════════════════════════════════════════════════════
PHASE 4: COMPLETION & TEARDOWN
═══════════════════════════════════════════════════════════

Cluster A completes:
  reviewer-A: emit cluster.completed
  seed: receives event → notify user → ask teardown?
  user: "yes"
  seed: POST /teardown {cluster_id: "cluster-A", archive_logs: true}
  → ClusterOrchestrator.teardown_cluster("cluster-A")
  → All 7 nodes gracefully shut down, archived, removed
```

---

## 6. New HTTP endpoints — v6.3

| Endpoint | Node | Description |
|----------|------|-------------|
| `GET /blueprints` | seed | List all blueprints (roles, teams, generated) |
| `GET /blueprints/{type}/{id}` | seed | Get blueprint content |
| `POST /blueprints/save` | seed | Save LLM-generated blueprint for reuse |
| `DELETE /blueprints/{type}/{id}` | seed | Remove blueprint |
| `POST /teams/provision` | seed | Provision complete team from ClusterSpec |
| `GET /clusters` | seed | List active clusters |
| `GET /teams/{cluster_id}` | seed | Team status + member list |
| `POST /teams/{cluster_id}/kickoff` | seed | Emit cluster.started on team gateway |
| `POST /teardown` | seed | Teardown team or single node |
| `POST /gateways/connect` | any | Runtime gateway join (no restart) |
| `POST /shutdown` | any | Graceful node shutdown |

---

## 7. Files thay đổi — v6.3

| File | Type | Description |
|------|------|-------------|
| `runtime/bootstrap.py` | MODIFY | BootstrapRequest v6.3 with all fields; `_step_write_config` full rewrite; add `teardown()` method |
| `runtime/team_wirer.py` | NEW | ClusterOrchestrator: provision + teardown orchestration |
| `runtime/blueprint_store.py` | NEW | Blueprint storage, INDEX.yaml management |
| `runtime/server.py` | MODIFY | New endpoints: /blueprints, /teams, /teardown, /shutdown, /gateways/connect |
| `runtime/models.py` | MODIFY | OrchestrationPlan, ClusterSpec, NodeSpec, ClusterProvisionResult, TeardownResult, BlueprintInfo |
| `runtime/worker_agent.py` | MODIFY | Runtime gateway connect (add GatewayConnection without restart) |
| `runtime/config.py` | MODIFY | LLMSpec, EventBusSpec, SchedulerSpec, etc. as config dataclasses |
| `blueprints/roles/pm.md` | NEW | PM role blueprint |
| `blueprints/roles/analyst.md` | NEW | Analyst role blueprint |
| `blueprints/roles/architect.md` | NEW | Architect role blueprint |
| `blueprints/roles/developer.md` | NEW | Generic developer blueprint |
| `blueprints/roles/developer-python.md` | NEW | Python developer blueprint |
| `blueprints/roles/developer-mobile.md` | NEW | Mobile developer blueprint |
| `blueprints/roles/tester.md` | NEW | Tester role blueprint |
| `blueprints/roles/reviewer.md` | NEW | Reviewer role blueprint |
| `blueprints/teams/standard-cluster.yaml` | NEW | Standard team composition |
| `blueprints/teams/minimal-cluster.yaml` | NEW | Lean team composition |
| `blueprints/INDEX.yaml` | NEW | Blueprint catalog |
| `docs/worklog/SPECS_V6.3.md` | NEW | This document |
| `docs/worklog/WORKLOG_V6.3.md` | NEW | Implementation worklog |

---

## 8. Updated readiness — full v6.x stack

```
v6.0: EventBus · Scheduler · 3-level autonomy
v6.1: Gateway = Channel · Multi-gateway membership · Full equality
v6.2: Task suspension/resumption · Human-in-the-loop · CheckpointStore
v6.3: Self-provisioning · Blueprint system · Dynamic team creation · Teardown

After full v6.x implementation:

  User experience:
    ✅ Natural language provisioning request
    ✅ Multi-turn clarification before commit
    ✅ Teams auto-created from blueprints or LLM-designed
    ✅ Teams self-wire subscriptions and start autonomously
    ✅ Human questions escalated during runtime
    ✅ Graceful teardown when done

  Team behavior:
    ✅ Agents communicate freely within team
    ✅ Cross-team members (multi-gateway)
    ✅ Task suspension when blocked
    ✅ Task switching while blocked
    ✅ Task resumption with context after answer
    ✅ Escalation chain: agent → agent → human
    ✅ Self-starting agents (all 3 autonomy levels)
    ✅ PM watches all events, manages escalations

  Infrastructure:
    ✅ Seed node = always-on orchestrator
    ✅ Blueprint reuse + LLM-generated blueprints
    ✅ Runtime gateway join (no restart for cross-team)
    ✅ Provision state tracking + partial failure rollback
    ✅ Clean teardown + log archiving

  Still not achievable (~10%):
    ❌ Implicit peer knowledge across sessions
    ❌ Emergent social dynamics
    ❌ True domain intuition without explicit context
```

---

## 9. Open questions — v6.3

**Q1: Port allocation for dynamically provisioned nodes**
Node listen trên port nào? Manual trong ClusterSpec? Auto-allocate từ range?
Cần: `port_range: [8090, 8200]` trong seed config, auto-assign khi provision.

**Q2: Node discovery sau restart**
Seed node restart → mất knowledge về active teams + PIDs.
Provision state file giải quyết một phần, nhưng PIDs thay đổi sau restart.
Cần process manager (systemd units per node? pm2? managed by seed?).

**Q3: Distributed teams — nodes trên nhiều máy**
Toàn bộ v6.3 giả định nodes trên cùng một machine (local process bootstrap).
Distributed provisioning (bootstrap node trên remote server) cần different mechanism.
→ Scope: v6.4 hoặc later. v6.3 = single-machine only.

**Q4: Blueprint versioning và compatibility**
Blueprint version "1.0" tương thích với GNOT v6.x.
Nếu GNOT upgrade, blueprints cũ có còn dùng được không?
→ Cần compatibility matrix hoặc blueprint migration tool.

**Q5: LLM-designed blueprint quality**
LLM generate blueprint lần đầu → quality không đảm bảo.
Cần review step trước khi save + reuse?
→ Option: save as "draft", require human approval trước khi promote to "stable".

---

*Spec: SPECS_V6.3.md | Mesh Runtime v6.3 | Repository: ai-infra-runtime-v2*
*Builds on: SPECS_V6.2.md, SPECS_V6.1.md, SPECS_V6.0.md*
*Problem statement: product owner review session, 2026-03-08*
