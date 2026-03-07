VẤN ĐỀ: 1 vấn đề phát sinh là khi có 1 worker node nằm trong mạng LAN/NAT, thấy node-0/gateway node nhưng node-0/gateway node lại không thấy nó, thì khi đó từ LLM giao việc xuống thì không thể giao đến worker node này.

GIẢI PHÁP: hỗ trợ mô hình job pull/push như sau:

* LLM không bao giờ nói chuyện trực tiếp với node worker. Gateway là coordinator trung tâm.
* Mỗi node có khai báo 1 gateway node của nó
* Mỗi node/gateway node có lưu 1 danh sách các trusted-nodes  mà nó có thể proxy job sang đó
* Mỗi node/gateway node sẽ có job queue (lưu theo từng target node)
* Nếu LLM gửi action đến target_node_id không tồn tại / không nằm trong danh sách trusted-nodes → Gateway trả lỗi ngay lập tức. Nếu nó thấy thì nó sẽ proxy action sang target node. Tức Gateway có 2 cách giao job cho worker: Push trực tiếp (proxy HTTP) nếu worker reachable hoặc Enqueue + Pull nếu worker không reachable.
	* Gateway có thể gọi endpoint /ping của target node để kiểm tra xem có thấy node đó không
	* CASE A — Worker reachable (Push Mode)
		Gateway sẽ:
		POST worker_endpoint/action
		Nếu worker trả sync → trả ngay cho LLM
		Nếu worker trả job_id → pass-through job_id

		Nếu call fail (timeout, network error):

		→ fallback sang queue mode.
	* CASE B — Worker không reachable (Pull Mode)
		Gateway sẽ:		Tạo job ->		Lưu vào queue của node ->		Trả job_id cho LLM
		Worker sẽ poll.
* Một node sẽ đăng ký với gateway của nó vào danh sách trusted-node và định kỳ nó sẽ gửi heartbeat đến gateway node (hoặc nó có cung cấp 1 /ping endpoint)
* Mỗi node poll đến gateway mà nó đăng ký để xem có job nào cho nó không
* Nếu worker node  offline, Gateway vẫn nhận job và queue lại. LLM sẽ thấy job vẫn pending.
* Kiến Trúc Tổng Thể
User
  ↓
Cloud LLM
  ↓
Gateway (node-0)
  ↓ (job queue per node)
Worker Nodes (node-1, node-2, ...)

# Gateway Node (Coordinator)

Lưu danh sách node

Nhận action từ LLM

Validate target_node_id

Enqueue job vào queue đúng node

Trả job_id cho LLM

Nhận result từ node

Trả result cho LLM

# Worker Node

Đăng ký với Gateway

Gửi heartbeat định kỳ

Poll job

Claim job

Execute

Report result

Worker không nhận job push từ Gateway.

------------

Để anh confirm trước khi code. Em đọc kỹ codebase hiện tại và phân tích ảnh hưởng cụ thể. Đây là plan:

---

## Implementation Plan: Push/Pull Job Model (v5.3)

### 1. Những gì thay đổi trong luồng hiện tại

Hiện tại `router.py` chỉ có một path duy nhất: **resolve → proxy HTTP hoặc lỗi**. Với v5.3, router sẽ có thêm path thứ hai: **resolve → ping → enqueue nếu không reach được**.

`job_id` hiện tại chỉ sống trong worker node. Với v5.3, khi pull mode, `job_id` sẽ sống ở **Gateway** — vì LLM chỉ nói chuyện với Gateway, và Gateway phải trả lời được `GET /result/{job_id}`.

---

### 2. Files mới (4 files)

**`runtime/node_registry.py`** — Quản lý trusted nodes tại Gateway:
```
register(node_id, address?)      # worker đăng ký
heartbeat(node_id)               # cập nhật timestamp
is_trusted(node_id) → bool       # kiểm tra trước khi nhận job
get_reachable_address(node_id)   # trả address nếu node đang online
```
- Lưu: `node_id → {address, last_heartbeat, last_known_reachable}`
- Background task: mark node unreachable nếu heartbeat timeout (configurable)

**`runtime/job_queue.py`** — Per-node job queue tại Gateway:
```
enqueue(node_id, job_id, payload) → QueuedJob
poll(node_id) → list[QueuedJob]  # worker gọi để lấy jobs chờ
claim(job_id, node_id) → QueuedJob | None
```
- Lưu in-memory: `Dict[node_id, List[QueuedJob]]`
- `QueuedJob` chứa đủ: `job_id`, `task_id`, `action`, `params`, `created_at`

**`runtime/gateway_router.py`** — Router nâng cấp kế thừa logic từ `router.py`:
```
route(request):
  1. Check target_node_id ∈ trusted_nodes → else 400 ngay
  2. Nếu target là local → execute local (giữ nguyên)
  3. Nếu target là remote:
     a. Ping target → reachable?
        → YES: proxy HTTP (push mode), nếu fail → fallback queue
        → NO: enqueue (pull mode), trả job_id
```

**`runtime/worker_agent.py`** — Background agent chạy trong worker node:
```
start():
  → register_with_gateway()
  → loop: heartbeat mỗi N giây
  → loop: poll jobs → claim → execute → report result
```

---

### 3. Files sửa đổi (5 files)

**`runtime/config.py`** — Thêm fields:
```yaml
# Worker node config
gateway_node_id: node-0
gateway_address: http://10.0.0.1:8080

# Gateway node config
trusted_nodes:
  - node-1
  - node-2
heartbeat_timeout_seconds: 30    # sau bao lâu không heartbeat → unreachable
poll_interval_seconds: 5         # worker poll gateway mỗi 5s
ping_timeout_seconds: 3          # timeout khi gateway ping worker
```

**`runtime/models.py`** — Thêm models:
```python
class NodeRegistration       # POST /nodes/register
class HeartbeatRequest       # POST /nodes/{node_id}/heartbeat
class QueuedJob              # job trong queue
class PollResponse           # GET /jobs/poll response
class JobResultReport        # POST /jobs/{job_id}/result
JobStatus thêm: QUEUED       # nằm trong queue, chờ worker pull
```

**`runtime/server.py`** — Thêm 5 endpoints mới (worker-facing):
```
POST /nodes/register          # worker đăng ký
POST /nodes/{node_id}/heartbeat
GET  /ping                    # reachability check (exempt auth)
GET  /jobs/poll               # worker poll ?node_id=xxx
POST /jobs/{job_id}/claim     # worker claim job
POST /jobs/{job_id}/result    # worker report kết quả
```
Endpoint `/result/{job_id}` hiện tại **giữ nguyên** — LLM không cần biết push hay pull.

**`runtime/router.py`** — Minimal change: deprecate, delegate sang `gateway_router.py`

**`node_runtime.py`** — Khi startup: nếu config có `gateway_node_id` → khởi động `WorkerAgent`

---

### 4. Sequence Flow

**Push Mode (worker reachable):**
```
LLM → POST /action {target: node-1}
  Gateway: node-1 ∈ trusted? ✓
  Gateway: GET node-1/ping → 200
  Gateway: POST node-1/action → {job_id: "node1-job-xxx"}
  Gateway: trả pass-through {job_id} cho LLM
LLM → GET /result/node1-job-xxx
  Gateway: proxy GET node-1/result/node1-job-xxx
```

**Pull Mode (worker behind NAT):**
```
LLM → POST /action {target: node-1}
  Gateway: node-1 ∈ trusted? ✓
  Gateway: GET node-1/ping → timeout
  Gateway: tạo job_id "node0-job-yyy" trong own JobManager
  Gateway: enqueue(node-1, job_id, payload)
  Gateway: trả {job_id: "node0-job-yyy", status: "accepted"}

Worker → GET /jobs/poll?node_id=node-1
  Gateway: trả [{job_id, action, params}]
Worker → POST /jobs/node0-job-yyy/claim
Worker: execute action locally
Worker → POST /jobs/node0-job-yyy/result {output: ...}
  Gateway: update JobManager[node0-job-yyy] → completed

LLM → GET /result/node0-job-yyy
  Gateway: trả completed từ own JobManager ✓
```

---

### 5. Những gì KHÔNG thay đổi

| Component | Lý do |
|-----------|-------|
| `GET /result/{job_id}` interface | LLM vẫn poll như cũ |
| `POST /action` request envelope | Không thay đổi format |
| Action executor, action plugins | Không liên quan |
| Auth middleware | Giữ nguyên |
| Schema validation | Giữ nguyên |
| Bootstrap workflow | Giữ nguyên |

---

### Câu hỏi cần anh xác nhận

**Q1:** Khi Push mode fail (proxy timeout/error), em sẽ **fallback sang queue** và trả `job_id` cho LLM. Anh có muốn LLM biết rằng job đã chuyển sang pull mode không, hay cứ transparent hoàn toàn?

**Q2:** Khi worker poll và nhận job, worker sẽ execute bằng **local ActionExecutor** của chính nó. Vậy action phải đã được load sẵn trong worker. Anh confirm đây là đúng ý không, hay gateway sẽ gửi cả code xuống?

**Q3:** `GET /result/{job_id}` hiện tại chỉ query local JobManager. Khi pull mode, `job_id` sống ở gateway — ổn. Nhưng khi push mode, `job_id` sống ở **worker node**. Gateway sẽ cần **proxy** `GET /result` sang worker. Em sẽ implement theo hướng: gateway track xem job_id nào là local và cái nào cần proxy sang worker. Anh đồng ý không?

-------

Q1 -> cứ transparent hoàn toàn
Q2 -> action phải đã được load sẵn trong worker
Q3 -> Anh đồng ý