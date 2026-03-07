Dưới đây là **Architecture Specification v4.1** — bản cập nhật đầy đủ và chính thức, tích hợp:

* Action-first model
* Plugin-based Python execution
* Markdown skill file
* Distributed self-routing mesh
* Async job model (không dùng mode flag)
* Extensible action system (thêm file Python là xong)

---

# AI-Orchestrated Distributed Execution Mesh

# Architecture Specification v4.1

## (Plugin-Based Node Runtime)

---

# 1. Mục tiêu v4.1

Phiên bản 4.1 chính thức chuyển Node thành:

> Distributed Plugin-Based Execution Runtime

Các mục tiêu:

* Action extensible vô hạn
* Không cần sửa core khi thêm capability
* Skill discovery bằng Markdown
* Action execution bằng Python plugin
* Hỗ trợ sync + async
* Distributed routing
* Phù hợp Digital Automation Factory

---

# 2. Tổng quan hệ thống

Hệ thống gồm 2 lớp chính:

## 2.1 Cloud AI Planner

Vai trò:

* Nhận yêu cầu từ user
* Load skill markdown từ node
* Reasoning
* Sinh task_id
* Quyết định gọi action nào
* Điều phối workflow
* Poll async job

Cloud có thể dùng LLM từ:

* OpenAI
* Anthropic
* Hoặc local model

Cloud KHÔNG:

* Thực thi action
* Lưu job state
* Routing

---

## 2.2 Execution Node

Mỗi node là:

* HTTP server
* Router
* Resolver
* Plugin runtime
* Job manager

Node có thể:

* Thực thi action local
* Proxy sang node khác
* Chạy async job

---

# 3. Cấu trúc Node

```
node/
 ├── node.yaml
 ├── skills.md
 ├── actions/
 │     ├── dump_database.py
 │     ├── check_disk.py
 │     ├── assemble_video.py
 │     └── ...
 └── runtime/
       ├── router.py
       ├── job_manager.py
       ├── action_loader.py
```

---

# 4. node.yaml

Ví dụ:

```yaml
node_id: node-2
listen: 0.0.0.0:8080

nodes:
  node-1: http://10.0.0.1:8080
  node-2: http://10.0.0.2:8080

default_resolver: node-1

max_hop: 10
cache_ttl_seconds: 300
```

---

# 5. skills.md

Markdown tự do.

Ví dụ:

```markdown
# Node: node-2

## Capabilities

### Action: assemble_video
Description:
Combine audio and images into a final MP4 video.

Input:
- audio_file (string)
- image_files (array)

Output:
- video_file_path (string)
```

Nguyên tắc:

* Node không parse skill.
* Cloud đọc skill để hiểu capability.
* Developer chịu trách nhiệm đồng bộ skill ↔ action.

---

# 6. Action Plugin Model

## 6.1 Nguyên tắc

* Mỗi file Python trong `actions/`
* Tên file = tên action
* Ví dụ: `assemble_video.py` ↔ `"assemble_video"`

---

## 6.2 Interface bắt buộc

```python
def run(params: dict, context: dict) -> dict:
    ...
```

---

## 6.3 context object

Node runtime inject:

```json
{
  "task_id": "...",
  "job_id": "... or None",
  "node_id": "...",
  "config": {...},
  "logger": "...",
  "storage": "...",
  "temp_dir": "..."
}
```

---

## 6.4 Async Action

Nếu action cần async, plugin phải khai báo:

```python
ASYNC = True
```

Hoặc:

```python
def run_async(params, context):
    ...
```

Runtime xử lý:

* Nếu ASYNC = True → spawn background job
* Trả job_id

---

# 7. Action Loading

Node khi startup:

1. Scan folder `actions/`
2. Import tất cả file `.py`
3. Build registry:

```python
action_registry = {
    "assemble_video": module,
    "check_disk": module
}
```

Hot reload (optional):

* Reload khi file thay đổi
* Hoặc reload mỗi request

---

# 8. API Endpoints

Node expose:

```
POST /action
GET  /result/{job_id}
GET  /resolve/{node_id}
GET  /skills
```

---

# 9. Request Envelope

```json
{
  "target_node_id": "node-2",
  "task_id": "task-001",
  "trace": {
    "hop_count": 0,
    "route_path": []
  },
  "payload": {
    "action": "assemble_video",
    "params": {
      "audio_file": "/tmp/audio.mp3",
      "image_files": ["/tmp/1.png"]
    }
  }
}
```

---

# 10. Routing Logic

Pseudo-code:

```
if target_node_id == self.node_id:
    execute_action()
else:
    resolve target
    increment hop_count
    append route_path
    proxy request
```

Reject nếu:

* hop_count > max_hop
* self.node_id trong route_path

---

# 11. Action Execution Flow

## 11.1 Sync

Response:

```json
{
  "task_id": "task-001",
  "status": "completed",
  "output": {...}
}
```

---

## 11.2 Async

Response:

```json
{
  "task_id": "task-001",
  "job_id": "node2-job-023",
  "status": "accepted",
  "estimated_completion_seconds": 60
}
```

Nếu có job_id ⇒ async.

---

# 12. Job Manager

Node maintain:

```
job_id → {
    task_id,
    status,
    progress,
    start_time,
    estimated_completion,
    output,
    error
}
```

Trạng thái:

* accepted
* running
* completed
* failed

---

# 13. GET /result/{job_id}

Running:

```json
{
  "status": "running",
  "progress": 45
}
```

Completed:

```json
{
  "status": "completed",
  "output": {...}
}
```

---

# 14. Storage Strategy (Khuyến nghị)

Vì action có thể tạo artifact lớn:

* Audio
* Image
* Video

Khuyến nghị có:

Storage Node chuyên biệt

Hoặc mount shared storage.

---

# 15. Digital Factory Use-Case (YouTube Automation)

Kiến trúc có thể gồm:

| Node            | Vai trò        |
| --------------- | -------------- |
| Trend Node      | Web + trending |
| Content Node    | Script         |
| Voice Node      | TTS            |
| Image Node      | Image          |
| Video Node      | Assemble       |
| Publishing Node | Upload         |
| Storage Node    | Artifact       |

Ví dụ tích hợp với:

* YouTube API
* Facebook API
* ElevenLabs

---

# 16. Bảo mật

Vì plugin là Python:

⚠ Nguy cơ RCE

Bắt buộc production:

* Chạy trong container
* Non-root user
* Timeout execution
* Memory limit
* Log đầy đủ
* Validate action name

---

# 17. Không nằm trong v4.1

* Intent endpoint
* Machine-validated schema
* Central service registry
* Message broker
* Workflow persistence engine

---

# 18. Định nghĩa kiến trúc chính thức

v4.1 là:

> LLM-Orchestrated Distributed Plugin Execution Mesh

Đặc điểm:

* LLM = Brain
* Node = Plugin Runtime
* Action = Python module
* Skill = Markdown discovery
* Async = job_id-based
* Routing = mesh-based

---

# 19. Hướng phát triển v4.2

Có thể bổ sung:

* Action metadata header
* Idempotency key
* Retry policy
* Artifact registry
* Structured skill schema

---