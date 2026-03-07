# Worklog — Execution Mesh v5.7 (Correctness & Observability)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-04
**Base version:** v5.6 → **v5.7**

---

## 1. Tổng quan

v5.7 giải quyết 4 items tồn đọng từ v5.3, tất cả là small-scoped, high-value:

| # | Item | Priority |
|---|------|---------|
| A | Lazy staleness check (thay background loop) | P2 |
| B | Pull job timeout (lazy check) | P3 |
| C | Migrate `on_event` → `lifespan` (FastAPI) | P3 |
| D | Queue depth trong `/health` | P3 |

---

## 2. Chi tiết từng thay đổi

---

### A. Lazy Staleness Check (`runtime/node_registry.py`)

#### Vấn đề gốc

`NodeRegistry.mark_stale_nodes_unreachable()` tồn tại từ v5.3 nhưng không ai
gọi nó. Nếu worker crash mà không gửi heartbeat cuối:

```
Worker crash
  ↓
Gateway vẫn thấy status = ONLINE (last_heartbeat quá cũ nhưng chưa ai check)
  ↓
Gateway tiếp tục cố PUSH → fail
  ↓
Fallback sang PULL → enqueue
  ↓
Queue tích lũy jobs mà không worker nào còn sống để nhận
```

#### Alternative đã cân nhắc

**Option A — Background loop (approach gốc):**
```python
async def _stale_checker_loop():
    while True:
        await asyncio.sleep(heartbeat_timeout / 2)
        await registry.mark_stale_nodes_unreachable()
```
Nhược điểm: thêm asyncio task, thêm concurrency complexity, khó test,
interval không đồng bộ với lúc thực sự cần biết node status.

**Option B — Lazy check (được chọn):**
Kiểm tra staleness ngay tại điểm sử dụng thông tin node — `get_address()`,
`get_status()`, `ping()`. Khi gateway chuẩn bị routing decision, nó sẽ tự
phát hiện node stale ngay trước khi quyết định push/pull.

```
Gateway gọi get_address(node-1)
  → _apply_lazy_staleness(entry)
     → last_heartbeat age > timeout? → mark UNREACHABLE
  → return address (nhưng status đã cập nhật)
  → GatewayRouter thấy UNREACHABLE → ping() → also stale → pull mode
```

Ưu điểm: zero background task, deterministic, dễ test, không có race condition
giữa checker loop và router decision.

Thêm: `ping()` giờ skip network probe nếu node đã được lazy-check là stale
— tránh unnecessary latency trên hot-path routing.

#### Implementation

3 methods được cập nhật: `get_address()`, `get_status()`, `ping()`.

Helper mới:
```python
def _is_heartbeat_stale(self, entry: _NodeEntry) -> bool:
    if entry.status != NodeStatus.ONLINE:
        return False        # chỉ check ONLINE nodes
    if entry.last_heartbeat is None:
        return False        # never sent heartbeat → not stale
    return (time.time() - entry.last_heartbeat) > self._heartbeat_timeout

def _apply_lazy_staleness(self, entry: _NodeEntry) -> None:
    if self._is_heartbeat_stale(entry):
        entry.status = NodeStatus.UNREACHABLE  # in-place, under lock
```

`mark_stale_nodes_unreachable()` được giữ lại cho explicit sweep nếu cần,
nhưng không còn cần thiết cho correctness.

---

### B. Pull Job Timeout (`runtime/job_queue.py`, `runtime/gateway_router.py`, `runtime/config.py`)

#### Vấn đề gốc

Khi gateway enqueue một PULL job và worker offline vĩnh viễn, job ngồi trong
queue mãi mãi ở trạng thái `queued`. Claude poll `GET /result/{job_id}` nhận
`{"status": "queued"}` mãi mà không bao giờ nhận được kết quả cuối cùng.

#### Alternative đã cân nhắc

**Option A — Background timeout sweeper:**
Background task quét queue định kỳ, mark expired jobs là FAILED.
Nhược điểm: cùng vấn đề như background stale-checker — thêm complexity.

**Option B — Lazy timeout check tại `GET /result` (được chọn):**
Khi `route_result()` được gọi và job vẫn là QUEUED, check xem
`time.time() - created_at > pull_job_timeout_seconds` không. Nếu có → mark
FAILED ngay, trả kết quả cho Claude.

```
Claude poll GET /result/job-xyz
  → route_result(job_id)
     → job.status == QUEUED?
        → get_queued_job(job_id) → check age
           → age > timeout → update_job(FAILED, error="timed out after Xs")
     → return JobStatusResponse(status="failed", error="...")
```

Claude nhận được `failed` rõ ràng thay vì `queued` mãi mãi.

#### Config mới

```yaml
pull_job_timeout_seconds: 300    # default 5 phút
```

#### Model thay đổi

`QueuedJob` thêm field:
```python
timeout_seconds: int = 300
```

Được set khi enqueue, sử dụng khi check.

---

### C. Migrate `on_event` → `lifespan` (`runtime/server.py`)

#### Vấn đề gốc

FastAPI deprecated `@app.on_event("startup")` từ v0.93. Mọi test run đều sinh
`DeprecationWarning`. Sẽ bị remove trong phiên bản tương lai.

#### Alternative

Không có alternative — đây là migration bắt buộc. FastAPI docs chỉ định rõ
pattern thay thế:

```python
# Trước (deprecated)
@app.on_event("startup")
async def on_startup():
    job_manager.start_cleanup_loop()

@app.on_event("shutdown")
async def on_shutdown():
    job_manager.stop_cleanup_loop()
```

```python
# Sau (v5.7)
@asynccontextmanager
async def lifespan(app: FastAPI):
    job_manager.start_cleanup_loop()
    logger.info("v5.7 startup ...")
    yield                           # app runs here
    job_manager.stop_cleanup_loop()
    logger.info("Shutdown complete")

app = FastAPI(lifespan=lifespan, ...)
```

Lợi ích: startup và shutdown logic nằm cùng một chỗ, dễ đọc hơn, không còn
DeprecationWarning trong test output.

---

### D. Queue Depth trong `/health` (`runtime/models.py`, `runtime/server.py`)

#### Vấn đề gốc

`GET /health` chỉ trả `jobs_active` (local JobManager jobs). Không có thông
tin về pull-queue. Operator và Claude không biết mesh có bị tắc nghẽn không —
ví dụ node-worker offline và 50 jobs đang chờ.

#### Alternative

Không có alternative hay hơn. Chỉ cần thêm field. Effort thấp, giá trị cao.

#### Implementation

`HealthResponse` thêm field:
```python
queue_depths: dict[str, int] = Field(default_factory=dict)
# e.g. {"node-1": 3, "node-2": 0}
```

`JobQueue.all_queue_depths()` đã có sẵn từ v5.3 — chỉ cần gọi trong health endpoint.

```json
// GET /health response (v5.7)
{
  "node_id": "node-0",
  "status": "healthy",
  "uptime_seconds": 3600.5,
  "actions_loaded": 5,
  "jobs_active": 2,
  "queue_depths": {
    "node-1": 3,
    "node-2": 0
  }
}
```

---

## 3. Files thay đổi

| File | Thay đổi |
|------|---------|
| `runtime/node_registry.py` | Thêm `_is_heartbeat_stale()`, `_apply_lazy_staleness()`; cập nhật `get_address()`, `get_status()`, `ping()` |
| `runtime/config.py` | Thêm `DEFAULT_PULL_JOB_TIMEOUT_SECONDS = 300`; thêm field `pull_job_timeout_seconds` vào `NodeConfig`; cập nhật `load_config()` |
| `runtime/models.py` | `QueuedJob` thêm `timeout_seconds: int = 300`; `HealthResponse` thêm `queue_depths: dict[str, int]` |
| `runtime/job_queue.py` | `enqueue()` nhận `timeout_seconds`; thêm `get_queued_job()` |
| `runtime/gateway_router.py` | `route_result()` thêm lazy timeout check; `_pull()` pass `pull_job_timeout_seconds`; thêm `import time` |
| `runtime/server.py` | `on_event` → `lifespan`; health endpoint thêm `queue_depths`; version 5.7.0 |
| `tests/test_mesh_ctl.py` | Load từ `mesh_ctl.py.deprecated` qua `exec()` thay vì import trực tiếp |
| `tests/test_v57_features.py` | **Mới** — 11 tests |
| `docs/WORKLOG_V5.7.md` | **Mới** — document này |
| `docs/SPECS_V5.7.md` | **Mới** — spec chính thức v5.7 |

---

## 4. Test Suite

### Mới (v5.7)

| Class | Tests | Coverage |
|-------|-------|---------|
| `TestLazyStaleness` | 6 | get_address stale, get_status stale, fresh node unaffected, ping skips probe, no-heartbeat node not stale, already-unreachable not re-processed |
| `TestPullJobTimeout` | 2 | Job within timeout → queued; job past timeout → failed with message |
| `TestLifespanMigration` | 1 | No DeprecationWarning from on_event on ASGI startup |
| `TestQueueDepthInHealth` | 2 | queue_depths field present; reflects actual queue count |
| **Tổng mới** | **11** | |

### Tổng kết

| Version | Tests | Pass |
|---------|-------|------|
| v5.6 | 83 | 83 |
| **v5.7** | **94** | **94** |

---

## 5. Remaining Items (cập nhật)

| Priority | Item | Status |
|----------|------|--------|
| P2 | ~~Background task `mark_stale_nodes_unreachable()`~~ | ✅ Done (lazy check) |
| P3 | ~~Pull job timeout~~ | ✅ Done (lazy check) |
| P3 | ~~Migrate `on_event` → `lifespan`~~ | ✅ Done |
| P3 | ~~Queue depth trong `/health`~~ | ✅ Done |
| P2 | Container isolation per-node (Docker / non-root) | Deferred |
| P3 | Job queue persistence (SQLite/Redis) | Deferred — Claude retry pattern |
| P4 | Idempotency key | Deferred — Claude tự handle |

---

*Document: WORKLOG_V5.7.md | Mesh Runtime v5.7 | Repository: ai-infra-runtime-v2*
