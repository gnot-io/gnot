# Worklog — Execution Mesh v6.0
## Event-Driven Infrastructure · Autonomous Agent Primitives · Channel-Scoped Communication

**Project:** ai-infra-runtime-v2  
**Base:** v5.13b → v6.0  
**Session:** Architecture review, 2026-03-08  
**Status:** Design complete, pre-implementation

---

## 1. Trigger

Phân tích Guide 23 (AI Dev Team) cho thấy pipeline PM-orchestrated hiện tại
có ba vấn đề cơ bản khi so sánh với một pro dev team thực tế:

1. **Linear waterfall** — analyst xong mới architect bắt đầu, architect xong mới
   dev bắt đầu. Không có parallelism, không có early feedback.

2. **PM là single coordinator** — mọi node giao tiếp qua PM, PM phải gọi từng bước.
   Nếu PM bận → toàn bộ pipeline đình trệ.

3. **Nodes hoàn toàn passive** — không node nào tự biết cần làm gì.
   Mọi hành động đều chờ được trigger từ bên ngoài.

Câu hỏi đặt ra: nếu thêm scheduler/trigger, event-based architecture, event watcher,
và event bus vào GNOT, liệu có thiết kế được AI dev team giống real team?

---

## 2. Phân tích kiến trúc hiện tại trước khi đề xuất

### 2.1 Những gì đã đọc

Đọc toàn bộ core runtime:
- `node_registry.py` — BGP routing, lazy staleness check
- `router.py` / `gateway_router.py` — push/pull dispatch
- `worker_agent.py` — heartbeat + poll loop
- `job_queue.py` — pull-mode queue, routing table
- `server.py` — endpoint inventory, middleware stack
- `config.py` — full NodeConfig schema, defaults
- `models.py` — all Pydantic models, enums
- `intent_handler.py` — ReAct loop, MESH_TOOL_SPEC
- `conversation_store.py` — in-memory session, lazy TTL

### 2.2 Phát hiện quan trọng

**WorkerAgent poll loop** — `_poll_loop` trong worker_agent.py là Level 2 autonomy
đã có sẵn. Fixed 5s interval. Đây là nền tảng tốt để build adaptive polling.

**ConversationStore là in-memory** — Sessions mất khi restart. Đây là known limitation
("On restart: sessions are lost (in-memory). This is intentional — session persistence
is a P4 item"). v6.0 plan P3-b sẽ address.

**Không có event primitive nào** — Không có `EventBus`, `Subscription`, `Scheduler`,
hay bất kỳ pub/sub mechanism nào. Gap hoàn toàn.

**`llm_chat` action** — Seed action cho phép bất kỳ node nào gọi LLM với custom system
prompt. Đây là building block quan trọng cho agent behavior. Node có `skills.md` định
nghĩa persona + decision rules.

---

## 3. Design decisions

### 3.1 Yêu cầu 1: Channels

**Vấn đề cụ thể:** Nhiều dev teams trên cùng mesh. Team A emit event "test.failed" →
không muốn Team B nhận. Team B emit "design.proposed" → không liên quan đến Team A.

**Options phân tích:**

**Option A: Virtual channel trong NodeRegistry (label-based)**
- Thêm `channel_id` vào `_NodeEntry` trong NodeRegistry
- `POST /broadcast?channel=dev-team-1` loop qua nodes có cùng channel
- Đơn giản, không thay đổi routing core
- Nhược: NodeRegistry phình to, mixing concerns (registry vs. messaging)

**Option B: Channel-as-node (routing-based)**
- Mỗi channel là một virtual node ID
- Resolver expand `target_node_id: "channel:dev-team-1"` thành list of members
- Tái dụng routing layer hiện tại
- Nhược: resolver phức tạp, không rõ ràng về ownership

**Option C: Dedicated ChannelRegistry module (separation of concerns)**
- Module riêng, rõ ràng, không mix với NodeRegistry
- Static membership từ `node.yaml`, dynamic qua API
- Channel là namespace cho EventBus — không phải routing primitive
- Nhược: thêm một class mới

**Quyết định: Option C.**

Lý do:
- NodeRegistry về routing (reachability, push/pull). ChannelRegistry về messaging scope.
  Hai concerns khác nhau, không nên mix.
- Channel membership stable (defined in config), không thay đổi như heartbeat status.
- Kiến trúc rõ ràng hơn: `EventBus.emit(event)` → check channel membership →
  fan-out to subscribers. Không cần router biết về channels.

**Channel role design:**

Ban đầu xem xét chỉ có "member". Sau đó thêm "observer" và "owner":
- Observer: nhận events nhưng không emit — useful cho PM watching team, hoặc
  audit node watching everything
- Owner: có thể kick members — useful cho PM của một team

Đây là simple RBAC đủ dùng mà không cần full ACL system.

---

### 3.2 Yêu cầu 2: Ba cấp độ autonomy

**Level 1 — Self-check:** Cơ chế này không có analog trong bất kỳ existing GNOT primitive nào.

Cách tiếp cận khác nhau được xem xét:

**Approach A: File system watch (inotify)**
```python
# Node watch filesystem events
inotify.watch("/tmp/devteam/", IN_CREATE)
# Khi requirement.txt được tạo → trigger action
```
Nhược: system-specific, yêu cầu pyinotify dependency, không portable across OS.
Không nhất quán với GNOT philosophy (zero external deps).

**Approach B: Declarative condition expression**
```yaml
schedule:
  - trigger_type: condition
    condition_expr: "file_exists('/tmp/devteam/*/requirement.txt') AND NOT file_exists('/tmp/devteam/*/user-stories.md')"
```
Nhược: cần implement expression evaluator. Complex, error-prone, không flexible.

**Approach C: check_action pattern (chosen)**
```python
# Node tự define check_action — một action nhỏ trả về {should_run: bool}
async def analyst_self_check(params, context) -> dict:
    # ... any logic: file check, DB query, API call, time check ...
    return {"should_run": True/False, **extra_context}
```

**Quyết định: Approach C.**

Lý do:
- Completely flexible — node tự define logic kiểm tra, không bị giới hạn bởi expression syntax
- Nhất quán với existing pattern: actions là unit of computation trong GNOT
- Extra context từ check_action được pass vào run_action — data flows naturally
- Test-able: check_action có thể được test độc lập qua `POST /action`
- Không cần thêm expression evaluator hay filesystem watch

**Anti-thundering-herd design:**
Khi nhiều nodes cùng check condition → cùng thấy project cần xử lý → cùng start.
Giải pháp: lock file convention (`<workspace>/.analyst_lock`). Check_action là responsible
for creating + checking lock. Đây là application-level concern, không cần Scheduler
phải biết về it.

**Level 2 — Adaptive polling:**

Fixed 5s là fine cho interactive workloads. Nhưng khi cluster idle overnight,
mỗi node poll mỗi 5s = 12 calls/minute/node = nhiều unnecessary load.

Exponential backoff với ceiling:
- Không tăng vô hạn (ceiling tránh miss time-sensitive jobs)
- Reset ngay khi nhận được job (stay responsive khi có work)
- Configurable via `poll_interval_max_seconds` + `poll_backoff_multiplier`

Tại sao không WebSocket/SSE thay cho polling?
- Pull mode là fundamental design của GNOT cho NAT traversal
- Nodes sau NAT không thể nhận inbound connections
- Pull mode giữ architecture consistent cho cả nodes có và không có self_address
- WebSocket/SSE sẽ break NAT traversal model

**Level 3 — Event-triggered:**

Đây là reactive side của pub/sub. Subscription trong EventBus khi matched → Scheduler
trigger callback_action. Hai components tách nhau:
- EventBus: delivery concern (match, fan-out, retry)
- Scheduler: trigger concern (what to do when event arrives)

Trong thực tế Scheduler event entries đăng ký Subscription trong EventBus, với
callback là internal handler. Khi EventBus deliver → Scheduler execute run_action.
Single responsibility giữa hai modules.

---

### 3.3 EventBus: In-process vs. External broker

**Câu hỏi:** Tại sao không dùng Redis Pub/Sub, RabbitMQ, NATS, hoặc Kafka?

**Phân tích:**

| | In-process | Redis | NATS | Kafka |
|--|----------|-------|------|-------|
| External deps | None | Redis server | NATS server | Kafka + ZK |
| Setup complexity | Zero | Low | Low | High |
| Durability | ❌ (RAM) | ✅ (AOF) | ✅ | ✅ (persistent) |
| Throughput | Medium | High | Very high | Very high |
| Latency | ~0ms | ~1ms | ~1ms | ~5-50ms |
| Cluster support | ❌ | ✅ | ✅ | ✅ |
| GNOT philosophy | ✅ | ❌ | ❌ | ❌ |

GNOT design philosophy từ specs đầu tiên: **zero external dependencies**.
Node runtime là một single Python process. Không cần Docker, không cần Redis,
không cần bất kỳ external service nào ngoài Python stdlib + small pip packages.

Đây là conscious trade-off:
- Durability: event log mất khi restart (P3-a sẽ add file persistence)
- Scale: single-process limits throughput (fine cho mesh node-to-node events)
- Cluster: nếu cần multi-gateway event sync, P4 item (out of scope v6.0)

Trong use case AI dev team, events thường là:
- Low frequency (1 event per phase transition, ~10-20 events per project)
- Short-lived (project runs for minutes, not days)
- Not mission-critical (losing events on restart is acceptable — project restarts)

In-process là appropriate.

---

### 3.4 Fan-out delivery design

Khi event được emit và có 3 subscribers, làm thế nào để deliver?

**Option A: Synchronous sequential**
```python
for sub in matching_subs:
    await deliver(event, sub)   # block until delivered
```
Nhược: slow, delivery failure blocks subsequent deliveries.

**Option B: Concurrent gather**
```python
await asyncio.gather(*[deliver(event, sub) for sub in matching_subs])
```
Nhược: tất cả deliveries start cùng lúc. Nếu 10 subscribers → 10 concurrent HTTP calls.
Có thể overwhelm worker nodes.

**Option C: Background queue worker (chosen)**
```python
# emit() puts (event, sub) pairs into asyncio.Queue
await self._delivery_queue.put((event, sub))
# Background task drains queue, controls concurrency
```

**Quyết định: Option C.**

Lý do:
- `emit()` returns immediately — emitter không bị block
- Background worker có thể control concurrency (e.g., max 5 concurrent deliveries)
- Retry logic naturally fits into worker loop
- Failed deliveries don't affect other deliveries

**Retry policy:**
```
Attempt 1: immediate
Attempt 2: +2s delay
Attempt 3: +4s delay
Fail: log to dead-letter list (P3-e: explicit dead-letter queue)
```
3 attempts với exponential backoff là sufficient cho transient network issues.

---

### 3.5 Pattern matching syntax

Subscription `event_type_pattern` cần match các trường hợp:

```
"test.failed"      → exact match
"test.*"           → prefix: match test.failed, test.passed, test.timeout
"*.failed"         → suffix: match test.failed, build.failed, deploy.failed
"*"                → match everything
"artifact.*.*"     → multi-level: match artifact.code.written, artifact.doc.updated
```

**Xem xét:** Dùng full glob pattern (`fnmatch`) hay custom implementation?

`fnmatch.fnmatch("test.failed", "test.*")` → works correctly.
`fnmatch.fnmatch("test.failed", "*.failed")` → works correctly.
`fnmatch.fnmatch("artifact.code.written", "artifact.*.*")` → works correctly.

**Quyết định: dùng `fnmatch.fnmatch`** từ Python stdlib.

Zero additional deps, well-tested, familiar syntax. Đủ expressive cho use cases hiện tại.

---

### 3.6 Human checkpoint mechanism (P3-d)

**Vấn đề cốt lõi:** Coroutine không thể "pause" và wait for external input trong
Python asyncio mà không blocking event loop.

**Options:**

**Option A: Polling — check for answer file**
```python
# Emitter writes question to checkpoint file
# Then polls until answer file appears
while not os.path.exists(answer_path):
    await asyncio.sleep(5)
answer = read(answer_path)
```
Nhược: crude, không efficient, không có timeout mechanism.

**Option B: asyncio.Event**
```python
event = asyncio.Event()
checkpoints[checkpoint_id] = event
await asyncio.wait_for(event.wait(), timeout=3600)
answer = checkpoint_answers[checkpoint_id]
```
Nhược: in-memory only, không survive restart. Answer mất khi process restart.

**Option C: Coroutine parking với persistent state (chosen for P3-d)**
```python
# Checkpoint creates a record in CheckpointManager
# Workflow suspends by raising CheckpointPause exception
# IntentHandler catches, saves session state, returns partial response
# POST /checkpoints/{id}/respond → resumes session with injected answer
```

**Quyết định: Option C** nhưng defer to P3-d.

P0/P1/P2 sẽ dùng simpler workaround: `human.input_required` event →
human interface (Claude Web hoặc Telegram bot) nhận → hỏi user → emit `human.answered`.
Workflow không "pause" thực sự — nó emit event và return. PM node watch `human.*`
events và relay. Không elegant nhưng functional cho MVP.

Option C là correct solution nhưng requires changes to IntentHandler state management.
Scope P3-d cho version kế tiếp.

---

## 4. Gap analysis cuối cùng: "AI dev team giống real team?"

Với v6.0 (full P0→P3), mức độ giống real team:

| Behavior | Pipeline cũ | v6.0 P1 | v6.0 P3 |
|----------|-------------|---------|---------|
| Agents tự phối hợp không qua PM | ❌ | ✅ | ✅ |
| PM chỉ watch, không coordinate | ❌ | ✅ | ✅ |
| Agent "raise hand" khi stuck | ❌ | ✅ | ✅ |
| Human checkpoint có cấu trúc | ❌ | Partial | ✅ |
| Parallel work (analyst + docs cùng lúc) | ❌ | ✅ | ✅ |
| Daily standup tự động | ❌ | ❌ | ✅ (P2 cron) |
| Analyst tự start khi thấy project mới | ❌ | ❌ | ✅ (P2 condition) |
| Full audit trail | ❌ | ✅ | ✅ |
| Session survive restart | ❌ | ❌ | ✅ (P3-b) |
| Priority jobs (critical bug preempt) | ❌ | ❌ | ✅ (P3-c) |
| Scoped team communication | ❌ | ✅ | ✅ |

**Những gì vẫn không replicate được (20% còn lại):**

1. **Implicit knowledge về teammates** — Engineer thực sự biết "Alice code nhanh
   nhưng hay quên error handling, Bob prefer verbose naming". Agent không có persistent
   cross-project memory về peer agents. Cần P4: agent-to-agent reputation/observation store.

2. **Emergent social dynamics** — Real teams có informal communication, trust building,
   psychological safety. Agents chỉ communicate qua structured events/actions.

3. **Ambiguity tolerance** — Real engineer thấy requirement mơ hồ sẽ dùng judgment
   dựa trên kinh nghiệm ngành để tự điền blank. Agents phải emit `clarification.needed`
   cho mọi ambiguity — không có domain intuition.

4. **True context switch với state preservation** — Engineer interrupt feature A để fix
   bug B, rồi return to A với full context. Agent mỗi `/intent` call là stateless —
   không có "resume" semantic thực sự cho complex multi-task state.

**Kết luận:** ~80% là achievable với v6.0. 20% còn lại là fundamental limitations
của LLM-based agents, không phải GNOT infrastructure limitations.

---

## 5. Quyết định về version naming

Version v6.0 thay vì v5.14 hay v5.x vì:

1. **Paradigm shift** — v5.x là request/response. v6.0 là event-driven.
   Đây không phải incremental feature addition — đây là architectural evolution.

2. **3 new modules** — ChannelRegistry, EventBus, Scheduler. Không phải tweaks.

3. **New HTTP API surface** — 9 new endpoints. Breaking thêm concept boundary.

4. **New config namespace** — `channels:`, `event_bus:`, `scheduler:`, `schedule:`.

Tuy nhiên, **backward compat hoàn toàn** — mọi v5.13b config vẫn hoạt động không đổi.
v6.0 là additive, không destructive.

---

## 6. Implementation notes cho developer

### Module initialization order trong `server.py`

```python
# Correct initialization order:
# 1. NodeConfig (loaded from yaml)
# 2. ActionRegistry (load action plugins)
# 3. JobManager, JobQueue (existing)
# 4. NodeRegistry (existing)
# 5. ChannelRegistry (v6.0 NEW) — needs node_id + config.channels
# 6. EventBus (v6.0 NEW) — needs ChannelRegistry + GatewayRouter
# 7. Scheduler (v6.0 NEW) — needs ActionExecutor + EventBus
# 8. GatewayRouter (existing) — needs NodeRegistry, JobQueue
# 9. IntentHandler (existing)

# Note: EventBus needs GatewayRouter for delivery, but GatewayRouter
# is initialized after EventBus. Resolve via lazy injection:
# event_bus._router = gateway_router  (set after both are created)
```

### Thread safety notes

`EventBus._subscriptions` được access từ:
- `subscribe()` / `unsubscribe()` — user-initiated
- `_match_subscription()` — called from `emit()`, potentially concurrent
- `_delivery_worker()` — reads sub config during delivery

Dùng `asyncio.Lock` như pattern của NodeRegistry và ConversationStore.
Không dùng threading.Lock vì entire runtime là single-threaded asyncio.

### Testing strategy

Mỗi P tier cần test suite riêng:
- `test_v60_channels.py` — ChannelRegistry membership, join/leave, broadcast scope
- `test_v60_eventbus.py` — emit, subscribe, pattern matching, delivery, retry
- `test_v60_scheduler.py` — condition trigger, cron tick, event trigger, concurrent control
- Integration test: full AI dev team flow với mock agents via ASGI test client

**Pattern matching edge cases to test:**
```python
assert match("test.failed", "test.*") == True
assert match("test.failed", "*.failed") == True
assert match("test.failed", "*") == True
assert match("test.failed", "build.*") == False
assert match("test.failed", "test.failed") == True
assert match("test.failed", "test.fail") == False  # not prefix, exact only
assert match("artifact.code.written", "artifact.*.*") == True
```

### Cron expression parser

Không dùng `croniter` (external dep). Implement minimal cron parser:
- Support standard 5-field format: `minute hour dom month dow`
- Support `*`, `/N` (step), `,` (list), `-` (range)
- Compute `next_tick(expression, from_time) → float`
- ~100 LOC, no external deps
- Test với common expressions: `"0 9 * * 1-5"`, `"*/15 * * * *"`, `"0 0 1 * *"`

---

## 7. Open questions (để decide khi implement)

**Q1: Event delivery — push or pull?**
Current design: EventBus deliver via `POST /action` to subscriber node (push).
Alternative: subscriber node polls `GET /events?since=<ts>&type=<pattern>` (pull).

Push pros: reactive, lower latency, consistent với WorkerAgent execution model.
Push cons: subscriber node phải be online khi event emitted.

Pull pros: works for offline/NAT nodes naturally.
Pull cons: adds polling latency, subscriber phải manage cursor.

**Recommendation:** Push as default, với fallback: nếu subscriber unreachable
và delivery fails after 3 retries → event stays in log, subscriber can replay
via `GET /events` when it comes back online. Hybrid approach.

**Q2: Scheduler — per-node hoặc centralized?**
Current design: Scheduler runs on the node that owns the schedule entries.
Alternative: Gateway has a central Scheduler, schedules actions on behalf of worker nodes.

Per-node pros: autonomous, no single point of failure.
Per-node cons: Scheduler state distributed, harder to inspect across mesh.

Centralized pros: single `/schedule` endpoint to see all schedules.
Centralized cons: gateway bottleneck, workers behind NAT might not be reachable.

**Recommendation:** Per-node Scheduler (consistent với autonomy philosophy).
Add `GET /mesh/schedules` endpoint on gateway that aggregates from all nodes.

**Q3: Channel creation — explicit hoặc auto-create?**
Option A: Channel phải được explicitly created (POST /channels) trước khi join.
Option B: Channel auto-created khi first node joins.

**Recommendation:** Auto-create on first join. Channels là lightweight (just a name
+ membership list). Requiring explicit creation adds friction without benefit.

**Q4: Event type namespace — enforce convention hay free-form?**
Current design: event_type là free-form string, convention là `domain.verb`.
Alternative: enforce namespace registration (`POST /event-types` để register schema).

**Recommendation:** Free-form for v6.0. Schema registry là P4 item.
Document convention strongly in specs and examples.

---

## 8. Liên quan đến các guides hiện có

Sau khi implement v6.0, các guides sau cần được update:

| Guide | Update cần thiết |
|-------|-----------------|
| Guide 23 — AI Dev Team | Major rewrite: event-based architecture, subscriptions, channels |
| Guide 09 — Multi-node execution | Thêm section về channel-scoped execution |
| Guide 13 — Auto-scale | Thêm: bootstrap với channel membership |
| Guide 14 — Full Mesh | Thêm: event bus across mesh, cross-team isolation |

Nên tạo thêm:
- **Guide 24 — Event-Driven AI Dev Team v2** (rewrite của Guide 23 với v6.0)
- **Guide 25 — Building Autonomous Agents** (level 1/2/3 autonomy walkthrough)

---

*Worklog: WORKLOG_V6.0.md | Mesh Runtime v6.0 | Repository: ai-infra-runtime-v2*
