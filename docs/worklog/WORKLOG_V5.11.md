# Worklog — Execution Mesh v5.11 (Action Schema Exposure + Caller Authorization)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-05
**Base version:** v5.10 → v5.11

---

## 1. Vấn đề / Context

### 1.1 Câu hỏi từ anh

> LLM có thể biết danh sách các action, nhưng description và schema chi tiết của từng
> action thì LLM đã biết chưa? LLM biết thì mới có thể gởi chính xác cấu trúc params.

> Khi triển khai vào doanh nghiệp, action như get_order_info yêu cầu user phải có quyền
> mới truy xuất được (API key). Việc yêu cầu API key sẽ được mô tả trong schema của
> action?

### 1.2 Phân tích ban đầu

**Gap G1 — Schema exposure:**
`SchemaRegistry` đã load `.schema.json` files kể từ v5.3 nhưng chỉ dùng để validate
params trước khi execute. Không bao giờ được expose ra ngoài: không trong response, không
trong capability tree, không trong IntentHandler system prompt.

Vì vậy LLM đang dựa hoàn toàn vào prior knowledge từ training data:
- `execute_command` — LLM biết vì đây là pattern phổ biến
- `get_order_info` — LLM không biết, tự đoán params, hallucinate field names

**Gap G2 — Authorization:**
Auth hiện tại là binary: có Bearer token hợp lệ → được gọi mọi thứ. Không có per-action
granularity.

---

## 2. Phân tích Approaches

### 2.1 Về authorization: ai validate credential?

**Câu hỏi chính:** Hệ thống có nên validate business logic của credentials không?
(e.g., "sk-user-abc có được xem order #123 không?")

**Kết luận:** Không. Mesh là delivery mechanism. Backend (CRM, ERP) là authority.
Mesh chỉ:
1. Kiểm tra caller có được gọi action này không (allowlist)
2. Kiểm tra caller có cung cấp đủ credentials mà action yêu cầu không
3. Deliver credentials đến action qua context

**Phân tách hai cấp độ (do anh confirm):**
- Cấp độ 1: open, không config gì, backward compatible
- Cấp độ 2: whitelist per Bearer token trong `node.yaml`

### 2.2 Về service credentials (node-side)

**Decision:** Framework không quản lý service credentials.

Lý do: Action là Python module — nó tự biết cần gì và lấy từ đâu. Pattern `os.environ`
là standard 12-factor app, operator đã quen. Thêm `node.yaml` credential management =
thêm complexity không cần thiết, và không thêm security value nào (secrets vẫn phải
store ở đâu đó trên node).

Nếu sau này cần rotation/vault integration → action implement bằng cách gọi
vault SDK trong `run()`, không cần framework thay đổi.

### 2.3 Về schema declaration

**Approach được chọn: `x-caller-credentials` trong schema file.**

Lý do:
- Consistent với cách params đã được khai báo (`.schema.json`)
- `x-` prefix là convention JSON Schema cho vendor extensions — không break standard
  JSON Schema tooling
- Operator/developer có thể edit schema mà không cần touch Python code
- Single source of truth cho một action = một schema file + một .py file

Alternative bị reject:
- Module attribute `CREDENTIALS = {...}` — coupling code với framework convention,
  dễ drift nếu developer quên, không có tooling để validate
- Separate credentials file — quá nhiều files per action

---

## 3. Implementation

### Step 1: models.py

Thêm:
- `CallerCredentialSpec(description, required, hint)` — spec cho một credential
- `ActionSpec(description, params_schema, caller_credentials, async_action)` — full spec
- `caller_credentials: dict[str, str]` vào `ActionRequest` — field riêng, không trong params
- `caller_token: str | None` vào `ActionRequest` — extracted từ Auth header
- Tương tự cho `QueuedJob` — để carry qua pull chain
- `caller_credentials: dict[str, str]` vào `IntentRequest`
- `action_specs: dict[str, ActionSpec]` vào `CapabilityNode`
- `action_specs: dict[str, dict]` vào `NodeRegistrationRequest`

**Quyết định `QueuedJob.caller_credentials` field riêng vs trong params:**
Nếu để vào `params`, credentials lẫn vào business data — dễ bị log, dễ bị expose qua
`/result/{job_id}`. Field riêng = explicit isolation. Tradeoff: QueuedJob model lớn hơn,
nhưng security > cleanliness ở đây.

### Step 2: config.py

Thêm `CallerPolicy` dataclass với method `allows(action)`. Thêm `check_caller_policy()`
function. Thêm `caller_policies: tuple` vào `NodeConfig`. Parse từ `node.yaml`:

```python
caller_policies=tuple(
    CallerPolicy(token=p["token"], allowed_actions=p.get("allowed_actions", "*"))
    for p in raw.get("caller_policies", [])
    if "token" in p
)
```

Dùng `tuple` (immutable) thay vì `list` vì `NodeConfig` là `frozen=True` dataclass.

### Step 3: schema_validator.py

Thêm 4 methods mới vào `ActionSchemaValidator`:
- `get_caller_credential_requirements(action)` — parse `x-caller-credentials`
- `check_caller_credentials(action, caller_credentials)` — list missing required keys
- `build_action_spec(action, module?)` — ActionSpec từ schema + module ASYNC flag
- `build_all_action_specs(registry?)` — dict spec cho tất cả actions có schema

Import `ActionSpec, CallerCredentialSpec` từ models để tránh circular import issue —
schema_validator import models là one-way dependency.

### Step 4: action_executor.py

Thêm hai exceptions mới: `CallerNotAllowedError`, `MissingCallerCredentialError`.

Update `execute()` signature: thêm `caller_token`, `caller_credentials`.

Logic mới trước schema validation:
```python
# Step 1: Policy check
if not check_caller_policy(self._caller_policies, caller_token, action_name):
    raise CallerNotAllowedError(action_name)

# Step 2: Credential check (chỉ check required credentials)
if self._schema_validator:
    missing = self._schema_validator.check_caller_credentials(
        action_name, caller_credentials or {}
    )
    if missing:
        raise MissingCallerCredentialError(action_name, missing)
```

Update `_build_context()`: inject `caller_credentials` vào context.

**Thứ tự check:** Policy → Credential → Schema validation → Execute. Lý do: nếu
caller không có quyền, không nên leak thông tin về schema hay credentials requirements.

### Step 5: job_queue.py

Update `enqueue()` signature thêm `caller_token`, `caller_credentials`. Pass vào
`QueuedJob` constructor. Không log credentials ở bất kỳ đâu.

### Step 6: gateway_router.py

- `_execute_local()`: pass `caller_token`, `caller_credentials` từ `request` vào executor.
  Bắt `CallerNotAllowedError` và `MissingCallerCredentialError` → trả `ErrorResponse`.
- `_pull()`: pass `caller_token`, `caller_credentials` vào `job_queue.enqueue()`.

**Lý do bắt exception ở `_execute_local()` thay vì executor:**
GatewayRouter đã có pattern bắt `KeyError` và wrap vào `ErrorResponse`. Consistent hơn
khi bắt authorization errors cùng chỗ.

### Step 7: worker_agent.py

- Constructor: thêm `schema_registry: dict | None = None`.
- `_register()`: khi build advertisement payload, nếu có `schema_registry` thì build
  `action_specs` và include trong payload.
- `_claim_and_execute()`: pass `job.caller_token`, `job.caller_credentials` đến executor.

### Step 8: node_registry.py

- `_NodeEntry`: thêm `action_specs: dict = field(default_factory=dict)`.
- `register()`: thêm `action_specs` parameter, lưu vào entry (cả khi create mới và
  khi update existing).
- `build_capability_tree()`: deserialize `action_specs` dicts thành `ActionSpec` objects.

**Deserialization approach:**
```python
for aname, raw_spec in entry.action_specs.items():
    if isinstance(raw_spec, ActionSpec):
        specs[aname] = raw_spec
    elif isinstance(raw_spec, dict):
        try:
            specs[aname] = ActionSpec(**raw_spec)
        except Exception:
            pass  # silently skip malformed spec
```

Dùng try/except thay vì validate vì specs đến từ external nodes (không trust format).

### Step 9: server.py

- Pass `caller_policies=list(config.caller_policies)` vào `ActionExecutor`.
- `/action` endpoint: thêm `http_req: Request` parameter để extract Authorization header.
  `caller_token = auth_header.removeprefix("Bearer ").strip() or None`.
  Dùng `req.model_copy(update={"caller_token": caller_token})` thay vì mutate.
- `/nodes/register`: pass `action_specs=getattr(req, 'action_specs', None)`.
- `IntentHandler`: pass `schema_validator=schema_validator`.
- Bump version `5.11.0`.

**Về `http_req: Request` trong FastAPI:**
FastAPI cho phép inject cả pydantic model và `Request` object vào cùng endpoint. Khi
có `http_req: Request` param, FastAPI tự inject raw Starlette request. Không cần
change route definition hay middleware.

### Step 10: node_runtime.py

- `attach_worker_agent()`: thêm `schema_registry: object | None = None` param.
- `WorkerAgent(...)`: thêm `schema_registry=dict(schema_registry or {})`.
- Call site: `attach_worker_agent(app, config, registry, schema_registry)`.

### Step 11: intent_handler.py

- Constructor: thêm `schema_validator: object | None = None`.
- `handle()`: lấy `caller_credentials = dict(req.caller_credentials)`, pass vào
  `_execute_tool_call(tc, caller_credentials)`.
- `_execute_tool_call()`: thêm `caller_credentials` param, inject vào `ActionRequest`.
- `_build_system_prompt()`: refactor hoàn toàn để render ActionSpec với params và
  caller_credentials. Action có `caller_credentials` requirement → hiển thị `⚠ caller_credential`.

**Design của `_render_node()` helper:**
Tách logic render một node thành helper function để dùng lại cho cả own-node và
remote nodes. Tránh code duplication và đảm bảo consistency.

### Step 12: Sample action

Tạo `seed/actions/get_order_info.py` + `get_order_info.schema.json` minh họa pattern:
- Schema có `x-caller-credentials: {crm_user_token: ...}`
- Action đọc `context["caller_credentials"]["crm_user_token"]`
- Action đọc `os.environ["CRM_SERVICE_KEY"]` cho service credential
- Comment giải thích "CRM validates the token, not mesh"

---

## 4. Tests

| Class | Tests | Covers |
|-------|-------|--------|
| `TestCallerPolicy` | 8 | `CallerPolicy.allows()`, `check_caller_policy()` mọi trường hợp: no policies, found+allowed, found+denied, unknown token, no token |
| `TestSchemaValidatorV511` | 8 | `get_caller_credential_requirements()`, `check_caller_credentials()` pass/fail, `build_action_spec()`, `build_all_action_specs()` |
| `TestActionExecutorV511` | 6 | No policies allows all; policy allow/block; missing credential raises; credential injected into context; unknown token denied |
| `TestNodeRegistryV511` | 2 | `register()` stores action_specs; `build_capability_tree()` exposes specs |
| `TestCapabilityEndpointV511` | 2 | GET /capabilities includes action_specs; own actions from schema files |
| `TestEndToEndV511` | 5 | No policies any token OK; policy blocks disallowed; missing cred → 400; with cred → 200; credentials NOT in response |
| `TestConfigV511` | 2 | `load_config()` với policies; empty policies |
| `TestIntentHandlerV511` | 3 | credentials forwarded to tool call; system prompt shows credential requirement; system prompt shows description |
| **Tổng mới** | **36** | |

**v5.10: 112 tests → v5.11: 148 tests (+36) — 148/148 pass**

---

## 5. Decisions log

### D1: Tại sao credential không đi qua params?

`ActionRequest.caller_credentials` là field riêng thay vì để trong
`ActionRequest.payload.params`.

Lý do:
1. `params` được log trong nhiều chỗ: server request log, executor debug log
2. `params` lưu trong `QueuedJob.params` trong memory (và tương lai có thể persist)
3. `params` đi vào `action.run(params, ctx)` — developer có thể vô tình log params
4. Explicit field = explicit intent: đây là sensitive data, xử lý khác với business data

### D2: Tại sao CallerNotAllowedError không return 403?

Authorization error return **400** thay vì **403 Forbidden**.

Lý do: Hệ thống đã có 403 cho sai Bearer token (node-level auth). Nếu dùng 403 cho
policy violation, caller không phân biệt được "Bearer token sai" vs "token đúng nhưng
không có quyền". 400 với `CALLER_NOT_ALLOWED` trong error message là rõ ràng hơn.

Có thể đổi thành 403 trong tương lai nếu cần strict REST compliance.

### D3: Tại sao `caller_policies` là `tuple` trong NodeConfig?

`NodeConfig` là `@dataclass(frozen=True)` — immutable. `list` không thể là field của
frozen dataclass (unhashable). `tuple` là immutable sequence phù hợp.

Khi sử dụng trong code cần list operations: `list(config.caller_policies)`.

### D4: Tại sao không validate credential format tại framework level?

Framework chỉ check "credential có mặt không" (`key in caller_credentials`) — không
validate format (e.g., "sk-" prefix, length, etc.).

Lý do: Framework không biết format của credentials là gì — mỗi backend có convention
riêng. Nếu key có mặt nhưng sai format → backend sẽ trả lỗi → action propagate error
về LLM. LLM có thể hỏi lại user.

### D5: `_render_node()` là inner function trong `_build_system_prompt()`

Thay vì method của class, dùng inner function để:
1. Không expose ra API public của class (implementation detail)
2. Closure tự nhiên — không cần pass params không liên quan
3. Consistent với pattern đã có trong codebase (các helper nhỏ)

---

## 6. Remaining Items (cập nhật)

| Priority | Item | Status | Ghi chú |
|----------|------|--------|---------|
| ✅ | **Action schema exposure** | Done v5.11 | description + params_schema |
| ✅ | **Caller authorization (allowlist)** | Done v5.11 | caller_policies in node.yaml |
| ✅ | **Caller credential delivery** | Done v5.11 | secure, not logged |
| ✅ | **x-caller-credentials in schema** | Done v5.11 | LLM-visible |
| P3 | Route withdrawal khi node disconnect | 🆕 v5.12 | |
| P3 | Result forwarding chain (pull multi-hop) | 🆕 v5.12 | |
| P3 | Session credential storage | v5.12 | User cung cấp 1 lần, session nhớ trong ConversationStore |
| P3 | Credential encryption in session | v5.12 | AES-GCM hoặc tương đương |
| P3 | Job queue persistence | Deferred | SQLite backend |
| P3 | Streaming intent response | Deferred | SSE |
| P2 | Container isolation | Deferred | non-root + ulimit |
| P4 | JWT-based caller_policies | Deferred | Token tự carry allowed_actions |
