# AI-Orchestrated Self-Bootstrapping Execution Mesh

## Architecture Specification v5.7

### (Correctness & Observability)

---

## 1. Thay đổi so với v5.6

| Thành phần | v5.6 | v5.7 |
|-----------|------|------|
| Stale node detection | Không có (bug) | Lazy check tại `get_address`, `get_status`, `ping` |
| Pull job timeout | Không có (job queued mãi) | Lazy check tại `GET /result` |
| FastAPI lifecycle | `on_event` (deprecated) | `lifespan` context manager |
| `GET /health` | `jobs_active` only | `jobs_active` + `queue_depths` per node |

---

## 2. Lazy Evaluation Pattern

v5.7 áp dụng **lazy evaluation** nhất quán cho 2 correctness problems:

> Thay vì dùng background task kiểm tra định kỳ, kiểm tra trạng thái
> ngay tại thời điểm thông tin được yêu cầu.

### 2.1 Node Staleness

```
Khi nào check: get_address(), get_status(), ping()
Điều kiện stale: status==ONLINE AND last_heartbeat không null AND age > timeout
Hành động: entry.status = UNREACHABLE (in-place, under asyncio.Lock)
```

Ưu điểm so với background loop:
- Zero background task, zero concurrency overhead
- Deterministic — trạng thái cập nhật đúng trước routing decision
- Dễ test — không cần mock timer hay sleep

### 2.2 Pull Job Timeout

```
Khi nào check: GET /result/{job_id} khi job.status == QUEUED
Điều kiện timeout: time.time() - queued_job.created_at > pull_job_timeout_seconds
Hành động: update_job(FAILED, error="Pull job timed out after Xs")
```

Claude nhận `{"status": "failed", "error": "Pull job timed out ..."}` thay vì
`{"status": "queued"}` mãi mãi.

---

## 3. Node Staleness — Algorithm

```python
def _is_heartbeat_stale(entry):
    if entry.status != ONLINE:  return False   # chỉ check ONLINE
    if entry.last_heartbeat is None: return False  # never sent → unknown
    return (now - entry.last_heartbeat) > heartbeat_timeout_seconds

# Tích hợp vào mọi routing-relevant access:
get_address(node_id):
    entry = _entries[node_id]
    _apply_lazy_staleness(entry)   # ← stale check
    return entry.address

get_status(node_id):
    entry = _entries[node_id]
    _apply_lazy_staleness(entry)   # ← stale check
    return entry.status

ping(node_id):
    entry = _entries[node_id]
    _apply_lazy_staleness(entry)   # ← stale check
    if entry.status == UNREACHABLE:
        return False               # skip network probe
    # else → proceed with HTTP /ping
```

---

## 4. Pull Job Timeout — Config

```yaml
# node.yaml — gateway
pull_job_timeout_seconds: 300    # default 5 phút
```

Flow:

```
Claude POST /action → node-1 (offline) → PULL → job_id returned
[worker offline indefinitely]
Claude GET /result/job-xyz (after 300s)
  → route_result: job QUEUED + age > 300s → mark FAILED
  → {"status": "failed", "error": "Pull job timed out after 305s — no worker claimed it"}
Claude nhận kết quả → có thể retry hoặc report lỗi
```

---

## 5. GET /health — Updated Response (v5.7)

```json
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

`queue_depths`: số unclaimed jobs đang chờ trong pull-queue per worker node.

Dùng để:
- Claude phát hiện bottleneck (node-1 có 10 jobs pending → worker có vấn đề)
- Operator monitor mesh health
- Tự động trigger bootstrap node mới khi queue quá sâu

---

## 6. FastAPI Lifespan (v5.7)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    job_manager.start_cleanup_loop()
    yield
    # shutdown
    job_manager.stop_cleanup_loop()

app = FastAPI(lifespan=lifespan, ...)
```

Thay thế hoàn toàn `@app.on_event("startup/shutdown")` đã deprecated.

---

## 7. Trạng thái hiện tại (v5.7)

**Đã có:**

- ✅ Bootstrap minimal (3 seed actions)
- ✅ Plugin-based node runtime
- ✅ Async job model (job_id-based)
- ✅ Distributed routing (mesh + loop protection)
- ✅ Skill discovery (Markdown)
- ✅ Authentication layer (Bearer token)
- ✅ Action schema validation
- ✅ Job TTL & cleanup
- ✅ Auto rollback (bootstrap)
- ✅ One-liner setup.sh
- ✅ Push/Pull delivery (NAT support)
- ✅ NodeRegistry + WorkerAgent + JobQueue
- ✅ Optional task_id + trace (v5.6)
- ✅ Claude Web curl-native workflow (v5.6)
- ✅ **Lazy node staleness check (v5.7)**
- ✅ **Pull job timeout — lazy check (v5.7)**
- ✅ **FastAPI lifespan migration (v5.7)**
- ✅ **Queue depth in /health (v5.7)**

**Chưa có:**

- ❌ Container isolation per-node (non-root user + ulimit minimum)
- ❌ Job queue persistence (SQLite — nếu cần)
- ❌ Idempotency key (Claude tự handle ở tầng logic)

---

*Spec: SPECS_V5.7.md | Mesh Runtime v5.7 | Repository: ai-infra-runtime-v2*
