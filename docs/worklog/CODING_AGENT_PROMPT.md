# Coding Agent Prompt — AI-Orchestrated Self-Bootstrapping Execution Mesh v5.0

---

## Nhiệm vụ

Bạn là một senior software engineer. Hãy generate toàn bộ codebase cho hệ thống **AI-Orchestrated Self-Bootstrapping Execution Mesh v5.0** dựa trên specification đính kèm bên dưới.

Yêu cầu chất lượng: production-ready, clean architecture, fully functional.

---

## Coding Standards (Bắt buộc)

- **Ngôn ngữ:** Python 3.11+
- **Indentation:** 4 spaces, tuyệt đối không dùng tab
- **Type hints:** bắt buộc cho tất cả function signature và class attribute
- **Docstring:** bắt buộc cho tất cả module, class, và public function (Google style)
- **Error handling:** tất cả exception phải được catch và log — không để exception unhandled
- **Logging:** dùng Python standard `logging`, không dùng `print()`
- **Naming convention:** `snake_case` cho variable/function, `PascalCase` cho class, `UPPER_SNAKE_CASE` cho constant
- **Không có magic string/number** — tất cả constant phải được define rõ ràng

---

## Cấu trúc project yêu cầu

```
mesh/
├── README.md
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
│
├── node_runtime.py              # Entry point: khởi động một node
│
├── runtime/
│   ├── __init__.py
│   ├── config.py                # Load và validate node.yaml
│   ├── server.py                # HTTP server (FastAPI)
│   ├── router.py                # Routing + proxy logic
│   ├── resolver.py              # Resolve node_id → address (với cache)
│   ├── action_loader.py         # Scan + import actions/*.py
│   ├── action_executor.py       # Thực thi action (sync/async detection)
│   └── job_manager.py           # In-memory job state management
│
├── seed/
│   ├── __init__.py
│   └── actions/
│       ├── write_file.py        # Seed action: write_file
│       ├── read_file.py         # Seed action: read_file
│       └── execute_command.py   # Seed action: execute_command
│
├── node-0/                      # Seed Node config (example)
│   ├── node.yaml
│   └── skills.md
│
└── tests/
    ├── __init__.py
    ├── test_router.py
    ├── test_resolver.py
    ├── test_job_manager.py
    ├── test_action_loader.py
    └── test_actions.py
```

---

## Yêu cầu chi tiết từng component

### 1. `runtime/config.py`

- Load `node.yaml` bằng `PyYAML`
- Parse và validate các field bắt buộc: `node_id`, `listen`, `nodes`, `default_resolver`, `max_hop`, `cache_ttl_seconds`
- Expose typed `NodeConfig` dataclass
- Raise lỗi rõ ràng nếu config thiếu field

### 2. `runtime/server.py`

- Dùng **FastAPI** + **uvicorn**
- Expose 4 endpoints:
  - `POST /action` — nhận request envelope, gọi router
  - `GET /result/{job_id}` — query job state từ job_manager
  - `GET /resolve/{node_id}` — query resolver
  - `GET /skills` — đọc và trả về raw content của `skills.md` với `Content-Type: text/markdown`
- Tất cả request/response đều dùng Pydantic model
- Middleware: log mọi incoming request (method, path, task_id nếu có)

### 3. `runtime/router.py`

- Implement routing logic đúng theo spec:
  - Nếu `target_node_id == self.node_id` → thực thi local
  - Ngược lại → resolve địa chỉ → proxy request (dùng `httpx` async)
- Loop protection:
  - Reject nếu `hop_count > max_hop`
  - Reject nếu `self.node_id` đã có trong `route_path`
- Khi proxy: tăng `hop_count`, append `self.node_id` vào `route_path`

### 4. `runtime/resolver.py`

- Implement resolve algorithm theo spec (local → config.nodes → default_resolver → NOT_FOUND)
- Cache kết quả resolve với TTL (`cache_ttl_seconds` từ config)
- Thread-safe cache (dùng `asyncio.Lock`)

### 5. `runtime/action_loader.py`

- Scan thư mục `actions/` của node
- Import từng `.py` file dưới dạng module
- Build `action_registry: dict[str, ModuleType]`
- Validate mỗi module phải có function `run(params: dict, context: dict) -> dict`
- Log warning nếu module thiếu `run`

### 6. `runtime/action_executor.py`

- Nhận action name + params + context
- Lookup trong `action_registry`
- Phát hiện async action: kiểm tra `ASYNC = True` hoặc `run_async` function
- Nếu sync: gọi `run()` trực tiếp, trả kết quả ngay
- Nếu async: tạo `job_id`, spawn background task (`asyncio.create_task`), trả `job_id` + `status: accepted`
- Build `context` object inject vào mỗi action:
  ```python
  context = {
      "task_id": ...,
      "job_id": ...,      # None nếu sync
      "node_id": ...,
      "config": ...,
      "logger": logging.getLogger(action_name),
      "temp_dir": "/tmp/mesh/{task_id}"
  }
  ```

### 7. `runtime/job_manager.py`

- In-memory store: `dict[str, JobState]`
- `JobState` là dataclass với: `job_id`, `task_id`, `status`, `progress`, `start_time`, `estimated_completion_seconds`, `output`, `error`
- Status enum: `accepted`, `running`, `completed`, `failed`
- Methods: `create_job()`, `update_job()`, `get_job()`, `job_id_generator(node_id)`
  - `job_id` format: `{node_id}-job-{uuid4_short}`
- Thread-safe với `asyncio.Lock`

### 8. Seed Actions (`seed/actions/`)

#### `write_file.py`
- Nhận `path` (string) và `content` (string)
- Tạo thư mục cha nếu chưa tồn tại (`mkdir -p` behavior)
- Ghi đè nếu file đã tồn tại
- Trả `{"success": true, "path": "..."}`
- KHÔNG async (sync)

#### `read_file.py`
- Nhận `path` (string)
- Raise error rõ ràng nếu file không tồn tại
- Trả `{"content": "...", "size_bytes": ...}`
- KHÔNG async (sync)

#### `execute_command.py`
- Nhận `command` (string) và `timeout_seconds` (int, default 60)
- Dùng `asyncio.create_subprocess_shell`
- **ASYNC = True** — luôn chạy async
- Trả `{"exit_code": ..., "stdout": "...", "stderr": "..."}`
- Enforce timeout, trả lỗi timeout rõ ràng nếu vượt quá

### 9. `node_runtime.py` (Entry point)

- Nhận CLI argument: `--config` (path đến `node.yaml`)
- Load config
- Load action registry
  - Nếu là Seed Node (node_id == "node-0" hoặc có flag): load từ `seed/actions/`
  - Nếu là non-seed node: load từ `{node_dir}/actions/`
- Khởi động FastAPI server với uvicorn
- Log rõ ràng khi startup: node_id, listen address, số action đã load

### 10. Pydantic Models (define trong `runtime/server.py` hoặc `runtime/models.py`)

```python
class TraceInfo(BaseModel):
    hop_count: int = 0
    route_path: list[str] = []

class ActionPayload(BaseModel):
    action: str
    params: dict[str, Any] = {}

class ActionRequest(BaseModel):
    target_node_id: str
    task_id: str
    trace: TraceInfo
    payload: ActionPayload

class SyncActionResponse(BaseModel):
    task_id: str
    status: Literal["completed"]
    output: dict[str, Any]

class AsyncActionResponse(BaseModel):
    task_id: str
    job_id: str
    status: Literal["accepted"]
    estimated_completion_seconds: int

class JobStatusResponse(BaseModel):
    task_id: str
    job_id: str
    status: str
    progress: int | None = None
    output: dict[str, Any] | None = None
    error: str | None = None

class ResolveResponse(BaseModel):
    node_id: str
    address: str

class ErrorResponse(BaseModel):
    error: str
    node_id: str | None = None
```

---

## Config files mẫu

### `node-0/node.yaml`
```yaml
node_id: node-0
listen: 0.0.0.0:8080

nodes:
  node-0: http://127.0.0.1:8080

default_resolver: node-0

max_hop: 10
cache_ttl_seconds: 300
```

### `node-0/skills.md`
Viết skills.md đầy đủ cho 3 action của Seed Node (write_file, read_file, execute_command).

---

## `requirements.txt`

Bao gồm (với version cụ thể):
- `fastapi`
- `uvicorn[standard]`
- `httpx`
- `pyyaml`
- `pydantic`

---

## `Dockerfile`

- Base image: `python:3.11-slim`
- Non-root user
- Copy requirements + install trước, copy source sau (tận dụng Docker layer cache)
- `ENTRYPOINT ["python", "node_runtime.py"]`
- `CMD ["--config", "node-0/node.yaml"]`

---

## `docker-compose.yml`

Định nghĩa service `node-0` với:
- Build từ Dockerfile
- Mount `node-0/` vào container
- Port mapping `8080:8080`

---

## Tests (`tests/`)

Viết unit test với `pytest` + `pytest-asyncio`:

- `test_router.py`: test local execution, test proxy routing, test loop protection (hop_count > max_hop, node đã trong route_path)
- `test_resolver.py`: test resolve local, resolve từ config, resolve qua default_resolver, NODE_NOT_FOUND
- `test_job_manager.py`: test create/update/get job, test job_id format
- `test_action_loader.py`: test scan + import, test module thiếu `run` bị bỏ qua với warning
- `test_actions.py`: test từng seed action (write_file, read_file, execute_command) với mock filesystem

---

## Lưu ý quan trọng

1. **Không bỏ bất kỳ component nào** — generate đầy đủ tất cả file kể trên.
2. **Mỗi file phải có module-level docstring** giải thích mục đích.
3. **Không dùng global mutable state** ngoại trừ `job_manager` và `action_registry` được init một lần khi startup.
4. **Async-first**: tất cả HTTP handler phải là `async def`. Blocking IO phải được wrap bằng `asyncio.to_thread()`.
5. **`execute_command`** là action duy nhất có `ASYNC = True`. Các seed action còn lại là sync.
6. **README.md** phải bao gồm: mô tả hệ thống, cấu trúc thư mục, hướng dẫn chạy local, hướng dẫn chạy bằng Docker, ví dụ curl cho từng endpoint.

---

## Specification đầy đủ

{PASTE_SPECS_V5.0_MD_CONTENT_HERE}
