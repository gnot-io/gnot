# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v5.11
### (Action Schema Exposure + Caller Authorization)

---

## 1. Vấn đề giải quyết trong v5.11

### 1.1 Gap từ v5.10

v5.10 đã giải quyết multi-hop routing. Tuy nhiên hệ thống có hai gap quan trọng khi
deploy vào doanh nghiệp thực tế:

**Gap G1 — LLM không biết schema chi tiết của từng action:**

```
v5.10 system prompt:
  - node-1a (via node-1)
    actions: get_order_info, list_orders     ← chỉ tên, không có gì khác

LLM muốn gọi get_order_info:
  → Params cần gì? Không biết
  → Hallucinate: {"order_number": "123"}    ← sai field name
                 {"id": 123}                ← sai type (int thay vì string)
                 {"order_id": "123"}        ← may đúng, may không
```

**Gap G2 — Không có cơ chế authorization per-action:**

```
v5.10: auth = một Bearer token cho toàn node
  → Ai có token → gọi được mọi action
  → Sales team (chỉ nên có get_order_info) cũng gọi được execute_command
  → DevOps (chỉ nên có execute_command) cũng gọi được get_order_info
  → Không có cách phân quyền
```

### 1.2 Phân tích và quyết định thiết kế

#### Về authorization

Hệ thống không thể predefined hay biết trước permission set của caller. Lý do: action
như `get_order_info` cần kết nối CRM backend — chính CRM mới là authority kiểm tra
"user này có quyền xem order này không". Mesh chỉ là delivery mechanism.

Vì vậy authorization trong v5.11 chia làm **hai cấp độ độc lập:**

| Cấp độ | Mô tả | Quản lý bởi |
|--------|-------|------------|
| **Cấp độ 1** | Caller có quyền gọi mọi action (open) | Không cần config |
| **Cấp độ 2** | Caller chỉ được gọi một số action nhất định | `caller_policies` trong `node.yaml` |

Ngoài ra, **một số action có thể yêu cầu thêm caller credentials** — ví dụ CRM API key
do CRM admin cấp. Action nhận key này, tự gọi CRM, CRM tự validate. Mesh không validate
business logic.

#### Về service credentials (credentials của action, không phải của caller)

Credentials dùng để action kết nối service nội bộ (DB password, service account) là
**trách nhiệm hoàn toàn của action** — đọc từ `os.environ`, không qua framework. Mesh
không biết, không quản lý, không cần quan tâm.

---

## 2. Kiến trúc authorization v5.11

```
POST /action  hoặc  POST /intent
  │
  ├─ AuthMiddleware: kiểm tra Bearer token (node-level, như cũ)
  │    → Không pass → 401 UNAUTHORIZED
  │
  ├─ GatewayRouter.route()
  │    → forward qua next-hop nếu cần (v5.10)
  │
  └─ ActionExecutor.execute()
       │
       ├─ Step 1: CallerPolicy check
       │    caller_policies == [] → PASS (cấp độ 1, open)
       │    caller_policies != [] →
       │      token không trong policies → 400 CALLER_NOT_ALLOWED
       │      token trong policies, action không trong allowed_actions → 400 CALLER_NOT_ALLOWED
       │      token trong policies, action allowed → PASS (cấp độ 2)
       │
       ├─ Step 2: Caller credential check
       │    schema[action]["x-caller-credentials"] == {} → PASS (no requirement)
       │    có requirement, caller_credentials chứa đủ → PASS
       │    có requirement, thiếu credential → 400 MISSING_CALLER_CREDENTIAL
       │
       ├─ Step 3: Params schema validation (như cũ)
       │
       └─ Step 4: Execute
            context["caller_credentials"] = caller_credentials
            # Service credentials: action.run() tự lấy từ os.environ
```

---

## 3. API thay đổi v5.11

### 3.1 POST /action (extended)

**Request:**
```json
{
  "target_node_id": "node-1a",
  "payload": {
    "action": "get_order_info",
    "params": {"order_id": "ORD-123"}
  },
  "caller_credentials": {
    "crm_user_token": "sk-user-abc123"
  }
}
```

`caller_credentials` — field mới, optional (default `{}`):
- Không bao giờ logged (không xuất hiện trong server logs)
- Không lưu vào `QueuedJob.params` (stored separately trong job)
- Không xuất hiện trong response body
- Forward qua pull chain: `QueuedJob.caller_credentials` field riêng
- Inject vào `context["caller_credentials"]` khi execute

**Response errors mới:**
```json
{"error": "CALLER_NOT_ALLOWED: action 'execute_command' is not permitted for this caller"}
{"error": "MISSING_CALLER_CREDENTIAL: action 'get_order_info' requires: crm_user_token"}
```

### 3.2 POST /intent (extended)

```json
{
  "prompt": "Tôi muốn xem thông tin đơn hàng #123",
  "session_id": "telegram-chat-999",
  "caller_credentials": {
    "crm_user_token": "sk-user-abc123"
  }
}
```

`caller_credentials` được forward vào mọi `mesh_action` tool call trong agent loop.
LLM không thấy, không log, không lưu vào conversation history.

### 3.3 GET /capabilities (extended v5.11)

```json
{
  "node_id": "node-0",
  "actions": ["execute_command", "read_file", "get_order_info"],
  "reachable": {
    "node-1a": {
      "node_id": "node-1a",
      "actions": ["get_order_info"],
      "action_specs": {
        "get_order_info": {
          "description": "Retrieve order details from CRM. Caller must provide CRM user token.",
          "params_schema": {
            "order_id": {"type": "string", "description": "Order ID, e.g. ORD-123"},
            "include_items": {"type": "boolean", "default": true}
          },
          "caller_credentials": {
            "crm_user_token": {
              "description": "CRM API key issued to the user by admin",
              "required": true,
              "hint": "Obtain from your CRM admin panel under Settings > API Keys"
            }
          },
          "async_action": false
        }
      },
      "next_hop": "node-1",
      "reachable": {}
    }
  }
}
```

`action_specs` — field mới trong `CapabilityNode`. Chứa full spec của mỗi action bao
gồm:
- `description` — mô tả tự nhiên, LLM đọc để hiểu action dùng cho gì
- `params_schema` — params cần truyền, type, description (LLM dùng để build params đúng)
- `caller_credentials` — credentials caller phải cung cấp (LLM dùng để hỏi user hoặc check session)
- `async_action` — action có chạy async (job polling) không

### 3.4 POST /nodes/register (extended)

```json
{
  "node_id": "node-1a",
  "actions": ["get_order_info"],
  "action_specs": {
    "get_order_info": {
      "description": "Retrieve order details",
      "params_schema": {"order_id": {"type": "string"}},
      "caller_credentials": {
        "crm_user_token": {"description": "CRM API key", "required": true, "hint": ""}
      },
      "async_action": false
    }
  },
  "advertise_routes": [],
  "capabilities": {}
}
```

---

## 4. node.yaml — cấu hình caller_policies

```yaml
node_id: node-1a
listen: 0.0.0.0:8080
auth_token: "sk-node-secret"

# Cấp độ 1: KHÔNG config caller_policies → mọi caller với Bearer token hợp lệ
#            được gọi mọi action.

# Cấp độ 2: CONFIG caller_policies → enforce whitelist per-token.
caller_policies:
  - token: "sk-sales-abc"
    allowed_actions:
      - get_order_info
      - list_orders

  - token: "sk-devops-xyz"
    allowed_actions:
      - execute_command
      - read_file
      - write_file

  - token: "sk-admin-000"
    allowed_actions: "*"          # wildcard = giống cấp độ 1 cho token này
```

**Quy tắc:**
- Nếu `caller_policies` không có hoặc rỗng → cấp độ 1 (open), backward compatible
- Token không có trong policies → **denied**
- Token có trong policies → check `allowed_actions`
- `allowed_actions: "*"` → wildcard, token này có thể gọi mọi action

---

## 5. Action schema — khai báo x-caller-credentials

File `{action_name}.schema.json` mở rộng với section `x-caller-credentials`:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "get_order_info",
  "description": "Retrieve order details from the CRM system. Caller must provide a valid CRM user token.",
  "x-caller-credentials": {
    "crm_user_token": {
      "description": "CRM API key issued to the user by the CRM admin",
      "required": true,
      "hint": "Obtain from your CRM admin panel under Settings > API Keys"
    }
  },
  "type": "object",
  "properties": {
    "order_id": {
      "type": "string",
      "minLength": 1,
      "description": "Order ID to retrieve, e.g. ORD-123"
    },
    "include_items": {
      "type": "boolean",
      "default": true,
      "description": "Whether to include line items in the response"
    }
  },
  "required": ["order_id"],
  "additionalProperties": false
}
```

`x-caller-credentials` là extension field (prefix `x-` là convention JSON Schema cho
vendor extensions). Không ảnh hưởng standard JSON Schema validation.

Mỗi credential entry:
- `description` — mô tả để LLM và user hiểu cần lấy key đó ở đâu
- `required` — nếu `true`, framework reject request nếu không có
- `hint` — gợi ý cách lấy key (hiển thị trong system prompt và error message)

---

## 6. Action module — cách dùng caller_credentials

```python
# get_order_info.py
import os
import httpx

DESCRIPTION = "Retrieve order details from the CRM system"

def run(params, context):
    order_id = params["order_id"]

    # ── Caller credential (validated bởi framework trước khi gọi run()) ──
    user_token = context["caller_credentials"]["crm_user_token"]

    # ── Service credential (action tự quản lý, không qua framework) ──
    service_key = os.environ["CRM_SERVICE_KEY"]

    # Gọi CRM — CRM tự validate user_token (business logic không thuộc về mesh)
    resp = httpx.get(
        f"https://crm.internal/api/orders/{order_id}",
        headers={
            "X-Service-Key":  service_key,   # identify service account
            "X-User-Token":   user_token,     # identify caller, CRM decides access
        }
    )
    resp.raise_for_status()
    return resp.json()
```

**Trách nhiệm phân tách rõ ràng:**

| Layer | Trách nhiệm |
|-------|------------|
| **Framework** | (1) Enforce caller policy — token được gọi action này không? (2) Check caller credentials present — framework deliver, không validate logic (3) Inject `context["caller_credentials"]` |
| **Action** | (1) Dùng `context["caller_credentials"]` để gọi backend (2) Tự quản lý service credentials qua `os.environ` (3) Interpret kết quả từ backend (403 → PermissionError...) |
| **Backend (CRM, ERP...)** | Validate actual authorization — user có quyền xem data này không |

---

## 7. Data Models v5.11

### 7.1 CallerCredentialSpec (new)

```python
class CallerCredentialSpec(BaseModel):
    description: str
    required: bool = True
    hint: str = ""
```

### 7.2 ActionSpec (new)

```python
class ActionSpec(BaseModel):
    description: str = ""
    params_schema: dict[str, Any] = {}
    caller_credentials: dict[str, CallerCredentialSpec] = {}
    async_action: bool = False
```

### 7.3 ActionRequest (extended)

```python
class ActionRequest(BaseModel):
    target_node_id: str
    task_id: str | None = None
    trace: TraceInfo | None = None
    payload: ActionPayload
    # v5.11
    caller_credentials: dict[str, str] = {}   # not logged, not stored in params
    caller_token: str | None = None            # extracted from Authorization header
```

### 7.4 QueuedJob (extended)

```python
class QueuedJob(BaseModel):
    job_id: str
    # ... existing fields ...
    # v5.11 — carry caller auth through pull chain (separate from params)
    caller_credentials: dict[str, str] = {}
    caller_token: str | None = None
```

### 7.5 IntentRequest (extended)

```python
class IntentRequest(BaseModel):
    prompt: str
    session_id: str | None = None
    node_hint: str | None = None
    max_turns: int | None = None
    # v5.11
    caller_credentials: dict[str, str] = {}
```

### 7.6 CapabilityNode (extended)

```python
class CapabilityNode(BaseModel):
    node_id: str
    actions: list[str]
    action_specs: dict[str, ActionSpec] = {}   # v5.11
    next_hop: str | None = None
    reachable: dict[str, "CapabilityNode"] = {}
```

### 7.7 NodeRegistrationRequest (extended)

```python
class NodeRegistrationRequest(BaseModel):
    node_id: str
    address: str | None = None
    actions: list[str] = []
    advertise_routes: list[str] = []
    capabilities: dict[str, list[str]] = {}
    action_specs: dict[str, dict] = {}        # v5.11
```

---

## 8. Config v5.11

### 8.1 CallerPolicy dataclass (new)

```python
@dataclass(frozen=True)
class CallerPolicy:
    token: str
    allowed_actions: list[str] | str   # list hoặc "*"

    def allows(self, action: str) -> bool:
        if self.allowed_actions == "*":
            return True
        return action in self.allowed_actions
```

### 8.2 check_caller_policy() (new)

```python
def check_caller_policy(
    policies: list[CallerPolicy],
    caller_token: str | None,
    action: str,
) -> bool:
    if not policies:
        return True   # cấp độ 1: open
    if caller_token is None:
        return False
    for policy in policies:
        if policy.token == caller_token:
            return policy.allows(action)
    return False  # token không trong policies
```

### 8.3 NodeConfig (extended)

```python
@dataclass(frozen=True)
class NodeConfig:
    # ... existing fields ...
    caller_policies: tuple = ()   # tuple of CallerPolicy, empty = cấp độ 1
```

---

## 9. ActionSchemaValidator — Methods v5.11

| Method | Mô tả |
|--------|-------|
| `get_caller_credential_requirements(action)` | Trả `dict[str, CallerCredentialSpec]` từ `x-caller-credentials` |
| `check_caller_credentials(action, caller_credentials)` | Trả list missing required keys |
| `build_action_spec(action, module?)` | Build `ActionSpec` từ schema + module metadata |
| `build_all_action_specs(registry?)` | Build spec cho tất cả actions có schema |

---

## 10. IntentHandler — System Prompt v5.11

System prompt bây giờ hiển thị đầy đủ thông tin:

```
## Mesh topology — nodes, actions, and requirements
  - node-0 [gateway, THIS NODE]
    ┌─ execute_command: Execute a shell command and return exit_code, stdout, stderr.
    │  param command (string): Shell command to execute
    │  param timeout_seconds (integer): Maximum execution time in seconds
    ┌─ get_order_info: Retrieve order details from CRM. Caller must provide CRM user token.
    │  param order_id (string): Order ID to retrieve, e.g. ORD-123
    │  ⚠ caller_credential crm_user_token (required): CRM API key issued to the user by admin
    │    hint: Obtain from your CRM admin panel under Settings > API Keys
  - node-1a via node-1
    ┌─ get_order_info: Retrieve order details...
    │  param order_id (string): ...
    │  ⚠ caller_credential crm_user_token (required): ...

## Caller credentials
- Actions marked ⚠ caller_credential require a key the user must supply.
- Check if caller_credentials already contains the required key.
- If missing, ask the user for it once; do not ask again in the same session.
```

LLM đọc system prompt → biết:
1. `get_order_info` cần `order_id` (string) — không hallucinate field name/type
2. Action đó cần `crm_user_token` từ caller
3. Nếu session chưa có → hỏi user một lần
4. Sau khi có → gọi action với `caller_credentials` trong request

---

## 11. Security: Credential Isolation

### Đảm bảo credentials không bị leak

| Vị trí | Credential behavior |
|--------|---------------------|
| Server request logs | `caller_credentials` field không được log |
| `QueuedJob.params` | `caller_credentials` được lưu trong field riêng, không trong `params` |
| Response body | Không bao giờ xuất hiện trong output |
| Conversation history | LLM không nhận credential, không lưu vào `session.messages` |
| Pull chain | Forward qua `QueuedJob.caller_credentials`, không trong `action` params |
| `context["caller_credentials"]` | Available chỉ trong `action.run()`, không trong AsyncResponse |

### Ví dụ flow an toàn với pull chain

```
Caller → POST /action {caller_credentials: {"crm_user_token": "sk-secret"}}
  │
  node-0.GatewayRouter:
    → enqueue pull job {
        job_id: "j1",
        action: "get_order_info",
        params: {"order_id": "123"},       ← KHÔNG có secret
        caller_credentials: {"crm_user_token": "sk-secret"},  ← field riêng
        caller_token: "sk-caller"
      }
  │
  node-1a.WorkerAgent polls:
    → _claim_and_execute(job)
    → executor.execute(
          "get_order_info",
          params={"order_id": "123"},
          caller_credentials={"crm_user_token": "sk-secret"},
      )
    → context["caller_credentials"] = {"crm_user_token": "sk-secret"}
    → action.run(params, context)
    → secret never in logs, never in result
```

---

## 12. Backward Compatibility

| Tình huống | Behaviour |
|-----------|-----------|
| `caller_policies` không config | Cấp độ 1 (open) — hoàn toàn như v5.10 |
| Request không có `caller_credentials` | `{}` — actions không yêu cầu credentials: OK |
| Action không có schema | Không validate credentials — allowed through |
| Schema không có `x-caller-credentials` | `{}` — không yêu cầu credentials |
| Node registration không có `action_specs` | `{}` — capability tree hiển thị actions, không có specs |
| Client dùng API v5.10 | Fully compatible — mọi field mới đều optional |

---

## 13. Files thay đổi

| File | Loại | Thay đổi chính |
|------|------|----------------|
| `runtime/models.py` | Modified | `CallerCredentialSpec`, `ActionSpec`; `caller_credentials`/`caller_token` trong `ActionRequest`, `QueuedJob`, `IntentRequest`; `action_specs` trong `CapabilityNode`, `NodeRegistrationRequest` |
| `runtime/config.py` | Modified | `CallerPolicy` dataclass; `check_caller_policy()`; `caller_policies` field trong `NodeConfig`; parse từ `node.yaml` |
| `runtime/schema_validator.py` | Modified | `get_caller_credential_requirements()`, `check_caller_credentials()`, `build_action_spec()`, `build_all_action_specs()` |
| `runtime/action_executor.py` | Modified | `CallerNotAllowedError`, `MissingCallerCredentialError`; `caller_policies` param; policy check + credential check trước execute; `caller_credentials` trong context |
| `runtime/job_queue.py` | Modified | `enqueue()` thêm `caller_token`, `caller_credentials` |
| `runtime/gateway_router.py` | Modified | `_execute_local()` pass `caller_token`/`caller_credentials`; `_pull()` pass vào enqueue |
| `runtime/worker_agent.py` | Modified | `schema_registry` param; advertisement gồm `action_specs`; `_claim_and_execute()` pass credentials đến executor |
| `runtime/node_registry.py` | Modified | `_NodeEntry.action_specs`; `register()` lưu specs; `build_capability_tree()` expose specs |
| `runtime/server.py` | Modified | `executor` nhận `caller_policies`; `/action` extract `caller_token`; `/nodes/register` pass `action_specs`; `IntentHandler` nhận `schema_validator`; version `5.11.0` |
| `mesh/node_runtime.py` | Modified | Pass `schema_registry` vào `WorkerAgent`; `attach_worker_agent()` nhận `schema_registry` |
| `runtime/intent_handler.py` | Modified | Constructor nhận `schema_validator`; `handle()` propagate `caller_credentials`; `_build_system_prompt()` render full ActionSpec |
| `seed/actions/get_order_info.py` | New | Sample enterprise action minh họa credential pattern |
| `seed/actions/get_order_info.schema.json` | New | Schema với `x-caller-credentials` |
| `tests/test_v511_features.py` | New | 36 tests |
| `docs/SPECS_V5.11.md` | New | Document này |
| `docs/WORKLOG_V5.11.md` | New | Implementation worklog |

---

## 14. Trạng thái hệ thống (v5.11)

**Đã có:**
- ✅ Bootstrap minimal + plugin actions
- ✅ Async job model (job_id polling)
- ✅ Distributed routing (push/pull/NAT)
- ✅ Authentication (Bearer token, node-level)
- ✅ Action schema validation
- ✅ NodeRegistry + WorkerAgent + JobQueue
- ✅ Claude Web curl-native workflow (v5.6)
- ✅ Lazy staleness + pull job timeout (v5.7)
- ✅ File upload/download (v5.8)
- ✅ POST /intent — ReAct agent loop (v5.9)
- ✅ BGP-style route advertisement + multi-hop (v5.10)
- ✅ GET /capabilities — capability tree (v5.10)
- ✅ **Action schema exposure — description + params (v5.11)**
- ✅ **Caller authorization — per-token action allowlist (v5.11)**
- ✅ **Caller credential delivery — secure, not logged (v5.11)**
- ✅ **x-caller-credentials in schema — LLM-visible requirement (v5.11)**
- ✅ **Service credentials — action tự quản lý qua env (v5.11 pattern)**

**Chưa có:**
- ❌ Route withdrawal khi node ngắt kết nối (P3 — v5.12)
- ❌ Result forwarding chain qua pull-mode multi-hop (P3 — v5.12)
- ❌ Session credential storage (P3 — user cung cấp một lần, session nhớ)
- ❌ Credential encryption at rest trong session (P3)
- ❌ Container isolation (P2)
- ❌ Job queue persistence (P3)
- ❌ Streaming intent response / SSE (P3)

---

*Spec: SPECS_V5.11.md | Mesh Runtime v5.11 | Repository: ai-infra-runtime-v2*
