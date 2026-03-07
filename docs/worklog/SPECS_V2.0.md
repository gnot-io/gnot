Dưới đây là bản **Architecture Specification v2.0 (Full Rewrite)** đã cập nhật đầy đủ:

* Phân biệt rõ **Task ID** và **Job ID**
* Intent luôn async
* Action có thể sync hoặc async
* **Không sử dụng thuộc tính `mode`**
* Nếu response có `job_id` ⇒ đó là async
* Nếu không có `job_id` ⇒ đó là sync

Tài liệu này đủ chi tiết để một agent khác có thể bắt đầu coding.

---

# AI-Orchestrated Distributed Node System

## Architecture Specification v2.0

---

# 1. Mục tiêu hệ thống

Thiết kế một hệ thống phân tán gồm:

* Một **Cloud AI Web (LLM Brain)** thực hiện reasoning theo mô hình ReAct.
* Nhiều **Execution Nodes** độc lập.
* Mỗi node:

  * Có IP + Port riêng
  * Có file cấu hình YAML riêng
  * Có khả năng tự xử lý và tự proxy
* Không có gateway đặc biệt.
* Mọi node là symmetric.
* Routing dựa trên static config + default resolver fallback.
* Không sử dụng message broker trong v1.

---

# 2. Thành phần hệ thống

## 2.1 Cloud AI Web (LLM Brain)

Vai trò:

* Nhận prompt từ user.
* Phân tích intent.
* Sinh `task_id`.
* Quyết định `target_node_id`.
* Gửi request.
* Poll theo `job_id`.
* Tiếp tục reasoning cho tới khi hoàn tất workflow.

Cloud có thể dùng LLM từ:

* OpenAI
* Anthropic
* Hoặc LLM local.

Cloud:

* Không thực thi action.
* Không tham gia routing.
* Không giữ execution state của node.

---

## 2.2 Execution Node

Mỗi node:

* Có `node_id`
* Có `node.yaml`
* Expose các endpoint:

```
POST /intent
POST /action
GET  /result/{job_id}
GET  /resolve/{node_id}
```

Mỗi node vừa là:

* Execution unit
* Router
* Resolver

---

# 3. Định nghĩa khái niệm

## 3.1 Task ID (Global Workflow ID)

* Do Cloud tạo.
* Đại diện cho toàn bộ workflow logic.
* Không thay đổi trong suốt quá trình.
* Có thể span nhiều node.

Ví dụ:

```
task-20260301-0001
```

---

## 3.2 Job ID (Node Execution ID)

* Do node thực thi tạo.
* Đại diện cho một execution cụ thể tại node đó.
* Chỉ tồn tại khi xử lý async.
* Mỗi async execution phải có job_id.

Ví dụ:

```
node2-job-839201
```

---

## 3.3 Quan hệ

Một task có thể tạo nhiều job:

```
Task T1
 ├── Job J1 (node-2)
 ├── Job J2 (node-2)
 └── Job J3 (node-3)
```

Task = logical orchestration
Job = physical execution instance

---

# 4. Node Configuration (node.yaml)

Ví dụ:

```yaml
node_id: node-2
listen: 0.0.0.0:8080

nodes:
  node-1: http://10.0.0.1:8080
  node-2: http://10.0.0.2:8080

default_resolver: node-1

cache_ttl_seconds: 300
max_hop: 10
```

---

# 5. Request Envelope Specification

Mọi request tới `/intent` và `/action` phải có cấu trúc:

```json
{
  "target_node_id": "node-2",
  "task_id": "task-001",
  "trace": {
    "hop_count": 0,
    "route_path": []
  },
  "payload": { ... }
}
```

---

## 5.1 Fields

### target_node_id

Node đích cần xử lý.

### task_id

Workflow ID do Cloud tạo.

### trace.hop_count

Số lần proxy.

### trace.route_path

Danh sách node đã đi qua.

### payload

Dữ liệu business.

---

# 6. Routing Logic

## 6.1 Request Handling

Pseudo-code:

```
if request.target_node_id == self.node_id:
    process locally
else:
    address = resolve(target_node_id)
    validate hop_count
    append self.node_id vào route_path
    increment hop_count
    proxy request
```

---

## 6.2 Loop Protection

Reject nếu:

* hop_count > max_hop
* self.node_id xuất hiện trong route_path

---

# 7. Resolve Mechanism

## 7.1 Resolve Algorithm

```
if node_id == self.node_id:
    return LOCAL

if node_id in config.nodes:
    return config.nodes[node_id]

if default_resolver exists:
    call default_resolver/resolve/node_id
    cache result
    return result

else:
    throw NODE_NOT_FOUND
```

---

## 7.2 GET /resolve/{node_id}

Response:

```json
{
  "node_id": "node-3",
  "address": "http://10.8.0.3:8080"
}
```

---

# 8. Intent Processing

## 8.1 Intent luôn Async

### Request

```json
{
  "target_node_id": "node-2",
  "task_id": "task-001",
  "trace": {...},
  "payload": {
    "intent": "migrate_data",
    "params": {...}
  }
}
```

---

### Response

```json
{
  "task_id": "task-001",
  "job_id": "node2-job-001",
  "status": "accepted",
  "estimated_completion_seconds": 60
}
```

Luôn tạo job_id.

Node phải:

* Tạo background job
* Lưu job state

---

# 9. Action Processing

## 9.1 Sync Action

Response:

```json
{
  "task_id": "task-001",
  "status": "completed",
  "output": {...}
}
```

Không có job_id.

---

## 9.2 Async Action

Response:

```json
{
  "task_id": "task-001",
  "job_id": "node2-job-002",
  "status": "accepted",
  "estimated_completion_seconds": 25
}
```

Nếu có job_id ⇒ async.

---

# 10. Result Polling

Cloud poll:

```
GET /result/{job_id}
```

---

## 10.1 Running

```json
{
  "task_id": "task-001",
  "job_id": "node2-job-002",
  "status": "running",
  "progress": 40
}
```

---

## 10.2 Completed

```json
{
  "task_id": "task-001",
  "job_id": "node2-job-002",
  "status": "completed",
  "output": {...}
}
```

---

## 10.3 Failed

```json
{
  "task_id": "task-001",
  "job_id": "node2-job-002",
  "status": "failed",
  "error": "network timeout"
}
```

---

# 11. Node Internal Storage

Node phải maintain:

```
job_id → {
    task_id,
    status,
    start_time,
    estimated_completion,
    progress,
    output,
    error
}
```

Job state nên persist (file/db) nếu yêu cầu reliability cao.

---

# 12. LLM Control Loop

1. Cloud sinh task_id.
2. Gửi /intent.
3. Nhận job_id.
4. Chờ ~70–80% estimated time.
5. Poll /result/{job_id}.
6. Khi completed:

   * Continue reasoning
   * Có thể sinh action mới
7. Workflow kết thúc khi không còn job pending.

Cloud giữ:

```
task_id → {
    reasoning_context,
    job_list
}
```

---

# 13. Security Requirements

1. Validate trace integrity.
2. Enforce hop limit.
3. HMAC hoặc mTLS giữa node.
4. Action-level permission check.
5. Audit log mọi intent và action.

Node không được trust LLM tuyệt đối.

---

# 14. Non-Goals v2.0

* Service registry như Consul
* Service mesh như Istio
* Message broker
* WebSocket push

---

# 15. System Properties

* Symmetric node mesh
* Static-first topology
* Hierarchical resolution
* Async-first orchestration
* Deterministic routing
* Lightweight infrastructure
* ReAct-compatible control loop

---

# 16. Implementation Order

1. HTTP server skeleton
2. node.yaml loader
3. Resolve logic
4. Proxy logic
5. Job manager (async worker pool)
6. Intent handler
7. Action handler
8. Result handler
9. Loop protection
10. Security layer
11. Persistence (optional)

---

# 17. Final Contract

1. Intent luôn async và luôn trả job_id.
2. Action:

   * Không có job_id → sync.
   * Có job_id → async.
3. Poll theo job_id.
4. Task ID do Cloud tạo.
5. Job ID do Node tạo.
6. Routing dựa trên target_node_id.
7. Mọi node là symmetric.
8. Không dùng message broker.

---