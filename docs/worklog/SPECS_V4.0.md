Dưới đây là **Architecture Specification v4.0 (Action-First, Markdown Skill, Intent Deferred)** — viết lại đầy đủ, clean, và nhất quán theo các điều chỉnh mới:

* Skill không còn là YAML/JSON schema → chỉ là `skills.md`
* `/intent` tạm thời chưa triển khai (sẽ dùng LLM sau)
* Giai đoạn đầu chỉ tập trung vào `/action`
* Task ID & Job ID giữ nguyên
* Nếu response có `job_id` ⇒ async
* Không dùng mode sync/async
* Skill chỉ để LLM đọc hiểu, không để machine-validate

---

# AI-Orchestrated Distributed Node System

## Architecture Specification v4.0 (Action-First Phase)

---

# 1. Mục tiêu giai đoạn hiện tại

Giai đoạn v4.0 tập trung:

* Xây dựng distributed execution mesh
* Hoàn thiện routing
* Hoàn thiện action execution
* Hoàn thiện async job model
* Hoàn thiện polling

Chưa triển khai:

* Structured skill schema
* Machine validation theo JSON Schema
* Intent LLM execution tại node

---

# 2. Tổng quan hệ thống

Hệ thống gồm:

## 2.1 Cloud AI Web (LLM Brain)

Vai trò:

* Nhận prompt từ user
* Reasoning
* Sinh `task_id`
* Chọn `target_node_id`
* Gửi `/action`
* Nếu nhận `job_id` → poll
* Tiếp tục reasoning

Cloud:

* Không tham gia routing
* Không validate schema machine-level
* Đọc skill markdown để biết node làm được gì

Cloud có thể dùng LLM từ:

* OpenAI
* Anthropic
* Hoặc local model

---

## 2.2 Execution Node

Mỗi node:

* Có IP + Port
* Có `node.yaml`
* Có `skills.md`
* Expose HTTP API

Node vừa là:

* Execution unit
* Router
* Resolver

---

# 3. File của Node

---

## 3.1 node.yaml (Network & Routing)

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

## 3.2 skills.md (Human-Readable Capability File)

Đây là file Markdown tự do.

Ví dụ:

```markdown
# Node: node-2

## Capabilities

### Action: dump_database
Description:
Dump a database to a file.

Input:
- db_name (string)

Output:
- file_path (string)

---

### Action: check_disk
Description:
Check available disk space.

Input:
- none

Output:
- free_space_gb (number)
```

Quan trọng:

* Không có schema machine-validated.
* LLM sẽ đọc và hiểu nội dung này.
* Node không parse Markdown để validate.

---

# 4. API Endpoints (v4.0)

Hiện tại node expose:

```
POST /action
GET  /result/{job_id}
GET  /resolve/{node_id}
GET  /skills
```

Lưu ý:

* `/intent` sẽ bổ sung ở phase sau.
* Hiện tại không triển khai `/intent`.

---

# 5. GET /skills

Trả về raw markdown.

Response:

```text
Content-Type: text/markdown

# Node: node-2
...
```

Cloud sẽ:

1. Fetch `/skills`
2. Cache nội dung
3. Inject vào prompt của LLM khi cần

Node không cần hiểu nội dung skill.

---

# 6. Task ID và Job ID

---

## 6.1 Task ID (Global Workflow ID)

* Do Cloud tạo.
* Xuyên suốt toàn bộ workflow.
* Có thể tạo nhiều action call.

Ví dụ:

```
task-20260301-001
```

---

## 6.2 Job ID (Node Execution ID)

* Do node tạo.
* Chỉ xuất hiện nếu action async.
* Scoped tại node.

Ví dụ:

```
node2-job-00023
```

---

# 7. Request Envelope (Action Only)

Mọi `/action` request phải có:

```json
{
  "target_node_id": "node-2",
  "task_id": "task-001",
  "trace": {
    "hop_count": 0,
    "route_path": []
  },
  "payload": {
    "action": "dump_database",
    "params": {
      "db_name": "customer_db"
    }
  }
}
```

---

# 8. Routing Logic

## 8.1 Khi nhận request

Pseudo-code:

```
if target_node_id == self.node_id:
    process_action()
else:
    address = resolve(target_node_id)
    validate hop_count
    append self.node_id vào route_path
    increment hop_count
    proxy request
```

---

## 8.2 Loop Protection

Reject nếu:

* hop_count > max_hop
* self.node_id nằm trong route_path

---

# 9. Resolve Logic

## 9.1 Resolve Algorithm

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
    NODE_NOT_FOUND
```

---

## 9.2 GET /resolve/{node_id}

Response:

```json
{
  "node_id": "node-3",
  "address": "http://10.8.0.3:8080"
}
```

---

# 10. Action Execution Model

## 10.1 Sync Action

Node xử lý ngay và trả:

```json
{
  "task_id": "task-001",
  "status": "completed",
  "output": {
    "free_space_gb": 120
  }
}
```

Không có `job_id`.

---

## 10.2 Async Action

Node:

1. Tạo job_id
2. Spawn background job
3. Lưu state

Response:

```json
{
  "task_id": "task-001",
  "job_id": "node2-job-023",
  "status": "accepted",
  "estimated_completion_seconds": 30
}
```

Nếu có `job_id` ⇒ async.

---

# 11. Polling

Cloud gọi:

```
GET /result/{job_id}
```

---

## 11.1 Running

```json
{
  "task_id": "task-001",
  "job_id": "node2-job-023",
  "status": "running",
  "progress": 40
}
```

---

## 11.2 Completed

```json
{
  "task_id": "task-001",
  "job_id": "node2-job-023",
  "status": "completed",
  "output": {...}
}
```

---

## 11.3 Failed

```json
{
  "task_id": "task-001",
  "job_id": "node2-job-023",
  "status": "failed",
  "error": "disk full"
}
```

---

# 12. Node Internal Storage

Node maintain:

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

Persistence:

* In-memory (v4 minimal)
* Durable storage (optional later)

---

# 13. Cloud Control Loop (Action-First)

1. Load skills from node.
2. Inject skill markdown vào prompt.
3. LLM decide action name + params.
4. Gửi `/action`.
5. Nếu response có `job_id`:

   * Sleep ~70% estimated time
   * Poll `/result`
6. Khi completed:

   * Continue reasoning.

---

# 14. Security (Minimal v4)

* Validate hop_count
* Validate route_path
* Optional HMAC
* Log all action execution

---

# 15. Non-Goals (v4)

* Machine-validated schema
* Intent endpoint
* Structured skill definition
* Service registry như Consul
* Service mesh như Istio
* Message broker

---

# 16. Kiến trúc hiện tại là gì?

Hệ thống bây giờ là:

> LLM-driven distributed action execution mesh
> với self-routing node
> và skill-based capability discovery (Markdown-driven)

---

# 17. Phase Roadmap

### v4 (hiện tại)

Action-only execution fabric.

### v5

Intent endpoint dùng LLM tại node.

### v6

Structured skill schema + validation.