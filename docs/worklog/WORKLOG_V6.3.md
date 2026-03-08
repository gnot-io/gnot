# Worklog — Execution Mesh v6.3
## Self-Provisioning Teams · Seed-as-Orchestrator · Blueprint System

**Project:** ai-infra-runtime-v2
**Base:** v6.2 → v6.3
**Session:** Architecture review, 2026-03-08
**Status:** Analysis complete — design pending

---

## 1. Trigger và bài toán gốc

Product owner đặt vision:

> "Anh kỳ vọng sau khi hệ thống này đã được implement hoàn chỉnh thì user có thể
> gởi yêu cầu như 'hãy chuẩn bị 2 dev team để làm 2 dự án dưới đây...'. Hệ thống
> có thể hỏi để làm rõ thêm thông tin. Khi đó các team sẽ tự động được tạo ra,
> trong quá trình hoạt động có thể hỏi tiếp thông tin từ user..."

Sau khi em present preliminary gap analysis, product owner clarify hai điểm:

**Clarification 1 — Gap A:**
> "Theo thiết kế, các node nếu có cấu hình LLM thì đều có thể hỗ trợ /intent
> endpoint; nếu chúng ta cho node đó quyền provision cluster thì nó có thể tạo
> ra các node khác. Thông thường seed node sẽ có quyền này."

**Clarification 2 — Blueprint:**
> "Khi tạo node mới, nếu hệ thống đã có sẵn blueprint/template thì sử dụng chúng,
> otherwise thì LLM có thể tự thiết kế ra."

Hai clarifications này thay đổi đáng kể gap analysis. Em phải re-read code
trước khi conclude.

---

## 2. Re-reading code trước khi conclude

### 2.1 Tại sao đọc lại code

Preliminary analysis dựa vào memory về architecture. Product owner's clarification
gợi ý rằng một số "gaps" có thể đã được giải quyết bởi existing design.

Specific question: **Bootstrap engine có đủ để tạo functional v6.x nodes không?**

### 2.2 Findings từ `bootstrap.py`

Đọc `_step_write_config`:

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

Confirm: `BootstrapRequest` chỉ có v5.x fields. Node được tạo sẽ không có:
- `gateway_node_id` / `gateway_address` → không join gateway
- `llm` config → không có `/intent`, không có `llm_chat`
- `event_bus` → không có EventBus
- `scheduler` → không có subscriptions
- `task_pool` / `checkpoint_store` → không có v6.2 features

**Gap B2 (BootstrapRequest thiếu v6.x fields) là CONFIRMED CRITICAL.**

### 2.3 Findings từ seed node analysis

Seed node (node-0) với LLM config:
- Có `/intent` endpoint ✅
- Có `/bootstrap` endpoint ✅
- Có `execute_command`, `write_file`, `read_file` seed actions ✅
- Có `mesh_action` tool trong ReAct loop ✅

Product owner đúng: seed node + LLM = orchestrator đã đủ capability.
**Gap A không phải gap thực sự — đã covered.**

---

## 3. Gap reclassification sau code review

### 3.1 Original gaps (preliminary)

```
Gap A: Cần Orchestrator node riêng
Gap B: Dynamic provisioning pipeline
  B1: Blueprint storage
  B2: BootstrapRequest thiếu v6.x fields
  B3: Post-bootstrap wiring
  B4: Clarification-to-provision transition
  B5: Project context isolation
  B6: Teardown
```

### 3.2 Reclassified

```
Gap A: NOT A GAP — seed node đã là orchestrator
Gap B1: REAL — cần convention + endpoints
Gap B2: REAL, CRITICAL — BootstrapRequest phải support v6.x
Gap B3: REAL, CRITICAL — cần TeamWirer sau bootstrap
Gap B4: REAL, IMPORTANT — cần ProvisionPlan structure
Gap B5: REAL — convention đủ, không cần code
Gap B6: REAL — cần /teardown + /shutdown endpoints
```

---

## 4. Key design decisions

### 4.1 Seed node permission model

**Câu hỏi:** Làm thế nào seed biết nó có quyền provision?

**Option A:** Hard-coded — seed luôn có `/bootstrap` endpoint.

**Option B:** Config flag — `provision_authority: true` trong node.yaml.

**Option C:** Token-based — certain tokens có provision rights.

**Quyết định: Option B** — explicit config flag `provision_authority: true`.

Rationale:
- Explicit beats implicit: operator biết rõ node nào có quyền này
- Security: không phải mọi LLM node đều nên có quyền tạo nodes mới
- Flexible: bất kỳ node nào có LLM + `provision_authority: true` đều có thể là orchestrator
  (không chỉ node-0)
- Consistent với registration_policy pattern (explicit opt-in)

```yaml
# node-0/node.yaml
provision_authority: true    # This node can provision new nodes
blueprints_dir: ./blueprints # Where to store/read blueprints
```

Khi `provision_authority: false` (default), `/bootstrap`, `/teams`, `/blueprints`
endpoints trả về 403.

### 4.2 Blueprint: file-based hay DB?

**Option A: File-based** (JSONL + YAML + Markdown files)
- Consistent với philosophy của GNOT (stateless, file-based)
- Human-readable — operator có thể edit blueprints bằng text editor
- Version control friendly (git)
- INDEX.yaml là single source of truth

**Option B: SQLite DB**
- Better querying (tags, full-text search)
- Atomic updates
- Nhưng: binary, không git-friendly, thêm dependency

**Quyết định: Option A — file-based.**

Rationale: Blueprint là configuration, không phải operational data.
Configuration thuộc về file system, không phải database.
INDEX.yaml đủ cho querying needs hiện tại.

### 4.3 TeamWirer: separate service hay orchestration trong /intent?

**Option A: TeamWirer là separate class, /intent gọi vào**

```
/intent → LLM → mesh_action("provision_team", TeamSpec) → TeamWirer.provision_team()
```

**Option B: LLM orchestrate trực tiếp, không có TeamWirer**

```
/intent → LLM → mesh_action("bootstrap", node1) → mesh_action("bootstrap", node2) → ...
              → mesh_action("subscribe", ...) → ...
```

**Quyết định: Option A — TeamWirer là separate class.**

Rationale:
- LLM không nên orchestrate individual bootstrap calls — quá nhiều steps,
  quá nhiều opportunity cho failure mà LLM không handle được cleanly
- TeamWirer có transactional semantics: nếu node 3/6 fail, rollback 1-2
  LLM không có reliable rollback logic
- Separation of concerns: LLM decides WHAT to provision, TeamWirer decides HOW
- Reusability: TeamWirer có thể được gọi từ API trực tiếp, không chỉ từ /intent

### 4.4 Subscription wiring: static defaults hay dynamic?

**Option A: Standard subscriptions per role (ROLE_SUBSCRIPTIONS dict)**
- Pre-defined subscriptions cho từng role
- TeamWirer automatically wire khi provision

**Option B: LLM decides subscriptions per node**
- Flexible nhưng không predictable
- LLM có thể miss critical subscriptions

**Option C: Hybrid (chosen)**
- ROLE_SUBSCRIPTIONS cung cấp sensible defaults
- Blueprint có thể override defaults
- LLM có thể thêm custom subscriptions trong TeamSpec

Rationale: "convention over configuration" — defaults đúng 80% cases.
Blueprint overrides cho specialized roles. LLM chỉ cần specify deviations.

### 4.5 Runtime gateway join — restart hay no-restart?

Khi seed node cần join một team gateway SAU KHI đã running:

**Option A: Restart seed với updated config**
- Simple implementation
- Downtime trong seed node
- User loses active /intent session

**Option B: Runtime gateway connect (no restart)**
- `POST /gateways/connect` → WorkerAgent adds new GatewayConnection
- No downtime
- Complex implementation (WorkerAgent không designed cho runtime modification)

**Quyết định: Option B** cho user experience lý do.

Rationale: Seed node là "always-on orchestrator". Nếu seed restart sau mỗi
team provision, user experience bị disrupted. User đang có /intent session
để track provisioning — session phải còn sống.

Implementation: WorkerAgent cần expose method `add_gateway_connection(config)`.
Method này tạo GatewayConnection instance mới và starts loops.
`_connections` list cần become thread-safe (asyncio.Lock).

### 4.6 Port allocation

**Vấn đề:** Khi provision N nodes dynamically, port nào dùng?

**Option A: Operator specifies in request**
- Full control nhưng burden on operator/LLM

**Option B: Auto-allocate từ range**
- Seed config: `port_range: [8090, 8200]`
- Seed tracks allocated ports trong `provision_state.yaml`
- Auto-assign next available port

**Option C: OS-assigned (port 0)**
- OS chọn available port
- Nhưng: không predictable, hard to configure firewall

**Quyết định: Option B** — auto-allocate từ configurable range.

```yaml
# node-0/node.yaml
provision_authority: true
port_range:
  start: 8090
  end: 8200
```

Seed tracks: `allocated_ports: [8090, 8091, 8092, ...]` trong provision_state.yaml.
Khi teardown → release port về pool.

### 4.7 Process management sau restart

**Vấn đề:** Seed node restart → PIDs của provisioned nodes bị mất.
Nodes vẫn running nhưng seed không biết PIDs → không thể teardown.

**Option A: Seed re-discovers processes sau restart**
- Đọc provision_state.yaml → tìm node directories → check PID files
- Mỗi node lưu PID trong `{node_dir}/node.pid`

**Option B: External process manager (systemd, pm2)**
- Reliable nhưng thêm external dependency
- Complex setup

**Quyết định: Option A — PID file per node.**

Bootstrap thêm step: write PID file sau khi process start.
Seed re-reads PID files khi needed (teardown, health-check).

```
node-analyst-A/
├── node.yaml
├── skills.md
├── actions/
├── node.pid          ← PID file (NEW)
└── checkpoints/      ← v6.2 checkpoint store
```

---

## 5. Design tensions

### 5.1 LLM autonomy vs. predictability

Blueprint system tạo tension:
- **High LLM autonomy**: LLM designs everything, blueprints rarely used
  → Flexible nhưng unpredictable, expensive (LLM call per node)
- **Low LLM autonomy**: Pre-defined blueprints for everything
  → Predictable nhưng rigid, can't handle novel project types

**Resolution**: Blueprint-first, LLM-fallback.
1. Query blueprints
2. If exact match → use blueprint (no LLM design needed)
3. If partial match → use blueprint as base, LLM customizes diff
4. If no match → LLM designs from scratch → save as new blueprint

This way LLM design cost is amortized: first time expensive, subsequent times free.

### 5.2 Provision atomicity vs. partial success

**All-or-nothing**: Either full team or nothing.
- Clean but: nếu node 6/7 fail, restart tất cả → expensive

**Partial success allowed**: Team runs với fewer nodes than planned.
- Pragmatic: team-A có 5/6 nodes vẫn có thể start
- Nhưng: complex status tracking, team behavior unpredictable

**Resolution**: All-or-nothing at team level, với retry at node level.
- Node bootstrap: retry 3 lần trước khi fail
- Team: nếu bất kỳ node nào fail sau retry → rollback toàn team
- Hai teams: independent — team-A fail không rollback team-B

### 5.3 Blueprint quality: LLM-generated vs. human-curated

LLM-generated blueprints có thể:
- Thiếu important actions
- Có incorrect event subscriptions
- Miss project-specific constraints

**Resolution**: Blueprint lifecycle:
```
generated → draft → reviewed → stable
```
- `generated`: LLM output, not yet validated
- `draft`: human has reviewed, considered usable
- `stable`: battle-tested, default choice for matching requests
- Index.yaml tracks lifecycle stage per blueprint
- When selecting blueprint: prefer `stable` > `draft` > `generated`

---

## 6. What v6.3 reveals about v6.0-v6.2

### 6.1 BootstrapRequest design intent vs. reality

`BootstrapRequest` docstring says:
> "Provides a high-level API for the Cloud AI Planner to create new nodes in the mesh."

The "Cloud AI Planner" in the docstring is exactly what seed node + LLM IS.
The original design intent anticipated LLM-driven provisioning.

But the implementation stopped at v5.x fields. V6.x features were designed
(specs exist) but bootstrap was never updated to reflect them.

**V6.3 closes this gap:** BootstrapRequest becomes the complete node specification
that the original intent described.

### 6.2 The emergent architecture

Looking at v6.0 → v6.1 → v6.2 → v6.3 together:

```
v6.0: "How do agents communicate and coordinate?"
      → EventBus + Scheduler + channels

v6.1: "What IS a channel? How do multi-team members work?"
      → Gateway = Channel, register = join

v6.2: "Can agents handle blocking situations like real developers?"
      → Task suspension + resume + human escalation

v6.3: "Who creates these agents in the first place?"
      → Seed orchestrates, blueprints enable, teams self-provision
```

Each version answered one level of the question "can we build a real AI dev team?"
V6.3 is the answer to "but who builds the team?"

The answer: **the system builds itself**, seeded by a single always-on node.

This is the "self-bootstrapping" in the product name finally fully realized:
- "Self": the system creates its own components
- "Bootstrapping": from a single seed node, entire teams emerge
- "Execution Mesh": the resulting network executes real projects autonomously

### 6.3 The v6.3 loop

```
User                Seed               Teams
  |                   |                   |
  |── "create teams"→ |                   |
  |← clarify ────────|                   |
  |── answers ──────→|                   |
  |                   |── provision ─────→|
  |                   |                   |── work autonomously
  |                   |                   |
  |← question ───────|←── escalate ──────|
  |── answer ────────→|                   |
  |                   |── answer ─────────→|
  |                   |                   |── resume task
  |                   |                   |
  |← "done" ─────────|←── complete ───────|
  |── "teardown" ────→|                   |
  |                   |── teardown ───────→|
  |                   |                   X (nodes removed)
```

This loop was not fully possible before v6.3:
- Before v6.2: teams couldn't ask user questions mid-work
- Before v6.1: teams couldn't have cross-team members
- Before v6.0: teams weren't event-driven
- Before v6.3: teams couldn't be created dynamically

---

## 7. What remains after v6.3

### 7.1 The remaining 10% — fundamental limits

After full v6.x implementation, ~10% of "real pro dev team" behavior remains
unachievable. Analysis of WHY:

**Implicit peer knowledge:**
Real teams develop shared understanding through months of working together.
"Alice always forgets error handling" is knowledge that emerges from observation,
not from any single interaction.

GNOT has no cross-session agent memory. Each session is fresh. This is
fundamental to stateless design — and it means agents can't "know" each other.

Possible future direction: persistent agent-to-agent knowledge graph (v7.x).
But this requires non-trivial design: who owns the knowledge? Who updates it?
When does it expire? How do we prevent knowledge poisoning?

**Emergent social dynamics:**
Real team cohesion, psychological safety, humor, and informal communication
are emergent from human social cognition. LLMs can simulate aspects of this
but cannot genuinely develop team identity.

This is not an infrastructure gap — it's a fundamental capability boundary.
The system can coordinate agents but cannot make them a "team" in the human sense.

**True domain intuition:**
A senior developer doesn't just know Python syntax — they have internalized
years of project experience, architectural patterns, common failure modes.

LLMs have broad domain knowledge from training but not project-specific
institutional knowledge. Blueprint context injection (v6.3) helps significantly
but cannot fully substitute for genuine domain expertise accumulated over time.

### 7.2 Single-machine limitation (v6.3 scope)

V6.3 provisions nodes as local processes on the same machine as seed node.
Distributed provisioning (bootstrap node on remote server via SSH, Docker, K8s)
is explicitly out of scope.

Why out of scope: distributed provisioning requires network connectivity management,
credential distribution, health-check across network boundaries, and distributed
rollback. This is a separate infrastructure concern from team coordination logic.

Possible future: v6.4 distributed provisioning via bootstrap agents on remote nodes.

---

## 8. Summary: the four-version journey

```
Start: v5.13b
  Single-gateway nodes, pull-mode jobs, no events, no autonomy
  "Can run a job if you tell it exactly what to do"

After v6.0:
  EventBus, pub/sub, scheduler, 3-level autonomy
  "Agents react to events and can self-start"

After v6.1:
  Gateway = Channel, multi-gateway membership, full equality
  "Multiple teams can exist and share members"

After v6.2:
  Task suspension/resumption, human-in-the-loop, checkpoint
  "Agents handle blocking situations like real developers"

After v6.3:
  Self-provisioning, blueprints, dynamic team creation, teardown
  "One request creates entire teams from nothing"

Net result:
  User: "Build me 2 dev teams for these projects"
  System: clarifies → provisions → runs → escalates → completes → cleans up
  User: never touches individual nodes, never writes configs, never manages processes
```

---

*Worklog: WORKLOG_V6.3.md | Mesh Runtime v6.3 | Repository: ai-infra-runtime-v2*
*Previous: WORKLOG_V6.2.md*
*Problem statement: product owner review session, 2026-03-08*
