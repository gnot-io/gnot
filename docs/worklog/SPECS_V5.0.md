# AI-Orchestrated Self-Bootstrapping Execution Mesh

## Architecture Specification v5.0

### (Single Seed → Infinite Expansion)

---

## 1. Triết lý thiết kế

Hệ thống được thiết kế theo nguyên lý:

> Tối giản tuyệt đối ban đầu — nhưng có khả năng tự mở rộng vô hạn.

Ban đầu hệ thống chỉ gồm:

```
User
  ↓
Cloud LLM
  ↓
Seed Node (node-0)
```

- Không có node khác.
- Không có cluster.
- Không có provisioning layer riêng.

Tất cả được bootstrap từ Seed Node.

---

## 2. Thành phần hệ thống

### 2.1 Cloud AI Planner

Vai trò:

- Nhận yêu cầu từ user
- Reasoning theo mô hình ReAct
- Sinh `task_id`
- Quyết định gọi action nào
- Tạo node mới khi cần
- Mở rộng cluster
- Poll async job

Cloud có thể dùng LLM từ:

- OpenAI
- Anthropic
- Hoặc model nội bộ

Cloud **không** thực thi hạ tầng. Cloud **chỉ** gọi action qua HTTP.

---

### 2.2 Seed Node (node-0)

Seed Node là node duy nhất tồn tại ban đầu.

Seed Node đóng đồng thời các vai trò:

- Execution runtime
- Gateway
- Bootstrap engine
- Infrastructure controller

Seed Node expose:

```
POST /action
GET  /result/{job_id}
GET  /resolve/{node_id}
GET  /skills
```

---

## 3. Seed Node Capabilities (Cố Định)

Seed Node chỉ có 3 action cơ bản:

### 3.1 `write_file`

```json
{
  "action": "write_file",
  "params": {
    "path": "string",
    "content": "string"
  }
}
```

Chức năng:

- Tạo file mới
- Ghi đè file cũ
- Tự tạo thư mục nếu chưa tồn tại

---

### 3.2 `read_file`

```json
{
  "action": "read_file",
  "params": {
    "path": "string"
  }
}
```

Chức năng:

- Đọc nội dung file
- Trả về nội dung dạng string

---

### 3.3 `execute_command`

```json
{
  "action": "execute_command",
  "params": {
    "command": "string",
    "timeout_seconds": 60
  }
}
```

Chức năng:

- Thực thi shell command
- Trả về `stdout`, `stderr`, `exit_code`

Đây là action mạnh nhất. Từ đây có thể:

- Tạo process
- Cài package
- Start / stop node
- Build container
- Chạy Docker
- Khởi động HTTP server

---

## 4. Request Envelope

Mọi request tới `/action` phải có cấu trúc chuẩn:

```json
{
  "target_node_id": "node-0",
  "task_id": "task-001",
  "trace": {
    "hop_count": 0,
    "route_path": []
  },
  "payload": {
    "action": "execute_command",
    "params": {
      "command": "mkdir -p node-1",
      "timeout_seconds": 10
    }
  }
}
```

### 4.1 Fields

| Field | Mô tả |
|---|---|
| `target_node_id` | Node đích cần xử lý |
| `task_id` | Workflow ID do Cloud tạo |
| `trace.hop_count` | Số lần proxy qua node trung gian |
| `trace.route_path` | Danh sách node đã đi qua |
| `payload.action` | Tên action cần thực thi |
| `payload.params` | Tham số đầu vào của action |

---

## 5. Action Execution Model

Tất cả action đều hỗ trợ 2 chế độ:

- **Sync** — nếu xử lý nhanh
- **Async** — nếu xử lý lâu

Quy tắc phân biệt: **Nếu response có `job_id` ⇒ async. Nếu không có `job_id` ⇒ sync.**

Không dùng flag `mode`.

### 5.1 Sync

Node xử lý ngay và trả:

```json
{
  "task_id": "task-001",
  "status": "completed",
  "output": {
    "exit_code": 0,
    "stdout": "Directory created",
    "stderr": ""
  }
}
```

Không có `job_id`.

---

### 5.2 Async

Node tạo background job và trả ngay:

```json
{
  "task_id": "task-001",
  "job_id": "node0-job-001",
  "status": "accepted",
  "estimated_completion_seconds": 30
}
```

Nếu có `job_id` ⇒ async.

---

## 6. Task ID và Job ID

### 6.1 Task ID (Global Workflow ID)

- Sinh bởi Cloud
- Đại diện toàn bộ workflow logic
- Không thay đổi trong suốt quá trình
- Có thể span nhiều node

Ví dụ:

```
task-20260301-0001
```

---

### 6.2 Job ID (Node Execution ID)

- Sinh bởi Node
- Đại diện cho một execution cụ thể tại node đó
- Chỉ xuất hiện khi xử lý async
- Scoped tại node sinh ra nó

Ví dụ:

```
node0-job-00023
```

---

### 6.3 Quan hệ

Một task có thể tạo nhiều job:

```
Task T1
 ├── Job J1 (node-0)
 ├── Job J2 (node-1)
 └── Job J3 (node-2)
```

- **Task** = logical orchestration
- **Job** = physical execution instance

---

## 7. Result Polling

Cloud gọi để kiểm tra trạng thái job:

```
GET /result/{job_id}
```

### 7.1 Running

```json
{
  "task_id": "task-001",
  "job_id": "node0-job-001",
  "status": "running",
  "progress": 45
}
```

---

### 7.2 Completed

```json
{
  "task_id": "task-001",
  "job_id": "node0-job-001",
  "status": "completed",
  "output": {
    "exit_code": 0,
    "stdout": "...",
    "stderr": ""
  }
}
```

---

### 7.3 Failed

```json
{
  "task_id": "task-001",
  "job_id": "node0-job-001",
  "status": "failed",
  "error": "timeout after 60s"
}
```

---

## 8. Routing Logic

### 8.1 Xử lý request

Khi nhận request, node thực hiện:

```
if target_node_id == self.node_id:
    execute_action()
else:
    address = resolve(target_node_id)
    validate hop_count
    append self.node_id vào route_path
    increment hop_count
    proxy request
```

---

### 8.2 Loop Protection

Reject ngay nếu:

- `hop_count > max_hop`
- `self.node_id` đã có trong `route_path`

---

## 9. Resolve Mechanism

### 9.1 Resolve Algorithm

```
if node_id == self.node_id:
    return LOCAL

if node_id in config.nodes:
    return config.nodes[node_id]

if default_resolver exists:
    call default_resolver/resolve/node_id
    cache result (theo cache_ttl_seconds)
    return result

else:
    throw NODE_NOT_FOUND
```

---

### 9.2 GET /resolve/{node_id}

Cloud hoặc node khác có thể query để tìm địa chỉ của một node bất kỳ.

Request:

```
GET /resolve/node-3
```

Response:

```json
{
  "node_id": "node-3",
  "address": "http://10.0.0.3:8080"
}
```

Nếu không tìm thấy:

```json
{
  "error": "NODE_NOT_FOUND",
  "node_id": "node-3"
}
```

---

## 10. Node Configuration (node.yaml)

Mỗi node — bao gồm Seed Node — đều có file `node.yaml`:

```yaml
node_id: node-0
listen: 0.0.0.0:8080

nodes:
  node-0: http://10.0.0.0:8080
  node-1: http://10.0.0.1:8080

default_resolver: node-0

max_hop: 10
cache_ttl_seconds: 300
```

---

## 11. Skills System

Mỗi node có file `skills.md` — chỉ là Markdown tự do.

Seed Node mẫu:

```markdown
# Node: node-0 (Seed Node)

## Capabilities

### Action: write_file
Description:
Write content to a file. Creates parent directories if needed.

Input:
- path (string): absolute or relative file path
- content (string): content to write

Output:
- success (boolean)

---

### Action: read_file
Description:
Read content from a file.

Input:
- path (string): file path to read

Output:
- content (string)

---

### Action: execute_command
Description:
Execute a shell command and return the result.

Input:
- command (string): shell command to run
- timeout_seconds (integer): max execution time

Output:
- exit_code (integer)
- stdout (string)
- stderr (string)
```

Nguyên tắc:

- Node **không** parse skill.
- Cloud đọc skill để hiểu capability.
- Developer chịu trách nhiệm đồng bộ `skills.md` ↔ action thực tế.

---

### 11.1 GET /skills

Node trả về raw Markdown:

```
GET /skills
```

Response:

```
Content-Type: text/markdown

# Node: node-0
...
```

Cloud sẽ:

1. Fetch `/skills`
2. Cache nội dung
3. Inject vào prompt LLM khi cần reasoning

---

## 12. Self-Expansion Mechanism

Từ 3 action cơ bản, LLM có thể tự khởi tạo node mới mà không cần can thiệp thủ công.

### Quy trình tạo Node mới

**Step 1 — Tạo thư mục node:**

```json
{
  "action": "execute_command",
  "params": { "command": "mkdir -p node-1/actions node-1/runtime" }
}
```

**Step 2 — Tạo cấu trúc file:**

```json
{ "action": "write_file", "params": { "path": "node-1/node.yaml", "content": "..." } }
{ "action": "write_file", "params": { "path": "node-1/skills.md", "content": "..." } }
{ "action": "write_file", "params": { "path": "node-1/actions/some_action.py", "content": "..." } }
```

**Step 3 — Cài dependency:**

```json
{
  "action": "execute_command",
  "params": { "command": "pip install <package>" }
}
```

Hoặc build container nếu cần isolation.

**Step 4 — Start Node:**

```json
{
  "action": "execute_command",
  "params": { "command": "python node_runtime.py --config node-1/node.yaml &" }
}
```

Node mới bắt đầu lắng nghe HTTP.

**Step 5 — Đăng ký node mới:**

Có 2 cách:

- Cloud gọi `write_file` để cập nhật `node.yaml` của node-0, thêm node mới vào danh sách `nodes`.
- Node mới tự gọi `default_resolver` để đăng ký địa chỉ.

---

## 13. Non-Seed Node Runtime

Sau khi được khởi tạo, các node mới hoạt động theo Plugin-Based Model (tương tự v4.1).

### Cấu trúc thư mục:

```
node-X/
 ├── node.yaml
 ├── skills.md
 └── actions/
       ├── some_action.py
       └── another_action.py
```

### Plugin Contract

Mỗi file Python trong `actions/` phải implement:

```python
def run(params: dict, context: dict) -> dict:
    ...
```

Optional — khai báo async:

```python
ASYNC = True
```

### context object

Node runtime inject vào mỗi action call:

```json
{
  "task_id": "...",
  "job_id": "... hoặc null",
  "node_id": "...",
  "config": {},
  "logger": "...",
  "storage": "...",
  "temp_dir": "..."
}
```

### Action Loader

Node khi startup:

1. Scan folder `actions/`
2. Import tất cả file `.py`
3. Build registry:

```python
action_registry = {
    "some_action": module,
    "another_action": module
}
```

---

## 14. Job Manager

Node maintain trạng thái job trong memory:

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

Trạng thái hợp lệ:

| Trạng thái | Ý nghĩa |
|---|---|
| `accepted` | Job đã được nhận, chờ xử lý |
| `running` | Đang thực thi |
| `completed` | Hoàn tất thành công |
| `failed` | Thất bại |

---

## 15. Cloud Control Loop

1. Cloud sinh `task_id`.
2. Fetch `/skills` từ node-0, cache nội dung.
3. LLM reasoning → quyết định action + params.
4. Gửi `/action`.
5. Nếu response có `job_id`:
   - Sleep ~70% `estimated_completion_seconds`
   - Poll `GET /result/{job_id}`
6. Khi `status = completed`:
   - Continue reasoning
   - Có thể sinh action tiếp theo (tạo node, cài package, start service...)
7. Workflow kết thúc khi không còn job pending.

Cloud maintain:

```
task_id → {
    reasoning_context,
    job_list
}
```

---

## 16. Bootstrap Evolution Model

Hệ thống tiến hóa theo tầng:

| Level | Mô tả |
|---|---|
| **Level 0 — Seed Only** | Chỉ node-0. Đủ để bắt đầu. |
| **Level 1 — Functional Nodes** | LLM tạo thêm: media node, scraping node, ML node |
| **Level 2 — Specialized Clusters** | LLM tạo: GPU node, CPU-heavy node, storage node |
| **Level 3 — Self-Optimization** | LLM refactor action, replace node yếu, scale khi quá tải |

---

## 17. Digital Factory Use-Case (YouTube Automation)

Ví dụ kiến trúc được bootstrap từ Seed:

| Node | Vai trò |
|---|---|
| Seed Node (node-0) | Bootstrap engine, gateway |
| Trend Node | Web scraping + trending |
| Content Node | Script generation |
| Voice Node | TTS (ElevenLabs...) |
| Image Node | Image generation |
| Video Node | Assemble video (FFmpeg...) |
| Publishing Node | Upload YouTube, Facebook... |
| Storage Node | Artifact storage |

---

## 18. Bảo mật

Vì Seed Node có `execute_command`, đây là action có mức rủi ro cao nhất.

**Production bắt buộc:**

- Chạy Seed Node trong container (Docker / sandbox)
- Non-root user
- Resource limit (CPU, RAM)
- Timeout execution
- Disk quota
- Network policy (giới hạn egress nếu cần)
- Secret isolation (không expose credential qua action)
- Validate action name (whitelist)
- Log đầy đủ mọi action call

> ⚠️ Nếu không có guardrail, LLM có khả năng thực thi command tùy ý trên host.

---

## 19. Định nghĩa kiến trúc chính thức

v5.0 là:

> Single Seed Node + LLM Brain
> có khả năng tự sinh ra distributed execution mesh
> bằng cách tự viết code và tự khởi tạo runtime.

Đặc điểm:

| Thành phần | Vai trò |
|---|---|
| LLM | Brain — reasoning, orchestration |
| Seed Node | Minimal kernel — bootstrap mọi thứ |
| Non-Seed Node | Plugin runtime — extensible vô hạn |
| Action | Python module |
| Skill | Markdown discovery |
| Async | job_id-based |
| Routing | Mesh-based, hop-protected |

---

## 20. Trạng thái hiện tại

**Đã có:**

- ✅ Bootstrap minimal (3 action cơ bản)
- ✅ Self-expansion logic rõ ràng
- ✅ Plugin-based node runtime
- ✅ Async job model (job_id-based)
- ✅ Distributed routing (mesh + loop protection)
- ✅ Skill discovery (Markdown)
- ✅ Resolve mechanism (`/resolve/{node_id}`)

**Chưa có:**

- ❌ Structured schema validation
- ❌ Permission layer chi tiết per-action
- ❌ Version control tích hợp
- ❌ Auto rollback
- ❌ Resource governance layer hoàn chỉnh

---

## 21. Hướng phát triển v5.1

Có thể bổ sung:

- Action metadata header
- Idempotency key
- Retry policy
- Artifact registry
- Structured skill schema
- Permission layer per-action
- Auto rollback khi bootstrap thất bại