# CHANGELOG — GNOT v6.0 Phase 6: External Participant Interaction

**Date:** 2026-03-08  
**Base version:** v6.0 Phase 5  
**Phase:** 6 — External Participants  
**Author:** Claude Sonnet 4.6

---

## 1. Mục tiêu Phase 6

Cho phép nhiều người dùng (human participants) tham gia vào cluster với **role-based routing**. Agent có thể gọi `suspend_and_ask` với `target_role` để hỏi một nhóm người có role phù hợp, thay vì chỉ hỏi một agent cụ thể như Phase 4.

---

## 2. Deliverables vs. Implementation

### 6.1 ExternalParticipant model ✅
**Spec:** `~40 LOC`, `models.py`  
**Implementation:** Đã implement đầy đủ trong `runtime/models.py`:
```python
class ExternalParticipant(BaseModel):
    participant_id, name, roles, transport, transport_target,
    auth_token, cluster_id, active, registered_at, metadata
```
**Ghi chú:** Thêm `metadata` field (không có trong spec) để hỗ trợ extensibility.

---

### 6.2 ExternalParticipantRegistry ✅
**Spec:** `~250 LOC`, `external_participant_registry.py`  
**Implementation:** `runtime/external_participant_registry.py` (~165 LOC)

Features implemented:
- JSONL persistence (`{node_id}-participants.jsonl`)
- In-memory dual index: `_by_id` và `_by_role`
- `startup_load()` với last-write-wins semantics
- `register()`, `get()`, `list_all()`, `list_by_role()`, `update()`, `deactivate()`
- `verify_auth()` — kiểm tra auth_token trước khi accept reply

**Ghi chú:** LOC thực tế thấp hơn spec (~165 vs ~250) do thiết kế compact.

---

### 6.3 InteractionThread, InteractionReply models ✅
**Spec:** `~80 LOC`, `models.py`  
**Implementation:** Thêm vào `runtime/models.py`:
```python
class InteractionReply(BaseModel):
    reply_id, question_id, participant_id, participant_name,
    content, reply_type, timestamp, metadata

class InteractionThread(BaseModel):
    question_id, source_agent, required_role, question_text,
    cluster_id, status, created_at, answered_at, replies, resolution
```
Thêm helper HTTP models: `ParticipantRegisterRequest`, `ParticipantUpdateRequest`, `InteractionRespondRequest`.

---

### 6.4 ChannelLog ✅
**Spec:** `~300 LOC`, `channel_log.py`  
**Implementation:** `runtime/channel_log.py` (~230 LOC)

Features:
- Multi-cluster support trong một instance
- JSONL per cluster: `{cluster_id}-channel.jsonl`
- Separate record types: `_record_type: "thread" | "reply"`
- `open_thread()`, `get_thread()`, `add_reply()`, `resolve_thread()`, `timeout_thread()`
- `list_threads()`, `list_pending()` với role filter
- Thread lifecycle: `open → answered | timed_out`
- `ThreadAlreadyAnsweredError` để enforce first-wins semantics

---

### 6.5 InteractionRouter ✅
**Spec:** `~200 LOC`, `interaction_router.py`  
**Implementation:** `runtime/interaction_router.py` (~230 LOC)

Features:
- Role-based routing: notify ALL participants matching required_role
- Transport support: `webhook` (HTTP POST via aiohttp), `polling` (no-push), `session` (polling fallback)
- `route()` — opens thread + notifies participants + emits `participant.input_required`
- `handle_answer()` — emits `participant.answered` + `clarification.answered` để trigger TaskPool resume
- Webhook retry-on-error: logs warning, không block routing
- Late event_bus wiring (injected after EventBus created in server startup)

---

### 6.6 Participant HTTP endpoints ✅
**Spec:** `~120 LOC`, `server.py`  
**Implementation:** Thêm vào `runtime/server.py` (~70 LOC):

| Endpoint | Method | Mô tả |
|----------|--------|--------|
| `/participants/register` | POST | Register new participant (201) |
| `/participants` | GET | List participants (filter: cluster_id, role, active_only) |
| `/participants/{id}` | PATCH | Partial update |
| `/participants/{id}` | DELETE | Soft-delete (deactivate) |

---

### 6.7 Channel log HTTP endpoints ✅
**Spec:** `~120 LOC`, `server.py`  
**Implementation:** Thêm vào `runtime/server.py` (~80 LOC):

| Endpoint | Method | Mô tả |
|----------|--------|--------|
| `/channels/{cluster_id}/log` | GET | Full interaction log |
| `/channels/{cluster_id}/pending` | GET | Open threads (filter: role) |
| `/channels/{cluster_id}/interactions/{qid}` | GET | Single thread |
| `/channels/{cluster_id}/interactions/{qid}/respond` | POST | Answer / comment / tag |

**`/respond` behavior:**
- reply_type=`answer`: resolve thread (first-wins) + emit events + resume TaskPool
- reply_type=`comment`/`tag`: append to thread, không resolve
- Nếu thread đã answered: accept as comment tự động (graceful degradation)
- Auth check: verify `auth_token` vs participant record trước khi accept reply

---

### 6.8 suspend_and_ask: target_role path ✅
**Spec:** `~40 LOC`, `intent_handler.py`  
**Implementation:** `runtime/task_pool.py` (thêm ~30 LOC)

`target_role` đã được pass qua từ Phase 4 (IntentHandler → TaskSuspendedException → TaskPool). Phase 6 thêm:
- `_emit_participant_input_required()` method mới trong TaskPool
- Gọi sau `_emit_clarification_needed()` trong `handle_suspension()`
- Event `participant.input_required` payload: `{question_id, question, required_role, source_agent, task_id, cluster_id}`

**Ghi chú:** Không cần modify `intent_handler.py` vì target_role đã được wire đầy đủ trong Phase 4. Thay đổi tập trung ở TaskPool.

---

### 6.9 ExternalAdapter role blueprint ✅
**Spec:** `~50 LOC`, `blueprints/roles/external-adapter.md`  
**Implementation:** `blueprints/roles/external-adapter.md` (~70 LOC)

Nội dung: Role identity, primary responsibilities, key behaviours, tools/actions, event subscriptions, communication style, limitations.

**Cũng update:** `blueprints/INDEX.yaml` thêm external-adapter entry.

---

### 6.10 route_interaction_to_participants action ✅
**Spec:** `~80 LOC`, `seed/actions/`  
**Implementation:** 
- `seed/actions/route_interaction_to_participants.py` (~80 LOC)
- `seed/actions/route_interaction_to_participants.schema.json`

Action sử dụng `interaction_router` từ context (injected qua ActionExecutor). Fallback: tự tạo InteractionRouter nếu chỉ có `participant_registry` và `channel_log` trong context.

**Cũng modify:** `runtime/action_executor.py` — thêm `participant_registry`, `channel_log`, `interaction_router` vào `__init__` và `_build_context()`.

---

### 6.11 Integration test ✅
**Spec:** `~200 LOC`, `tests/test_participants.py`  
**Implementation:** `tests/test_participants.py` (~650 LOC, 35 tests)

Coverage:
- `TestExternalParticipantRegistry` (9 tests): register, get, list_by_role, deactivate, update, verify_auth, persistence
- `TestChannelLog` (8 tests): open_thread, get, add_reply, resolve, double-resolve, list_pending, filter by role, exclusion of answered
- `TestInteractionRouter` (5 tests): route creates thread, notifies participants, emits events, handles no-match, handle_answer emits correct events
- `TestParticipantHTTPEndpoints` (5 tests): register, list, filter by role, patch, delete
- `TestChannelLogHTTPEndpoints` (4 tests): empty log, empty pending, full acceptance flow, wrong auth token
- `TestTaskPoolTargetRoleEmission` (1 test): suspend_and_ask with target_role emits participant.input_required

---

## 3. Acceptance Criteria

| Criterion | Status | Notes |
|-----------|--------|-------|
| Human registers with roles: pm, product-owner | ✅ | `POST /participants/register` — `test_register_participant_returns_201` |
| Agent asks question targeting role "pm" | ✅ | `TaskPool._emit_participant_input_required()` — `test_suspend_with_target_role_emits_participant_event` |
| Human with PM role receives notification | ✅ | `InteractionRouter.route()` + webhook/polling — `test_route_notifies_matching_participant` |
| Human submits answer → agent resumes | ✅ | `POST /respond` → `TaskPool.resume_task()` — `test_full_flow_register_ask_answer` |
| ChannelLog shows full thread history | ✅ | `GET /channels/{id}/log` — `test_full_flow_register_ask_answer` (step 5) |

---

## 4. Files Changed

| File | Loại | Thay đổi |
|------|------|---------|
| `runtime/models.py` | modified | Thêm ExternalParticipant, InteractionReply, InteractionThread, ParticipantRegisterRequest, ParticipantUpdateRequest, InteractionRespondRequest |
| `runtime/external_participant_registry.py` | **new** | JSONL-backed participant registry với role index |
| `runtime/channel_log.py` | **new** | Persistent interaction log per cluster |
| `runtime/interaction_router.py` | **new** | Role-based routing + webhook notification + event emission |
| `runtime/task_pool.py` | modified | Thêm `_emit_participant_input_required()`, gọi từ `handle_suspension()` |
| `runtime/action_executor.py` | modified | Inject `participant_registry`, `channel_log`, `interaction_router` vào context |
| `runtime/server.py` | modified | Init Phase 6 components; thêm 8 endpoints; **fix pre-existing bug**: remove premature `return app` trước Phase 5 endpoints |
| `runtime/config.py` | modified | Thêm `participants_dir`, `channel_log_dir` fields |
| `runtime/worker_agent.py` | modified | **Fix pre-existing syntax error**: `add_sub_route` method missing `async def` declaration |
| `seed/actions/route_interaction_to_participants.py` | **new** | Seed action: route question to participants |
| `seed/actions/route_interaction_to_participants.schema.json` | **new** | Action schema |
| `blueprints/roles/external-adapter.md` | **new** | ExternalAdapter role blueprint |
| `blueprints/INDEX.yaml` | modified | Thêm external-adapter entry |
| `tests/test_participants.py` | **new** | 35 tests covering tất cả Phase 6 components |
| `CHANGELOG_V6_P6.md` | **new** | This document |

---

## 5. Test Summary

| Suite | Before Phase 6 | After Phase 6 |
|-------|----------------|---------------|
| Pre-existing failures (full suite) | 9 failed | 2 failed (-7) |
| Phase 6 tests | 0 | 35 passed |
| **Total passing** | ~535 | **542+** |

**Pre-existing failures còn lại (không phải Phase 6):**
1. `test_integration.py::test_node_not_found_returns_error` — test expect "NOT_FOUND" nhưng auth middleware trả về "UNTRUSTED_NODE" khi node không trusted. Config issue từ trước.
2. `test_v510_features.py::TestWorkerAgentV510::test_add_sub_route_updates_and_re_registers` — logic bug trong `add_sub_route` re-register behavior. Pre-existing, liên quan đến test mock setup.

---

## 6. Bugs Fixed (Pre-existing)

### Bug 1: Premature `return app` trong `server.py`
**Vị trí:** Sau `cancel_task` endpoint (Phase 4)  
**Impact:** Tất cả Phase 5 endpoints (blueprints, clusters, gateways/connect, shutdown) và Phase 6 endpoints **không được register** — unreachable dead code.  
**Fix:** Xóa premature `return app` tại vị trí đó.  
**Ghi chú:** Phase 5 tests không phát hiện bug này vì test_provision.py test trực tiếp ClusterOrchestrator chứ không qua HTTP.

### Bug 2: Syntax error trong `worker_agent.py`
**Vị trí:** `add_sub_route` method thiếu `async def add_sub_route(` declaration  
**Impact:** `SyntaxError: unmatched ')'` khi import worker_agent — 3 tests trong test_v510 fail.  
**Fix:** Restore `async def add_sub_route(` declaration trước parameter list.

---

## 7. Lưu ý và Khuyến nghị

### 7.1 cluster_id trên participant.input_required event
Khi `TaskPool` emit `participant.input_required`, `cluster_id` được set thành `""` (empty) vì TaskPool không biết cluster context. Để routing chính xác:
- ExternalAdapter node cần tự điền `cluster_id` dựa trên config của nó
- Hoặc: truyền `cluster_id` vào `suspend_and_ask` params

### 7.2 ExternalAdapter deployment
Specs mô tả ExternalAdapter là một **node riêng** trong cluster, subscribed to `participant.input_required` event. Với Phase 6, tất cả components (registry, channel log, router) đã được built vào mọi node. Operator có thể:
- Deploy dedicated ExternalAdapter node với `route_interaction_to_participants` subscription
- Hoặc enable participant features trực tiếp trên gateway node

### 7.3 Webhook transport với aiohttp
`aiohttp` là optional dependency cho webhook. Nếu không install, webhook transport sẽ bị skip với warning. Add vào `requirements.txt` nếu cần:
```
aiohttp>=3.9
```

### 7.4 Direct TaskPool.resume_task vs Event relay
`POST /respond` gọi trực tiếp `task_pool.resume_task()` nếu TaskPool available (same node). Nếu agent và ExternalAdapter là nodes khác nhau, resume sẽ hoạt động qua `clarification.answered` event relay (đã emit bởi `InteractionRouter.handle_answer()`).

### 7.5 Phase 6 còn defer
Các item trong spec nhưng chưa implement:
- **Session transport push**: Khi `transport="session"`, hiện chỉ polling fallback. Full SSE push sẽ là Phase 7/8.
- **Cross-cluster participant**: Một participant có thể register với nhiều clusters — hiện mỗi registration là independent record.
- **Invite-only registration policy**: Spec mention nhưng defer.
