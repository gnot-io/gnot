# Worklog — Execution Mesh v5.3 (Push/Pull Job Model)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-04
**Author:** Claude (AI Coding Agent)
**Base version:** v5.2 → **v5.3**

---

## 1. Tổng quan

### Vấn đề

Kiến trúc v5.0–v5.2 giả định gateway luôn có thể gọi HTTP trực tiếp vào worker node. Điều này không đúng trong thực tế: worker node thường nằm sau NAT/LAN, thấy được gateway nhưng gateway không thấy được worker. Khi đó mọi action gửi đến worker đều thất bại.

### Giải pháp

v5.3 thêm **mô hình Push/Pull** — gateway tự động phát hiện worker có reachable hay không và điều phối theo hai mode:

| Mode | Điều kiện | Cơ chế |
|------|-----------|--------|
| **Push** | Worker reachable (gateway ping được) | Gateway proxy HTTP trực tiếp — giống v5.2 |
| **Pull** | Worker sau NAT (ping fail hoặc không có địa chỉ) | Gateway enqueue job, worker chủ động poll |

**LLM và Cloud Planner hoàn toàn transparent** — interface `POST /action` và `GET /result/{job_id}` không thay đổi. LLM không cần biết job được deliver theo mode nào.

---

## 2. Kiến trúc mới

### 2.1 Sơ đồ tổng thể

```
Cloud LLM
    │
    │ POST /action {target_node_id: "node-1"}
    ▼
Gateway (node-0)
    │
    ├─ node-1 ∈ trusted_nodes? ──── NO ──→ 400 UNTRUSTED_NODE
    │
    ├─ GET node-1/ping
    │      │
    │      ├── 200 OK → PUSH: POST node-1/action
    │      │             nếu fail → fallback PULL
    │      │
    │      └── timeout/error → PULL: enqueue vào job_queue[node-1]
    │                          trả job_id cho LLM
    │
    ▼
Worker (node-1) — sau NAT, chạy WorkerAgent
    │
    ├── Startup: POST gateway/nodes/register
    ├── Loop:    POST gateway/nodes/node-1/heartbeat  (mỗi 10s)
    └── Loop:    GET  gateway/jobs/poll?node_id=node-1 (mỗi 5s)
                   │
                   ├── Có job → POST gateway/jobs/{job_id}/claim
                   ├── Execute locally
                   └── POST gateway/jobs/{job_id}/result
```

### 2.2 GET /result/{job_id} — routing mới

| Loại job | Nơi lưu state | Cách trả kết quả |
|----------|--------------|-----------------|
| LOCAL | Local JobManager | Query trực tiếp |
| PULL | Local JobManager (gateway owns it) | Query trực tiếp |
| PUSH | Worker's JobManager | Proxy GET sang worker |

---

## 3. Features đã implement

### A. NodeRegistry (`runtime/node_registry.py`)

**204 LOC** — Gateway-side store quản lý trusted nodes.

**Chức năng:**

| Method | Mô tả |
|--------|-------|
| `is_trusted(node_id)` | Kiểm tra whitelist trước khi nhận job |
| `register(node_id, address?)` | Worker đăng ký — static (từ config) hoặc dynamic (POST /nodes/register) |
| `heartbeat(node_id)` | Cập nhật `last_heartbeat`, set status ONLINE |
| `ping(node_id)` | Active probe tới `/ping` của worker — quyết định push vs pull |
| `mark_stale_nodes_unreachable()` | Background: mark UNREACHABLE nếu heartbeat quá `heartbeat_timeout_seconds` |
| `list_nodes()` | Trả danh sách NodeInfo cho debug endpoint |

**NodeStatus lifecycle:**

```
UNKNOWN → ONLINE (heartbeat/ping OK)
ONLINE  → UNREACHABLE (heartbeat stale hoặc ping fail)
UNREACHABLE → ONLINE (heartbeat nhận lại)
```

**Config (node.yaml):**

```yaml
trusted_nodes:
  - node-1
  - node-2
heartbeat_timeout_seconds: 30
ping_timeout_seconds: 3
```

---

### B. JobQueue (`runtime/job_queue.py`)

**201 LOC** — Per-node pull queue và push-job routing table.

**Hai vai trò:**

**B.1 Pull queue** — Lưu jobs chờ worker pull:

```
enqueue(node_id, task_id, action, params) → QueuedJob
poll(node_id) → list[QueuedJob]           # chỉ unclaimed
claim(job_id, node_id) → QueuedJob | None # atomic, reject nếu đã claimed
remove_from_queue(job_id, node_id)        # sau khi worker report xong
```

**B.2 Push routing table** — Track push-mode jobs để proxy `/result`:

```
register_push_job(job_id, task_id, target_node_id, worker_address)
get_route(job_id) → _JobRouteEntry | None  # mode: PUSH | PULL
```

**Claim là atomic** — nếu hai worker cùng poll và cùng gọi claim, chỉ một cái thành công. Worker kia nhận `None` và bỏ qua.

---

### C. GatewayRouter (`runtime/gateway_router.py`)

**308 LOC** — Thay thế `RequestRouter`. Toàn bộ routing logic push/pull nằm ở đây.

**route() decision tree:**

```python
if hop_count > max_hop or self in route_path:
    return ErrorResponse (loop protection — giữ nguyên)

if target == self:
    return execute_local() (giữ nguyên)

if target not in trusted_nodes:
    return ErrorResponse("UNTRUSTED_NODE")  # ← NEW: whitelist gate

address = node_registry.get_address(target)
if not address:
    return _pull()  # ← không có địa chỉ, queue ngay

reachable = await node_registry.ping(target)
if reachable:
    result = await _push(address)
    if isinstance(result, ErrorResponse):
        return await _pull()  # ← fallback transparent
    return result
else:
    return await _pull()
```

**route_result() — GET /result routing:**

```python
# 1. Check local JobManager (LOCAL + PULL jobs)
job = await job_manager.get_job(job_id)
if job: return JobStatusResponse(...)

# 2. Check job_queue routing table (PUSH jobs)
route = await job_queue.get_route(job_id)
if route.mode == PUSH:
    return await _proxy_result(job_id, route.worker_address)

return ErrorResponse("JOB_NOT_FOUND")
```

---

### D. WorkerAgent (`runtime/worker_agent.py`)

**285 LOC** — Background service chạy bên trong mỗi worker node.

**Khởi động khi** `node_runtime.py` phát hiện config có `gateway_node_id` + `gateway_address`.

**3 vòng lặp song song (asyncio tasks):**

| Task | Interval | Việc làm |
|------|----------|---------|
| Registration | Once on startup (retry 12 lần) | `POST /nodes/register` |
| Heartbeat loop | `heartbeat_interval_seconds` (default 10s) | `POST /nodes/{node_id}/heartbeat` |
| Poll loop | `poll_interval_seconds` (default 5s) | `GET /jobs/poll` → claim → execute → report |

**Claim-and-execute flow:**

```
poll() → [job1, job2, ...]
  for each job:
    claim(job_id)  ← atomic, skip nếu thất bại
    execute locally via ActionExecutor
    nếu action là async → await local job_manager
    report result → POST /jobs/{job_id}/result
```

**Config (node-1/node.yaml):**

```yaml
gateway_node_id: node-0
gateway_address: http://10.0.0.1:8080
self_address: null        # nếu null → gateway không thể push, luôn pull
heartbeat_interval_seconds: 10
poll_interval_seconds: 5
```

---

### E. Server endpoints mới (`runtime/server.py`)

6 endpoints mới phục vụ worker-gateway communication:

| Endpoint | Method | Auth | Mô tả |
|----------|--------|------|-------|
| `/ping` | GET | ❌ exempt | Reachability probe — gateway gọi để quyết định push vs pull |
| `/nodes/register` | POST | ✅ | Worker đăng ký với gateway |
| `/nodes/{node_id}/heartbeat` | POST | ✅ | Worker gửi heartbeat |
| `/nodes` | GET | ✅ | List trusted nodes + status (debug) |
| `/jobs/poll` | GET | ✅ | Worker poll jobs `?node_id=xxx` |
| `/jobs/{job_id}/claim` | POST | ✅ | Worker claim job (atomic) |
| `/jobs/{job_id}/result` | POST | ✅ | Worker báo cáo kết quả |

**Endpoints giữ nguyên interface (transparent với LLM):**

- `POST /action` — vẫn nhận cùng request envelope
- `GET /result/{job_id}` — vẫn trả cùng JobStatusResponse, nhưng giờ routing qua GatewayRouter

---

### F. Config thay đổi (`runtime/config.py`, `node.yaml`)

**NodeConfig thêm 8 fields:**

```python
# Gateway fields
trusted_nodes: list[str]            # whitelist node IDs
heartbeat_timeout_seconds: int      # default 30
ping_timeout_seconds: float         # default 3.0

# Worker fields
gateway_node_id: str | None         # ID của gateway node
gateway_address: str | None         # HTTP address của gateway
self_address: str | None            # địa chỉ của chính worker (nếu reachable)
heartbeat_interval_seconds: int     # default 10
poll_interval_seconds: int          # default 5
```

**Hai derived properties:**

```python
config.is_gateway  # True nếu có trusted_nodes
config.is_worker   # True nếu có gateway_node_id + gateway_address
```

---

## 4. Files thay đổi

### Files mới (4)

| File | LOC | Mô tả |
|------|-----|-------|
| `runtime/gateway_router.py` | 308 | Push/pull router thay thế RequestRouter |
| `runtime/node_registry.py` | 204 | Trusted node store + liveness tracking |
| `runtime/job_queue.py` | 201 | Per-node pull queue + push routing table |
| `runtime/worker_agent.py` | 285 | Worker background agent |

### Files sửa đổi (7)

| File | Thay đổi chính |
|------|---------------|
| `runtime/models.py` | Thêm `JobStatus.QUEUED`, `JobMode`, `NodeStatus`; thêm 7 models: `NodeRegistrationRequest/Response`, `HeartbeatRequest/Response`, `NodeInfo`, `QueuedJob`, `PollResponse`, `ClaimRequest/Response`, `JobResultReport` |
| `runtime/config.py` | Thêm 8 fields gateway/worker; thêm `is_gateway`, `is_worker` properties; cải thiện log startup |
| `runtime/server.py` | Dùng `GatewayRouter` thay `RequestRouter`; thêm 6 endpoints; `/result/{job_id}` route qua GatewayRouter; init `NodeRegistry` + `JobQueue` |
| `runtime/auth.py` | Thêm `/ping` vào `DEFAULT_EXEMPT_PATHS` |
| `node_runtime.py` | Thêm `attach_worker_agent()` — tự động start `WorkerAgent` nếu `config.is_worker`; log mode (gateway/worker/standalone) |
| `node-0/node.yaml` | Thêm `trusted_nodes`, `heartbeat_timeout_seconds`, `ping_timeout_seconds` |
| `node-1/node.yaml` | **Mới** — worker config mẫu với `gateway_node_id`, `gateway_address`, `self_address` |

### Files không thay đổi

`router.py` (giữ lại cho backward compat), `resolver.py`, `action_executor.py`, `action_loader.py`, `job_manager.py`, `llm_client.py`, `schema_validator.py`, `bootstrap.py`, `models.py` (base models)

---

## 5. Test Suite

### Files mới (4)

| Test file | Tests | Coverage |
|-----------|-------|---------|
| `tests/test_node_registry.py` | 10 | register, heartbeat, stale detection, ping reachable/unreachable/no-address, list |
| `tests/test_job_queue.py` | 10 | enqueue, poll, claim, double-claim, wrong-node claim, push registration, routing, depth, remove |
| `tests/test_gateway_router.py` | 11 | local exec, untrusted reject, hop/loop protection, push sync, push async, pull no-address, pull unreachable, push-fail fallback, route_result local/unknown |
| `tests/test_v53_integration.py` | 9 | Full ASGI stack: ping, register, heartbeat, list nodes, pull enqueue, complete poll→claim→report→result flow, untrusted action, untrusted poll |

### Tổng kết test

| Test file | v5.2 | v5.3 | Delta |
|-----------|------|------|-------|
| test_action_loader.py | 5 | 5 | — |
| test_actions.py | 9 | 9 | — |
| test_auth.py | 6 | 6 | — |
| test_bootstrap.py | 8 | 8 | — |
| test_integration.py | 10 | 10 | — |
| test_job_cleanup.py | 7 | 7 | — |
| test_job_manager.py | 8 | 8 | — |
| test_llm_client.py | 11 | 11 | — |
| test_resolver.py | 4 | 4 | — |
| test_router.py | 5 | 5 | — |
| test_schema_validator.py | 12 | 12 | — |
| test_v52_features.py | 13 | 13 | — |
| **test_node_registry.py** | — | 10 | **+10 NEW** |
| **test_job_queue.py** | — | 10 | **+10 NEW** |
| **test_gateway_router.py** | — | 11 | **+11 NEW** |
| **test_v53_integration.py** | — | 9 | **+9 NEW** |
| **Tổng** | **102** | **142** | **+40** |

**142/142 Pass ✅**

---

## 6. Behavior Matrix

| Scenario | Gateway nhìn thấy worker? | self_address set? | Mode | Behavior |
|----------|--------------------------|-------------------|------|---------|
| Worker trong cùng mạng LAN | ✅ | ✅ | **Push** | Proxy HTTP trực tiếp |
| Worker sau NAT | ❌ | ❌ | **Pull** | Enqueue, worker poll |
| Worker vừa khởi động (chưa heartbeat) | ❓ | ✅ | **Push → fallback Pull** | Ping fail → queue |
| Worker offline tạm thời | ❌ | ✅ | **Pull** | Job queue lại, LLM thấy `accepted` → pending |
| Worker quay lại online | ✅ | ✅ | **Push** | Các job mới gửi push; job cũ trong queue vẫn được worker poll và xử lý |

---

## 7. Những gì chưa hoàn thành (Remaining)

| Priority | Item | Ghi chú |
|----------|------|---------|
| **P1** | **Cloud AI Planner** | ReAct reasoning loop — vẫn là item quan trọng nhất chưa implement |
| P2 | Heartbeat stale-checker loop | `mark_stale_nodes_unreachable()` đã có nhưng chưa có background task gọi nó định kỳ — cần thêm vào server startup |
| P2 | Multi-node live test (real HTTP) | Tests hiện dùng ASGI transport + mock; chưa test 2 process thật với NAT thực |
| P2 | Container isolation per-node | Bootstrap tạo process, chưa tạo Docker container |
| P3 | Job queue persistence | Queue mất khi gateway restart — cần SQLite hoặc Redis |
| P3 | Pull job timeout | Job trong queue không có timeout; nếu worker offline vĩnh viễn, LLM sẽ poll mãi |
| P3 | Queue depth trong /health | `GET /health` chưa expose `queue_depths` per node |
| P3 | Migrate `on_event` → `lifespan` | FastAPI deprecation warning |
| P4 | Idempotency key | Tránh double-enqueue nếu LLM retry cùng request |

---

## 8. Metrics tổng kết

| Metric | v5.0 | v5.1 | v5.2 | v5.3 |
|--------|------|------|------|------|
| Files code | 25 | 36 | 44 | 51 |
| Lines of code | ~1,200 | ~2,400 | ~4,000 | ~5,600 |
| Test cases | 35 | 78 | 102 | 142 |
| Test pass rate | 100% | 100% | 100% | 100% |
| Endpoints | 5 | 6 | 8 | 14 |
| Actions (seed) | 3 | 3 | 5 | 5 |
| Delivery modes | Push only | Push only | Push only | **Push + Pull** |

---

*Document generated: 2026-03-04 | Mesh Runtime v5.3 | Repository: ai-infra-runtime-v2*