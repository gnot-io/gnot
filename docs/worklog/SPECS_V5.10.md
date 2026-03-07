# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v5.10
### (BGP-style Route Advertisement & Multi-Hop Capability Discovery)

---

## 1. Vấn đề giải quyết trong v5.10

### 1.1 Gap được xác định từ v5.9

Trước v5.10, hệ thống chỉ hỗ trợ routing đến **direct children** của một gateway. Với topology
nhiều lớp NAT như sau:

```
Internet
  Claude Web / Telegram Bot
        │
      node-0   (gateway, public)
        │
      node-1   (private, direct child của node-0)
        │
      node-1a  (private, direct child của node-1)
                 └── action: get_order_info
```

Khi Claude Web gọi `POST /action {target_node_id: "node-1a"}` vào node-0, hệ thống trả về:

```json
{"error": "UNTRUSTED_NODE: node-1a is not in the trusted-node list"}
```

**Vì sao?** NodeRegistry của node-0 chỉ chứa node-1 (đã đăng ký trực tiếp). node-1a không
đăng ký với node-0, nên node-0 không biết node-1a tồn tại, không biết nó reachable qua
node-1, và không biết nó có action `get_order_info`.

Hệ quả: Claude Web phải thực hiện nhiều bước thủ công — query từng node, tự suy luận
path, tự orchestrate multi-hop. Điều này vi phạm mục tiêu thiết kế.

### 1.2 Ba gap cụ thể

| Gap | Mô tả | Hệ quả |
|-----|-------|--------|
| **G1** | Routing không qua được node trung gian | UNTRUSTED_NODE error cho mọi sub-node |
| **G2** | Capability discovery chỉ thấy local node | Claude không biết `get_order_info` tồn tại |
| **G3** | Registration không carry actions và sub-nodes | NodeRegistry chỉ lưu `node_id + address + status` |

---

## 2. Giải pháp: BGP-style Route Advertisement

### 2.1 Tham chiếu từ Internet routing

Internet giải quyết bài toán tương tự với **BGP (Border Gateway Protocol)**:

- Mỗi **Autonomous System (AS)** khi kết nối với neighbor, nó **quảng bá (advertise)** danh
  sách các prefixes mà nó có thể reach.
- Neighbor học route và propagate tiếp lên peer của nó.
- Khi router cần forward packet, nó chỉ cần biết **next-hop** cho destination — không cần
  biết full path.

Map sang Execution Mesh:

| Internet | Execution Mesh |
|----------|---------------|
| Autonomous System | Node |
| IP prefix | Node ID |
| BGP neighbor | Gateway node |
| Route advertisement | `advertise_routes` trong `/nodes/register` |
| Next-hop router | Next-hop node |
| Routing table | `NodeRegistry._entries` với `next_hop` field |
| BGP UPDATE message | Re-registration khi sub-node join |

### 2.2 Luồng advertisement v5.10

```
1. node-1a khởi động
   → WorkerAgent đọc local action_registry (chứa "get_order_info")
   → POST /nodes/register lên node-1:
     {
       "node_id": "node-1a",
       "actions": ["execute_command", "get_order_info"],
       "advertise_routes": [],
       "capabilities": {}
     }

2. node-1 nhận request
   → NodeRegistry cài entry: node-1a (actions=[...], next_hop=None)  ← direct child
   → worker_agent_ref["agent"].add_sub_route("node-1a", ["execute_command","get_order_info"])
     → cập nhật self._sub_routes
     → RE-ADVERTISE lên node-0:
       POST /nodes/register {
         "node_id": "node-1",
         "actions": ["execute_command"],
         "advertise_routes": ["node-1a"],
         "capabilities": {"node-1a": ["execute_command", "get_order_info"]}
       }

3. node-0 nhận re-registration từ node-1
   → NodeRegistry cài thêm entry: node-1a (actions=[...], next_hop="node-1")
   → GET /capabilities trên node-0 bây giờ thấy:
     {
       "node_id": "node-0",
       "actions": ["execute_command", "read_file", "write_file"],
       "reachable": {
         "node-1":  {"actions": ["execute_command"], "next_hop": null},
         "node-1a": {"actions": ["execute_command", "get_order_info"], "next_hop": "node-1"}
       }
     }
```

### 2.3 Propagation với N lớp

Ví dụ 3 lớp: node-0 → node-1 → node-1a → node-1a-sub:

```
node-1a-sub đăng ký với node-1a
  → node-1a re-advertise lên node-1:
    advertise_routes: ["node-1a-sub"]

node-1 nhận → cài node-1a-sub với next_hop="node-1a"
node-1 re-advertise lên node-0:
  advertise_routes: ["node-1a", "node-1a-sub"]
  capabilities: {
    "node-1a":     ["execute_command", "get_order_info"],
    "node-1a-sub": ["special_action"]
  }

node-0 cài:
  node-1a:     next_hop="node-1"
  node-1a-sub: next_hop="node-1"   ← node-0 chỉ biết next-hop là node-1,
                                      không cần biết node-1a là intermediate
```

**Quan trọng:** Mỗi node chỉ cần biết **next-hop trực tiếp của nó**, không cần biết full
path. Đây chính xác là cách BGP hoạt động.

---

## 3. Kiến trúc tổng quan v5.10

```
┌────────────────────────────────────────────────────────────────────────┐
│                     Internet / Cloudflare Tunnel                        │
└─────────────────────────────┬──────────────────────────────────────────┘
                              │
         ┌────────────────────▼──────────────────────┐
         │              node-0  (gateway, public)      │
         │                                             │
         │  NodeRegistry:                              │
         │    node-1   → addr=None, next_hop=None      │
         │    node-2   → addr=None, next_hop=None      │
         │    node-1a  → addr=None, next_hop="node-1"  │
         │    node-2a  → addr=None, next_hop="node-2"  │
         │                                             │
         │  GET /capabilities   ←── Claude Web         │
         │  POST /action        ←── Claude Web         │
         │  POST /intent        ←── Telegram Bot        │
         └──────┬──────────────────────┬───────────────┘
                │ pull queue           │ pull queue
                ▼                      ▼
  ┌─────────────────────┐   ┌─────────────────────┐
  │  node-1  (private)  │   │  node-2  (private)  │
  │                     │   │                     │
  │  NodeRegistry:      │   │  NodeRegistry:      │
  │   node-1a → direct  │   │   node-2a → direct  │
  │                     │   │                     │
  └──────────┬──────────┘   └──────────┬──────────┘
             │ pull queue               │ pull queue
             ▼                          ▼
  ┌─────────────────────┐   ┌─────────────────────┐
  │  node-1a (private)  │   │  node-2a (private)  │
  │  actions:           │   │  actions:           │
  │   get_order_info    │   │   send_notification │
  └─────────────────────┘   └─────────────────────┘
```

---

## 4. Routing Decision Tree (v5.10)

`GatewayRouter.route()` bây giờ có 5 bước thay vì 4:

```
route(request):
  1. Hop guard: hop_count > max_hop → ERROR (loop protection)
  2. Loop guard: node_id in route_path → ERROR
  3. Local: target == self → execute_local()
  4. [NEW v5.10] Next-hop: get_next_hop(target) != target
       → target là sub-node được advertise
       → _forward_via_next_hop(request, next_hop)
          ├─ next_hop có address + reachable → _push(request, hop_address)
          │    request.target_node_id KHÔNG đổi
          │    next_hop sẽ lặp lại routing logic cho target
          └─ next_hop không reachable → _pull() với _mesh_forward action
               next_hop poll → _handle_mesh_forward → POST /action local
  5. Trusted check: is_trusted(target)? → push/pull như cũ
     Not trusted → UNTRUSTED_NODE error
```

### 4.1 Push-mode multi-hop (next_hop reachable)

```
node-0.route(target="node-1a"):
  → get_next_hop("node-1a") = "node-1"
  → node-1 address? không có (NAT) → ping → unreachable
  → _pull(): enqueue job {action="_mesh_forward",
                          params={_mesh_forward_target="node-1a",
                                  action="get_order_info", ...}}
             target = "node-1"  ← đây là key: enqueue cho node-1 không phải node-1a

node-1 polls → nhận _mesh_forward job
  → _handle_mesh_forward()
  → POST self._self_url/action {target_node_id: "node-1a", action: "get_order_info"}
  → node-1's own GatewayRouter.route("node-1a")
  → node-1a là direct child → enqueue pull job cho node-1a

node-1a polls → executes → reports result → node-1 reports → node-0 answers caller
```

### 4.2 Hop counting

Mỗi lần forward qua `_forward_via_next_hop`, `trace.hop_count += 1` và
`trace.route_path.append(current_node_id)`. Topology 3 lớp với `max_hop=5`:

```
hop 0: node-0 → forward to node-1   (hop_count=1)
hop 1: node-1 → forward to node-1a  (hop_count=2)
hop 2: node-1a executes
```

---

## 5. API mới / thay đổi

### 5.1 POST /nodes/register (extended)

**Request** (backward compatible — mọi field mới đều optional):
```json
{
  "node_id": "node-1",
  "address": null,
  "actions": ["execute_command", "read_file"],
  "advertise_routes": ["node-1a", "node-1b"],
  "capabilities": {
    "node-1a": ["execute_command", "get_order_info"],
    "node-1b": ["send_notification"]
  }
}
```

**Response** (extended với `routes_acknowledged`):
```json
{
  "node_id": "node-1",
  "registered": true,
  "routes_acknowledged": ["node-1a", "node-1b"],
  "message": "Node node-1 registered successfully"
}
```

**Logic server-side:**
1. `NodeRegistry.register()` cài node-1 với actions; cài node-1a, node-1b với `next_hop="node-1"`
2. Nếu node này đang chạy worker mode (`worker_agent_ref["agent"] is not None`):
   → gọi `agent.add_sub_route()` cho từng advertised node → trigger re-advertisement lên gateway của node này

### 5.2 GET /capabilities (mới)

**Response:**
```json
{
  "node_id": "node-0",
  "actions": ["execute_command", "read_file", "write_file"],
  "reachable": {
    "node-1": {
      "node_id": "node-1",
      "actions": ["execute_command"],
      "next_hop": null,
      "reachable": {}
    },
    "node-1a": {
      "node_id": "node-1a",
      "actions": ["execute_command", "get_order_info"],
      "next_hop": "node-1",
      "reachable": {}
    },
    "node-2": {
      "node_id": "node-2",
      "actions": ["execute_command"],
      "next_hop": null,
      "reachable": {}
    },
    "node-2a": {
      "node_id": "node-2a",
      "actions": ["send_notification"],
      "next_hop": "node-2",
      "reachable": {}
    }
  }
}
```

**Ý nghĩa của `next_hop`:**
- `null` — node là direct child, caller dùng `target_node_id` trực tiếp
- `"node-1"` — node reachable qua node-1; tuy nhiên **caller không cần quan tâm** — chỉ cần
  set `target_node_id: "node-1a"` và routing tự động xử lý

**Dùng bởi:**
- Claude Web: `GET /capabilities` để biết toàn bộ mesh trước khi orchestrate
- `IntentHandler._build_system_prompt()`: build context cho LLM
- Operators: debug topology

### 5.3 Endpoint summary (v5.10 — thêm vào danh sách v5.9)

| Method | Path | Mới/Thay đổi | Mô tả |
|--------|------|-------------|-------|
| POST | `/nodes/register` | Modified | Thêm `actions`, `advertise_routes`, `capabilities` |
| GET | `/capabilities` | **New** | Full capability tree với next_hop annotations |

---

## 6. Data Models

### 6.1 NodeRegistrationRequest (modified)

```python
class NodeRegistrationRequest(BaseModel):
    node_id: str
    address: str | None = None
    # v5.10
    actions: list[str] = []
    advertise_routes: list[str] = []
    capabilities: dict[str, list[str]] = {}
```

### 6.2 CapabilityNode (new)

```python
class CapabilityNode(BaseModel):
    node_id: str
    actions: list[str]
    next_hop: str | None = None
    reachable: dict[str, "CapabilityNode"] = {}
```

### 6.3 CapabilityTreeResponse (new)

```python
class CapabilityTreeResponse(BaseModel):
    node_id: str
    actions: list[str]
    reachable: dict[str, CapabilityNode]
```

### 6.4 _NodeEntry (modified, internal)

```python
@dataclass
class _NodeEntry:
    node_id: str
    address: str | None = None
    last_heartbeat: float | None = None
    status: NodeStatus = NodeStatus.UNKNOWN
    # v5.10
    actions: list = field(default_factory=list)
    next_hop: str | None = None   # None = direct child
```

---

## 7. NodeRegistry — Methods v5.10

| Method | Signature | Mô tả |
|--------|-----------|-------|
| `register()` | `(node_id, address?, actions?, advertise_routes?, capabilities?)` | Cài node + sub-routes |
| `get_next_hop()` | `(target_node_id) → str \| None` | None=unknown, id=next hop |
| `get_actions()` | `(node_id) → list[str]` | Actions của node, `[]` nếu unknown |
| `build_capability_tree()` | `(own_actions) → dict[str, CapabilityNode]` | Flat map toàn bộ reachable nodes |
| `is_trusted()` | `(node_id) → bool` | True cho cả direct và advertised nodes |
| *(unchanged)* | `ping`, `heartbeat`, `get_address`, `list_nodes`, ... | Không thay đổi |

**Lưu ý quan trọng về `is_trusted()`:** Method này không thay đổi code, nhưng behavior thay
đổi vì `_entries` bây giờ bao gồm cả indirect nodes. Điều này đồng nghĩa
`GatewayRouter.route()` sẽ bước qua trusted-check cho advertised nodes — nhưng phần
`get_next_hop()` được check trước nên indirect nodes luôn đi qua `_forward_via_next_hop()`
đúng path.

---

## 8. WorkerAgent — Thay đổi v5.10

### 8.1 Constructor

```python
WorkerAgent(config, executor, action_registry=None)
```

`action_registry` mới — dict `{action_name: module}` từ `load_actions()`. Dùng để:
- Build danh sách `actions` khi register
- Track `_sub_routes` (sub-node IDs → their actions)

### 8.2 `_register()` — payload mới

```python
payload = {
    "node_id": self._node_id,
    "address": self._self_address,
    "actions": list(self._action_registry.keys()),        # own actions
    "advertise_routes": list(self._sub_routes.keys()),    # known sub-nodes
    "capabilities": dict(self._sub_routes),               # {sub_id: [actions]}
}
```

### 8.3 `add_sub_route(sub_node_id, sub_actions)` — new

Được gọi bởi server `/nodes/register` handler khi một sub-node đăng ký:
1. Cập nhật `self._sub_routes[sub_node_id] = sub_actions`
2. Gọi `self._register()` để re-advertise lên gateway của node này

### 8.4 `_handle_mesh_forward(job)` — new

Xử lý job có `action="_mesh_forward"` — forwarding instruction từ gateway cấp trên:
1. Đọc `job.params["_mesh_forward_target"]` — original target
2. `POST self._self_url/action {target: original_target, action: ..., params: ...}`
3. Node's own GatewayRouter xử lý tiếp (có thể lại forward hoặc execute local)

---

## 9. IntentHandler — System Prompt v5.10

Trước v5.10, system prompt chỉ liệt kê online nodes và actions chung. Từ v5.10, dùng
`build_capability_tree()` để build context phong phú hơn:

```
## Mesh topology — nodes and their capabilities
  - node-0 [gateway, THIS NODE]
    actions: execute_command, read_file, write_file
  - node-1 (direct)
    actions: execute_command
  - node-1a via node-1
    actions: execute_command, get_order_info
  - node-2 (direct)
    actions: execute_command
  - node-2a via node-2
    actions: send_notification

## Routing rules
- Target ANY node listed above — routing is automatic, even through multiple NAT layers.
- Simply set `target_node_id` to the node that has the action you need.
```

LLM bây giờ có thể thấy `get_order_info` trên `node-1a` và biết cần call
`mesh_action(target_node_id="node-1a", action="get_order_info", ...)` mà không cần biết
gì về routing path.

---

## 10. Config v5.10

Không có config mới trong v5.10. Tuy nhiên, node-1 (intermediate gateway) cần được deploy
với `gateway_address` trỏ đến node-0 ĐỂ re-advertisement hoạt động:

```yaml
# node-1/node.yaml
node_id: node-1
listen: 0.0.0.0:8080
gateway_node_id: node-0
gateway_address: http://node-0-public-url:8080
auth_token: same-token-as-node-0
trusted_nodes:
  - node-1a
  - node-1b
```

```yaml
# node-1a/node.yaml
node_id: node-1a
listen: 0.0.0.0:8080
gateway_node_id: node-1
gateway_address: http://node-1-internal-ip:8080
auth_token: same-token-as-node-0
```

---

## 11. Use Case End-to-End: "Tôi muốn biết thông tin đơn hàng #123"

### Điều kiện khởi tạo

- node-1a đã đăng ký với node-1, node-1 đã re-advertise lên node-0
- node-0 NodeRegistry biết `node-1a → next_hop="node-1", actions=["get_order_info"]`

### Luồng qua POST /intent (Telegram)

```
[Telegram Bot]
  POST /intent {
    "prompt": "Tôi muốn biết thông tin đơn hàng #123",
    "session_id": "telegram-chat-12345678"
  }

[IntentHandler — node-0]
  Bước 1: build system prompt với capability tree
    → LLM thấy: "node-1a via node-1 — actions: get_order_info"

  Bước 2: LLM call
    → LLM trả: tool_call {
        name: "mesh_action",
        arguments: {
          "target_node_id": "node-1a",
          "action": "get_order_info",
          "params": {"order_id": "123"}
        }
      }

  Bước 3: IntentHandler._execute_tool_call()
    → GatewayRouter.route(target="node-1a")
    → get_next_hop("node-1a") = "node-1"
    → _forward_via_next_hop(request, "node-1")
    → node-1 has no address (NAT) → _pull() with _mesh_forward
    → job_id = "node1-job-abc"

  Bước 4: IntentHandler._poll_job("node1-job-abc")
    [background polling]

[node-1 WorkerAgent]
  → polls /jobs/poll → nhận _mesh_forward job
  → _claim_and_execute → _handle_mesh_forward()
  → POST http://localhost:8080/action {
      "target_node_id": "node-1a",
      "action": "get_order_info",
      "params": {"order_id": "123"}
    }
  → node-1 GatewayRouter.route("node-1a")
  → node-1a là direct child → _pull() → job queued for node-1a
  → node-1 reports "forwarded" result to node-0

[node-1a WorkerAgent]
  → polls → nhận job → execute get_order_info({"order_id": "123"})
  → trả: {"order_id": "123", "status": "DELIVERED", "items": [...]}
  → reports to node-1 gateway

[IntentHandler — poll resolves]
  → LLM nhận tool result
  → LLM reply: "Đơn hàng #123 đã được giao thành công vào ngày 28/02/2026.
                Gồm 3 sản phẩm: iPhone 16, AirPods Pro, USB-C cable."

[Telegram Bot]
  → user nhận tin nhắn reply
```

---

## 12. Backward Compatibility

| Tình huống | Behaviour |
|-----------|-----------|
| Node đăng ký không có `actions` | `actions=[]` — không ảnh hưởng routing |
| Node đăng ký không có `advertise_routes` | Không install sub-routes — routing cũ |
| Node dùng `trusted_nodes` static trong yaml | Vẫn hoạt động như trước — static whitelist vẫn được pre-populate |
| Caller gọi `POST /action` với direct child | Routing cũ (bước 5 trong decision tree) |
| Existing node không gửi `actions` | `get_actions()` trả `[]` — capability tree vẫn hiển thị node, chỉ không có action list |

---

## 13. Trạng thái hệ thống (v5.10)

**Đã có:**
- ✅ Bootstrap minimal + plugin actions
- ✅ Async job model (job_id polling)
- ✅ Distributed routing (push/pull/NAT)
- ✅ Authentication (Bearer token)
- ✅ Action schema validation
- ✅ NodeRegistry + WorkerAgent + JobQueue
- ✅ Claude Web curl-native workflow (v5.6)
- ✅ Lazy staleness + pull job timeout (v5.7)
- ✅ Queue depth in /health (v5.7)
- ✅ File upload/download (v5.8)
- ✅ POST /intent — ReAct agent loop (v5.9)
- ✅ ConversationStore — multi-turn session state (v5.9)
- ✅ LLMClient tool calling (v5.9)
- ✅ **BGP-style route advertisement (v5.10)**
- ✅ **Multi-hop routing qua N lớp NAT (v5.10)**
- ✅ **GET /capabilities — full capability tree (v5.10)**
- ✅ **WorkerAgent re-advertisement khi sub-node join (v5.10)**

**Chưa có:**
- ❌ Container isolation (P2)
- ❌ Session persistence across restarts (P3)
- ❌ Streaming intent response / SSE (P3)
- ❌ Upload chunked/resumable (P3)
- ❌ File ACL (P3)
- ❌ Route withdrawal khi node ngắt kết nối (P3) — xem Remaining Items

---

## 14. Known Limitations v5.10

### L1 — Route withdrawal chưa implement

Khi node-1a disconnect, NodeRegistry của node-0 vẫn giữ entry `node-1a → next_hop=node-1`.
Routing vẫn cố gắng forward → fail sau timeout. Lazy staleness check của node-1a's heartbeat
chưa được propagate upward.

**Workaround:** Pull job timeout (v5.7) đảm bảo request cuối cùng fail với rõ ràng thay vì
hang mãi mãi. TTL = `pull_job_timeout_seconds` (default 300s).

**Fix dự kiến (v5.11):** Node gửi `advertise_withdrawal` khi heartbeat miss threshold; gateway
xóa sub-routes của node đó.

### L2 — _mesh_forward không propagate job_id về caller

Khi next-hop là pull-mode, `_handle_mesh_forward` trả về
`{forwarded: true, downstream_job_id: "..."}`. IntentHandler hiện tại không poll
`downstream_job_id` — nó poll `job_id` gốc trên node-0, nhưng node-0 chỉ biết job trả về
kết quả "forwarded" chứ không phải kết quả thực từ node-1a.

**Ảnh hưởng:** Multi-hop pull chains (node-0 → node-1 pull → node-1a pull) hiện tại
chưa hoàn chỉnh end-to-end trong POST /intent. Push paths (node-0 → node-1 push → node-1a
pull) hoạt động đầy đủ vì intermediate hops là synchronous.

**Fix dự kiến (v5.11):** Result forwarding chain — khi node-1 hoàn thành forward, nó report
kết quả thực (không phải "forwarded") về node-0.

### L3 — Flat capability tree (không nested)

`build_capability_tree()` trả về flat map — mọi reachable node đều ở level 0 của dict
`reachable`, kèm `next_hop` annotation. Sub-tree của node-1 không được nest bên trong
entry của node-1. Đây là trade-off có chủ đích: simple over perfect.

---

*Spec: SPECS_V5.10.md | Mesh Runtime v5.10 | Repository: ai-infra-runtime-v2*
