# Worklog — Execution Mesh v5.1 (Feature Upgrade)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-01
**Author:** Claude (AI Coding Agent)
**Base version:** v5.0 → **v5.1**

---

## 1. Tổng quan

Upgrade hệ thống từ v5.0 lên v5.1 với 6 features mới: Authentication Layer, Action Schema Validation, Integration Test Suite, Job TTL & Cleanup, Node Bootstrap Workflow, và Auto Rollback. Tổng cộng thêm 8 files mới, sửa 7 files hiện có, tăng test suite từ 35 lên 78 test cases — tất cả pass.

---

## 2. Features đã implement

### A. Authentication Layer

**Files:** `runtime/auth.py` (new), `runtime/config.py` (modified), `runtime/server.py` (modified), `node-0/node.yaml` (modified)

**Mô tả:**
- Bearer token authentication middleware cho FastAPI
- Token cấu hình trong `node.yaml` qua field `auth_token`
- Nếu không set `auth_token` → open mode (mọi request đều pass)
- Constant-time comparison (`secrets.compare_digest`) chống timing attacks
- Exempt paths mặc định: `/health`, `/skills`, `/docs`, `/openapi.json`

**Hành vi:**

| Trường hợp | HTTP Status | Response |
|-------------|-------------|----------|
| Không set auth_token | Mọi request pass | — |
| Không gửi token | 401 | `{"error": "UNAUTHORIZED"}` |
| Token sai | 403 | `{"error": "FORBIDDEN"}` |
| Token đúng | Pass through | — |
| Exempt path (health/skills) | Pass through | — |

**Config mẫu:**
```yaml
auth_token: mesh-secret-token-v51
```

**Verification:**
- ✅ `/health` truy cập được không cần token
- ✅ `POST /action` không có token → 401
- ✅ `POST /action` token sai → 403
- ✅ `POST /action` token đúng → 200

---

### B. Action Schema Validation

**Files:** `runtime/schema_validator.py` (new), `runtime/action_executor.py` (modified), `runtime/server.py` (modified), `seed/actions/*.schema.json` (3 new files)

**Mô tả:**
- JSON Schema (Draft 7) validation cho action params trước khi execution
- Schema files co-located với action modules: `{action_name}.schema.json`
- Actions không có schema → open validation (cho pass)
- Schema validation errors trả HTTP 422 với chi tiết lỗi
- Pre-validate schema khi load (reject schema files bị lỗi)

**Schema files đã tạo:**

| Action | Schema | Validation rules |
|--------|--------|-----------------|
| `write_file` | `write_file.schema.json` | `path` (string, required, minLength 1), `content` (string, required), no additionalProperties |
| `read_file` | `read_file.schema.json` | `path` (string, required, minLength 1), no additionalProperties |
| `execute_command` | `execute_command.schema.json` | `command` (string, required, minLength 1), `timeout_seconds` (int, optional, min 1, max 3600), no additionalProperties |

**Verification:**
- ✅ Thiếu required field (`content`) → 422 `SCHEMA_VALIDATION_ERROR: 'content' is a required property`
- ✅ Extra property (`extra`) → 422 `Additional properties are not allowed`
- ✅ Params hợp lệ → pass through bình thường

---

### C. Integration Test Suite

**Files:** `tests/test_integration.py` (new)

**Mô tả:**
- Integration tests dùng FastAPI ASGI transport (không cần port thật)
- Test full stack: auth middleware → routing → schema validation → action execution → response
- 10 test cases covering:

| Test | Mô tả |
|------|--------|
| `test_local_action_execution` | Sync action chạy local, verify output |
| `test_schema_validation_rejects_bad_params` | Invalid params → 422 |
| `test_node_not_found_returns_error` | Route tới node không tồn tại → 400 |
| `test_hop_count_protection` | hop_count > max_hop → 400 |
| `test_loop_detection` | self trong route_path → 400 |
| `test_auth_blocks_unauthenticated` | Không có token → 401 |
| `test_auth_allows_authenticated` | Token đúng → 200 |
| `test_health_and_skills_exempt_from_auth` | Exempt paths pass |
| `test_resolve_endpoint` | Resolve known/unknown nodes |
| `test_action_not_found_returns_error` | Action không tồn tại → 400 |

---

### D. Job TTL & Cleanup

**Files:** `runtime/job_manager.py` (modified), `runtime/config.py` (modified), `runtime/server.py` (modified)

**Mô tả:**
- Background asyncio task chạy theo `cleanup_interval_seconds` (mặc định 60s)
- Xóa jobs ở trạng thái terminal (`completed`/`failed`) quá `job_ttl_seconds` (mặc định 3600s)
- Jobs đang `running`/`accepted` không bao giờ bị xóa
- `completed_time` tracked tự động khi job chuyển sang terminal status
- Cleanup loop start/stop via FastAPI `on_event("startup")`/`on_event("shutdown")`
- Config trong `node.yaml`:

```yaml
job_ttl_seconds: 3600
cleanup_interval_seconds: 60
```

**Thay đổi so với v5.0:**

| Thành phần | v5.0 | v5.1 |
|------------|------|------|
| Jobs lưu trữ | Mãi mãi trong memory | TTL-based cleanup |
| `completed_time` | Không có | Auto-tracked |
| Background cleanup | Không có | asyncio task mỗi 60s |
| `total_count()` | Không có | Mới thêm |

---

### F. Node Bootstrap Workflow

**Files:** `runtime/bootstrap.py` (new), `runtime/server.py` (modified)

**Mô tả:**
- `BootstrapEngine` — orchestrator tạo node mới qua 8 bước tuần tự
- `BootstrapRequest` dataclass chứa toàn bộ spec cho node mới
- `POST /bootstrap` endpoint trên seed node
- Hỗ trợ: custom actions, schemas, skills, pip packages, auth token, extra_nodes

**8 bước bootstrap:**

| Step | Tên | Mô tả | Rollback |
|------|-----|--------|----------|
| 1 | `create_dirs` | Tạo `node-X/` và `node-X/actions/` | Xóa toàn bộ directory |
| 2 | `write_config` | Ghi `node.yaml` | (covered by dir removal) |
| 3 | `write_skills` | Ghi `skills.md` | (covered by dir removal) |
| 4 | `write_actions` | Ghi `.py` + `.schema.json` | (covered by dir removal) |
| 5 | `install_deps` | `pip install` packages | — |
| 6 | `start_node` | Start process background | Kill process (SIGTERM → SIGKILL) |
| 7 | `health_check` | GET /health (5 retries) | — |
| 8 | `register_node` | Thêm vào seed's `node.yaml` | Revert config file |

**Response mẫu (success):**
```json
{
    "node_id": "node-1",
    "status": "completed",
    "address": "http://127.0.0.1:8081",
    "pid": 12345,
    "steps_completed": ["create_dirs", "write_config", "write_skills", "write_actions", "start_node", "health_check", "register_node"],
    "steps_rolled_back": []
}
```

---

### G. Auto Rollback

**Files:** Tích hợp trong `runtime/bootstrap.py`

**Mô tả:**
- `_RollbackTracker` — tracks undo operations cho mỗi bước
- Khi bất kỳ step nào fail → tự động rollback tất cả steps đã hoàn thành theo thứ tự ngược
- Rollback resilient: nếu một undo thất bại, các undo khác vẫn tiếp tục
- Process kill escalation: SIGTERM → chờ 5s → SIGKILL
- Config file revert: khôi phục chính xác nodes dict ban đầu

**Ví dụ rollback flow:**

```
Step 1: create_dirs ✅
Step 2: write_config ✅
Step 3: write_skills ✅
Step 4: write_actions ✅
Step 5: start_node ✅
Step 6: health_check ❌ (failed after 5 retries)

→ Auto-rollback:
  Undo start_node: kill pid (SIGTERM → SIGKILL)
  Undo create_dirs: rm -rf node-X/

Result: {"status": "rolled_back", "steps_rolled_back": ["start_node", "create_dirs"]}
```

---

## 3. Tổng hợp thay đổi

### Files mới (8)

| File | LOC (approx) | Feature |
|------|-------------|---------|
| `runtime/auth.py` | 75 | A. Authentication |
| `runtime/schema_validator.py` | 115 | B. Schema Validation |
| `runtime/bootstrap.py` | 340 | F. Bootstrap + G. Rollback |
| `seed/actions/write_file.schema.json` | 15 | B. Schema |
| `seed/actions/read_file.schema.json` | 12 | B. Schema |
| `seed/actions/execute_command.schema.json` | 18 | B. Schema |
| `tests/test_auth.py` | 80 | A. Tests |
| `tests/test_schema_validator.py` | 110 | B. Tests |
| `tests/test_job_cleanup.py` | 90 | D. Tests |
| `tests/test_bootstrap.py` | 150 | F+G. Tests |
| `tests/test_integration.py` | 175 | C. Integration Tests |

### Files sửa đổi (7)

| File | Thay đổi chính |
|------|---------------|
| `runtime/config.py` | Thêm `auth_token`, `job_ttl_seconds`, `cleanup_interval_seconds` |
| `runtime/server.py` | Tích hợp auth middleware, schema validator, job cleanup lifecycle, `/bootstrap` endpoint |
| `runtime/action_executor.py` | Thêm `schema_validator` parameter, validate trước execute |
| `runtime/job_manager.py` | Thêm `completed_time`, `cleanup_expired()`, `start_cleanup_loop()`, `stop_cleanup_loop()`, `total_count()` |
| `node_runtime.py` | Load schemas, pass `schema_registry` + `config_path` to `create_app` |
| `node-0/node.yaml` | Thêm `auth_token`, `job_ttl_seconds`, `cleanup_interval_seconds` |
| `requirements.txt` | Thêm `jsonschema==4.23.0` |
| `tests/test_router.py` | Cập nhật `ActionExecutor` constructor call |

---

## 4. Test Results

| Test file | Tests | Trạng thái |
|-----------|-------|------------|
| `test_action_loader.py` | 5 | ✅ Pass |
| `test_actions.py` | 13 | ✅ Pass |
| `test_auth.py` | 6 | ✅ Pass (NEW) |
| `test_bootstrap.py` | 8 | ✅ Pass (NEW) |
| `test_integration.py` | 10 | ✅ Pass (NEW) |
| `test_job_cleanup.py` | 7 | ✅ Pass (NEW) |
| `test_job_manager.py` | 8 | ✅ Pass |
| `test_resolver.py` | 4 | ✅ Pass |
| `test_router.py` | 5 | ✅ Pass (updated) |
| `test_schema_validator.py` | 12 | ✅ Pass (NEW) |
| **Tổng** | **78** | **78/78 Pass** |

---

## 5. Live Verification (node-0 v5.1)

| Test | Input | Expected | Actual |
|------|-------|----------|--------|
| Health exempt | `GET /health` (no token) | 200 | ✅ 200 |
| No token | `POST /action` (no token) | 401 | ✅ 401 `UNAUTHORIZED` |
| Wrong token | `POST /action` (Bearer wrong) | 403 | ✅ 403 `FORBIDDEN` |
| Correct token | `POST /action` (Bearer mesh-secret-token-v51) | 200 | ✅ 200 completed |
| Missing field | `write_file` without `content` | 422 | ✅ 422 `'content' is a required property` |
| Extra property | `read_file` with `extra` field | 422 | ✅ 422 `Additional properties are not allowed` |

---

## 6. Những gì chưa hoàn thành (Remaining)

| # | Item | Priority | Ghi chú |
|---|------|----------|---------|
| 1 | **Cloud AI Planner** | P1 | ReAct reasoning loop, task lifecycle, skills caching |
| 2 | **Multi-node proxy test (real HTTP)** | P2 | Integration tests dùng ASGI transport, chưa test real HTTP proxy giữa 2 process |
| 3 | **Container isolation per-node** | P2 | Bootstrap tạo process, chưa tạo container |
| 4 | **Persistent job store** | P3 | Job store vẫn in-memory, mất khi restart |
| 5 | **Idempotency key** | P3 | Chưa implement |
| 6 | **Retry policy** | P3 | Chưa có configurable retry cho failed actions |
| 7 | **Artifact registry** | P3 | Chưa có central registry |
| 8 | **Observability** | P3 | OpenTelemetry, Prometheus, structured logging |
| 9 | **Migrate on_event to lifespan** | P4 | FastAPI deprecation warning |

---

## 7. Metrics tổng kết

| Metric | v5.0 | v5.1 | Delta |
|--------|------|------|-------|
| Files code | 25 | 36 | +11 |
| Lines of code | ~1,200 | ~2,400 | +1,200 |
| Test cases | 35 | 78 | +43 |
| Test pass rate | 100% | 100% | — |
| Endpoints | 5 | 6 (+bootstrap) | +1 |
| Dependencies | 8 | 9 (+jsonschema) | +1 |
| Features | Core runtime | +Auth, +Schema, +Cleanup, +Bootstrap, +Rollback | +6 |
| Deploy status | ✅ Running | ✅ Running (v5.1) | Upgraded |

---

*Document generated: 2026-03-01 | Mesh Runtime v5.1 | Repository: ai-infra-runtime-v2*
