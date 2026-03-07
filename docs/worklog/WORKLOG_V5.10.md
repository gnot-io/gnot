# Worklog — Execution Mesh v5.10 (BGP-style Route Advertisement)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-05
**Base version:** v5.9 → v5.10

---

## 1. Vấn đề / Context

### Yêu cầu từ user

> Giả sử hệ thống có node-0 (public), node-1 (internal, register với node-0), node-1a
> (internal, register với node-1, có action đặc biệt `get_order_info`). Claude Web có thể
> giao tiếp với node-0. User hỏi "Tôi muốn biết thông tin đơn hàng #123" — hệ thống phải
> biết node-1a có action đó và route request đến đúng nơi.

### Gap được xác định từ review v5.9

Chạy thử với payload `POST /action {target_node_id: "node-1a"}`:

```json
{"error": "UNTRUSTED_NODE: node-1a is not in the trusted-node list", "node_id": "node-0"}
```

**Root cause 1 — Routing:** `GatewayRouter.route()` check `is_trusted(target)` ngay sau khi
check local. node-1a không trong trusted list của node-0 nên fail ngay. Không có cơ chế
để node-0 biết "node-1a reachable qua node-1".

**Root cause 2 — Discovery:** `GET /skills` trả Markdown text của local node. Không có
endpoint trả structured JSON với capability tree toàn bộ mesh. Claude/IntentHandler không
thể biết `get_order_info` tồn tại trên node-1a.

**Root cause 3 — Registration:** `NodeRegistrationRequest` chỉ có `{node_id, address}`.
Khi node-1 đăng ký với node-0, không có cách nào để node-1 thông báo "tôi có sub-node
node-1a với action get_order_info".

---

## 2. Phân tích Alternative Approaches

Trước khi implement, đã phân tích 5 hướng tham chiếu từ thiết kế routing mạng Internet:

### Option A — Source Routing (explicit path trong request)

Caller encode full path: `route: ["node-0", "node-1", "node-1a"]`. Mỗi hop đọc element
kế tiếp.

**Tham chiếu:** MPLS Label Stack, IPv6 Segment Routing.

Ưu điểm: routing logic đơn giản, không cần state.

Nhược điểm: caller phải biết full topology trước — đây chính là vấn đề discovery chưa giải
quyết. Nếu topology thay đổi (node restart, re-route), caller có stale path. Coupling cao.

**Verdict: viable nhưng fragile. Chọn làm fallback, không phải primary.**

### Option B — Distance Vector / RIP-style

Mỗi node duy trì routing table, broadcast định kỳ cho neighbors. Node học và propagate.

**Tham chiếu:** RIP (Routing Information Protocol).

Ưu điểm: self-healing, caller không cần biết path.

Nhược điểm: count-to-infinity problem nổi tiếng của RIP. Broadcast định kỳ = overhead liên
tục. Convergence chậm. Quá phức tạp cho tree topology.

**Verdict: không phù hợp.**

### Option C — Link-State / OSPF-style

Mỗi node flood adjacency info ra toàn mạng. Mỗi node dựng full graph, chạy Dijkstra.

**Tham chiếu:** OSPF (Open Shortest Path First).

Ưu điểm: optimal path, full visibility.

Nhược điểm: flood storm với mesh lớn. Mỗi node cần maintain full graph. Overkill cho tree
topology.

**Verdict: không phù hợp với scale và topology.**

### Option D — DNS-style Hierarchical Delegation

Khi resolver không biết `node-1a`, hỏi authoritative server cho zone "node-1.*".

**Tham chiếu:** DNS recursive resolution.

Ưu điểm: hierarchical, scalable, tự nhiên với tree topology.

Nhược điểm: yêu cầu naming convention nhất quán (phải biết "node-1a thuộc zone node-1").
Nếu names tùy ý thì không hoạt động. Coupling giữa name và topology là anti-pattern.

**Verdict: tốt nếu enforce naming convention, nhưng quá brittle.**

### Option E — BGP-style Route Advertisement (được chọn)

Khi một node kết nối với gateway, nó **advertise** danh sách destinations nó có thể reach.
Gateway cập nhật routing table với next-hop = node đó.

**Tham chiếu:** BGP (Border Gateway Protocol) — inter-domain routing của Internet.

Mapping:
- AS → Node
- IP prefix → Node ID
- BGP neighbor → Gateway
- Route advertisement → `advertise_routes` trong `/nodes/register`
- Next-hop router → Next-hop node
- Routing table → `NodeRegistry._entries` với `next_hop` field
- BGP UPDATE → Re-registration khi sub-node join

Ưu điểm:
- Caller không cần biết path — chỉ cần biết target node ID
- Self-healing: khi topology thay đổi, node re-advertise
- Consistent với cách Internet thực sự hoạt động
- Capability discovery O(1): `GET /capabilities` trả full tree
- Backward compatible: không dùng `advertise_routes` → hoạt động như v5.9

Nhược điểm:
- Registration payload lớn hơn (acceptable — chỉ xảy ra lúc startup)
- Route withdrawal chưa implement (known limitation, xem SPECS)
- Node phải biết sub-nodes của nó (hoặc re-advertise khi sub-node join — đây là mechanism chính)

**Verdict: phù hợp nhất. Được chọn.**

---

## 3. Implementation Details

### 3.1 models.py — NodeRegistrationRequest extended

Thêm 3 fields optional (backward compatible):

```python
actions: list[str] = []             # actions node này cung cấp
advertise_routes: list[str] = []    # sub-node IDs reachable qua node này
capabilities: dict[str, list[str]] = {}  # {sub_node_id: [actions]}
```

Thêm 2 models mới cho capability tree response:

```python
class CapabilityNode(BaseModel):
    node_id: str
    actions: list[str]
    next_hop: str | None = None   # None = direct, str = route via
    reachable: dict[str, "CapabilityNode"] = {}

class CapabilityTreeResponse(BaseModel):
    node_id: str
    actions: list[str]
    reachable: dict[str, CapabilityNode]
```

`CapabilityNode.model_rebuild()` để resolve forward reference.

### 3.2 node_registry.py — _NodeEntry + register() + new methods

**_NodeEntry** thêm 2 fields:
```python
actions: list = field(default_factory=list)
next_hop: str | None = None   # None = direct child của node này
```

`field` import thêm vào `from dataclasses import dataclass, field`.

**register()** — signature mở rộng:
```python
async def register(
    self,
    node_id: str,
    address: str | None = None,
    actions: list[str] | None = None,
    advertise_routes: list[str] | None = None,
    capabilities: dict[str, list[str]] | None = None,
) -> None:
```

Logic mới:
1. Cài entry cho `node_id` với `actions` (như cũ + actions mới)
2. Với mỗi `sub_id` trong `advertise_routes`:
   - Nếu entry đã tồn tại: update `actions`, keep `next_hop` nếu đã có
   - Nếu chưa tồn tại: tạo entry với `next_hop=node_id`

**Quyết định thiết kế:** "First writer wins" cho `next_hop`. Nếu node-1a sau đó tự đăng ký
trực tiếp với node-0, `next_hop` không bị overwrite vì entry đã tồn tại. Actions được update.
Điều này đảm bảo route stability — không bị flip khi có multiple registration paths.

**Methods mới:**

`get_next_hop(target_node_id)` — sync, không cần lock:
- `None` nếu target không biết
- `target_node_id` nếu là direct child (`next_hop=None` trong entry)
- `entry.next_hop` nếu là indirect

`get_actions(node_id)` — sync, trả `[]` nếu không biết.

`build_capability_tree(own_actions)` — sync, scan toàn bộ `_entries`, build flat dict
`{node_id: CapabilityNode}` với `next_hop` annotation.

### 3.3 gateway_router.py — Next-hop routing

**route()** — thêm step 4 (BGP next-hop check) giữa step "local" và step "trusted check":

```python
next_hop = self._node_registry.get_next_hop(target)
if next_hop is not None and next_hop != target:
    # target là indirect node — forward qua next_hop
    forwarded = request.model_copy(deep=True)
    forwarded.trace.hop_count += 1
    forwarded.trace.route_path.append(self._config.node_id)
    return await self._forward_via_next_hop(forwarded, next_hop)
```

**Quan trọng:** `target_node_id` trong `forwarded` **không thay đổi**. Request vẫn
chứa target là `"node-1a"`. Khi request đến node-1, node-1 sẽ lặp lại routing logic
— thấy `"node-1a"` là direct child của nó → route normally.

**_forward_via_next_hop(request, next_hop_node_id)** — new method:

```
1. Lookup next_hop address (registry + resolver fallback)
2. Nếu có address và reachable → _push(request, hop_address)
3. Fallback (NAT/không reachable) → _pull() với special encoding:
   - pull_req.target_node_id = next_hop_node_id  ← enqueue cho next_hop
   - pull_req.payload.params["_mesh_forward_target"] = original target
   - pull_req.payload.action = "_mesh_forward"
```

**Lý do encoding vào params thay vì field riêng:** `QueuedJob` model không có field cho
forwarding metadata. Thay vì thêm field mới (breaking change cho QueuedJob), encode vào
params là least-intrusive. WorkerAgent detect `action == "_mesh_forward"` và extract.

### 3.4 worker_agent.py — Advertisement + Forward handling

**Constructor** — thêm `action_registry: dict | None = None`:
- `self._action_registry = action_registry or {}`
- `self._sub_routes: dict[str, list[str]] = {}` — sub-nodes đã đăng ký
- `self._sub_routes_lock = asyncio.Lock()` — thread safety
- `self._self_url` — để _handle_mesh_forward biết địa chỉ /action của chính nó

**_register()** — build advertisement payload:
```python
payload["actions"] = list(self._action_registry.keys())
if sub_routes_snapshot:
    payload["advertise_routes"] = list(sub_routes_snapshot.keys())
    payload["capabilities"] = dict(sub_routes_snapshot)
```

**add_sub_route(sub_node_id, sub_actions)** — được gọi bởi server khi sub-node đăng ký:
1. Lock → update `self._sub_routes[sub_node_id] = sub_actions`
2. Gọi `self._register()` → re-advertise lên gateway cấp trên

**_handle_mesh_forward(job)** — POST về local /action endpoint:
```python
original_target = job.params["_mesh_forward_target"]
POST self._self_url/action {
    target_node_id: original_target,
    action: job.params["action"],
    params: {k:v for k,v in job.params if k not in special_keys}
}
```
Node's own GatewayRouter xử lý tiếp — có thể execute local hoặc forward tiếp nếu còn
nhiều lớp.

**_claim_and_execute()** — thêm dispatch:
```python
if job.action == "_mesh_forward":
    response = await self._handle_mesh_forward(job)
else:
    response = await self._executor.execute(...)
```

### 3.5 server.py — /nodes/register + /capabilities + worker_agent_ref

**/nodes/register endpoint** — update:
```python
await node_registry.register(
    node_id=req.node_id,
    address=req.address,
    actions=req.actions,
    advertise_routes=req.advertise_routes,
    capabilities=req.capabilities,
)

# Propagate upward nếu node này cũng là worker
agent = worker_agent_ref.get("agent")
if agent is not None and (req.actions or req.advertise_routes):
    for advertised_id, advertised_actions in all_reachable.items():
        await agent.add_sub_route(advertised_id, advertised_actions)
```

**worker_agent_ref** — mutable dict `{"agent": None}`:
- Tạo sau `app = FastAPI(...)`, gán vào `app.state.worker_agent_ref`
- node_runtime.attach_worker_agent() inject agent vào dict này sau startup
- Các endpoint closure capture dict bằng reference → agent injection tự động hoạt động

**/capabilities endpoint** — mới:
```python
own_actions = list(registry.keys())
reachable = node_registry.build_capability_tree(own_actions)
return CapabilityTreeResponse(node_id=config.node_id, actions=own_actions, reachable=reachable)
```

### 3.6 node_runtime.py — Pass action_registry to WorkerAgent

```python
agent = WorkerAgent(
    config=config,
    executor=worker_executor,
    action_registry=dict(registry),
)
```

Và inject agent sau startup:
```python
if hasattr(app.state, "worker_agent_ref"):
    app.state.worker_agent_ref["agent"] = agent
```

### 3.7 intent_handler.py — Richer system prompt

`_build_system_prompt()` thay toàn bộ logic cũ (dùng `get_all_statuses()`) bằng
`build_capability_tree()`:

```python
own_actions = list(self._action_registry.keys())
reachable = self._node_registry.build_capability_tree(own_actions)

for node_id, cap in sorted(reachable.items()):
    via = f" via {cap.next_hop}" if cap.next_hop else " (direct)"
    lines.append(f"  - {node_id}{via}")
    if cap.actions:
        lines.append(f"    actions: {', '.join(cap.actions)}")
```

Routing rules section update: "Target ANY node — routing is automatic, even through multiple
NAT layers."

---

## 4. Files thay đổi

| File | Loại | Thay đổi chính |
|------|------|----------------|
| `runtime/models.py` | Modified | `NodeRegistrationRequest` + 3 fields; `CapabilityNode`, `CapabilityTreeResponse` |
| `runtime/node_registry.py` | Modified | `_NodeEntry` + `actions`/`next_hop`; `register()` extended; `get_next_hop()`, `get_actions()`, `build_capability_tree()` |
| `runtime/gateway_router.py` | Modified | `route()` + step 4 next-hop; `_forward_via_next_hop()` |
| `runtime/worker_agent.py` | Modified | constructor + `action_registry`/`_sub_routes`; `_register()` sends advertisement; `add_sub_route()`; `_handle_mesh_forward()` |
| `runtime/server.py` | Modified | `/nodes/register` xử lý advertisement + upward propagation; `GET /capabilities` mới; `worker_agent_ref` injection |
| `mesh/node_runtime.py` | Modified | Pass `action_registry` to `WorkerAgent`; inject `worker_agent_ref` sau startup |
| `runtime/intent_handler.py` | Modified | `_build_system_prompt()` dùng `build_capability_tree()` |
| `tests/test_v510_features.py` | New | 20 tests mới |
| `docs/SPECS_V5.10.md` | New | Document này |
| `docs/WORKLOG_V5.10.md` | New | Document này |

---

## 5. Test Suite

| Class | Tests | Covers |
|-------|-------|--------|
| `TestNodeRegistryV510` | 7 | register() stores actions; sub-routes với next_hop; get_next_hop(); build_capability_tree(); first-writer-wins |
| `TestGatewayRouterV510` | 3 | direct child routing; indirect node via next-hop; unknown node error |
| `TestWorkerAgentV510` | 3 | registration payload gồm actions; add_sub_route() updates + re-registers; sub_routes trong payload |
| `TestCapabilityEndpoints` | 4 | POST /nodes/register stores actions; sub-routes propagated; GET /capabilities self; empty reachable |
| `TestIntentHandlerSystemPromptV510` | 3 | node IDs trong prompt; next_hop annotation; direct node hiển thị "(direct)" |
| **Tổng mới** | **20** | |

**v5.9: 92 tests → v5.10: 112 tests (+20) — 112/112 pass**

---

## 6. Decisions log

### D1: Tại sao flat capability tree thay vì nested?

`build_capability_tree()` trả flat dict thay vì nest node-1a bên trong entry của node-1.

**Lý do:**
1. Caller (Claude, IntentHandler) cần lookup by node_id — flat dict = O(1) lookup, nested = O(depth) traversal
2. Với topology sâu (5+ lớp), nested JSON cồng kềnh và khó parse
3. `next_hop` annotation đủ để caller hiểu routing path nếu cần

Compromise: `reachable: {}` field trong `CapabilityNode` được giữ cho future use khi cần
fully nested tree (P4 item).

### D2: Tại sao "first writer wins" cho next_hop?

Khi node-1a đăng ký qua node-1 (advertised) rồi sau đó tự đăng ký trực tiếp với node-0,
next_hop không bị override.

**Lý do:** Route stability. Nếu node-1a có thể reach node-0 trực tiếp, nó sẽ register
với `address` — GatewayRouter sẽ push trực tiếp qua address đó, bỏ qua next_hop lookup.
Nếu không có address, giữ indirect route (via node-1) là đúng hơn.

**Alternative bị reject:** "Last writer wins" — dễ gây route flapping nếu node-1a
re-registers nhiều lần.

### D3: Tại sao encode _mesh_forward vào params thay vì model field?

`QueuedJob` model hiện tại không có field cho forwarding metadata. Options:
1. Thêm `forward_target: str | None` vào QueuedJob — clean nhưng breaking change cho
   existing job serialization
2. Encode vào `params` với prefix `_mesh_` — non-breaking, isolated

Chọn Option 2 vì backward compat quan trọng hơn cleanliness ở field level. Convention
`_mesh_*` prefix dễ filter khi extract original params.

### D4: worker_agent_ref injection pattern

Server `create_app()` return FastAPI app. WorkerAgent được create trong `node_runtime.py`
sau đó — không có access trực tiếp vào agent từ server closure.

Options:
1. Pass agent vào `create_app()` — chicken-and-egg: agent cần executor cần registry cần app
2. Mutable dict được inject sau — same pattern với closure, agent available sau startup event
3. `app.state` — FastAPI built-in mechanism cho app-level state

Chọn kết hợp 2+3: `worker_agent_ref` dict được tạo trong `create_app()`, gán vào
`app.state.worker_agent_ref`, và node_runtime inject agent vào dict sau startup.
Endpoint closures capture dict bằng reference → automatically thấy agent sau injection.

### D5: Re-advertisement scope khi sub-node join

Khi node-1a đăng ký với node-1, node-1 re-advertise **mọi sub-route** (không chỉ node-1a)
lên node-0. Cụ thể: `add_sub_route()` cập nhật `_sub_routes` rồi gọi `_register()` với
toàn bộ snapshot.

**Lý do:** Đảm bảo node-0 có full picture. Nếu node-0 restart và mất state, node-1 sẽ
re-advertise đầy đủ khi re-register. Overhead nhỏ — xảy ra tại thời điểm topology thay đổi,
không phải hot path.

---

## 7. Remaining Items (cập nhật)

| Priority | Item | Status | Ghi chú |
|----------|------|--------|---------|
| P2 | Lazy node staleness check | ✅ Done (v5.7) | |
| P3 | Pull job timeout | ✅ Done (v5.7) | |
| P3 | FastAPI lifespan migration | ✅ Done (v5.7) | |
| P3 | Queue depth in /health | ✅ Done (v5.7) | |
| P3 | File upload/download | ✅ Done (v5.8) | |
| P3 | Intent / agent loop | ✅ Done (v5.9) | |
| P3 | **BGP route advertisement** | ✅ Done (v5.10) | |
| P3 | **Multi-hop routing** | ✅ Done (v5.10) | |
| P3 | **GET /capabilities** | ✅ Done (v5.10) | |
| P2 | Container isolation | ⏸ Deferred | non-root + ulimit |
| P3 | Job queue persistence | ⏸ Deferred | SQLite backend |
| P3 | Session persistence | ⏸ Deferred | Conversations survive restart |
| P3 | Streaming intent response | ⏸ Deferred | SSE stream từng step |
| P3 | **Route withdrawal** | 🆕 v5.11 | Xóa routes khi node disconnect/timeout |
| P3 | **_mesh_forward result chain** | 🆕 v5.11 | Propagate real result qua pull chain |
| P3 | Upload chunked/resumable | ⏸ Deferred | |
| P3 | File ACL | ⏸ Deferred | |
| P4 | Nested capability tree | ⏸ Deferred | `reachable` field trong CapabilityNode |
| P4 | Telegram bot reference impl | ⏸ Deferred | |
