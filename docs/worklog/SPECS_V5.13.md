# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v5.13
### (Key Rotation · Credential Persistence · Action Specs Cascade)

---

## 1. Vấn đề giải quyết trong v5.13

### G1 — Key rotation vô tình xóa credentials

v5.12 dùng `auth_token` làm root secret cho credential encryption:

```
key = SHA-256(auth_token + ":" + session_id)
```

Operator rotate `auth_token` (security routine, mỗi 90 ngày) → key mới → tất cả
ciphertext cũ không thể decrypt → mọi active sessions mất credentials → user phải
nhập lại. Operator không nhận ra hệ quả, khó debug.

### G2 — Process restart xóa credentials

`CredentialStore._store` là in-memory dict. Node restart (deploy update, container
restart) → dict empty → tất cả sessions mất credentials. Session TTL còn 45 phút nhưng
sau restart user vẫn phải nhập lại.

### G3 — Action specs không cascade qua multi-hop

Khi `node-1a` register với `node-1`, specs được lưu trong `node-1`'s NodeRegistry ✓.
Nhưng khi `node-1` re-advertise lên `node-0`, chỉ có `actions: list[str]` được forward —
`action_specs` bị drop. `node-0`'s NodeRegistry tạo entry cho `node-1a` với
`action_specs = {}`. LLM thấy `node-1a` nhưng không biết params hay credential requirements
cho `get_order_info`.

---

## 2. Kiến trúc giải pháp

### 2.1 Separate Encryption Key (G1)

**Thêm field `credential_encryption_key` vào `node.yaml`:**

```yaml
auth_token: "sk-node-auth-secret"          # authentication (giữ nguyên)
credential_encryption_key: "sk-enc-stable" # encryption key riêng (v5.13)
```

**Key derivation (v5.13):**
```
# Ưu tiên 1: credential_encryption_key (explicit, dedicated)
key = SHA-256(credential_encryption_key + ":" + session_id)

# Fallback (backward-compat, dùng khi không set credential_encryption_key):
key = SHA-256(auth_token + ":" + session_id)   # như v5.12
```

**Logic trong `server.py`:**
```python
_cred_enc_key = config.credential_encryption_key or config.auth_token
credential_store = CredentialStore(encryption_key=_cred_enc_key, ...)
```

**Kết quả:**
- Operator rotate `auth_token` → `credential_encryption_key` không đổi → credentials vẫn OK
- Operator muốn invalidate tất cả credentials (explicit re-key) → đổi `credential_encryption_key`
- Không set `credential_encryption_key` → fallback `auth_token` (backward compat với v5.12)

**Fingerprint cho rotation audit:**
```python
store.encryption_key_fingerprint  # → "a3f7b2c1" (8 hex chars của SHA-256 key)
# Log khi startup — operator có thể verify key đúng mà không lộ key
```

---

### 2.2 Credential Persistence (G2)

**Opt-in qua `credential_store_path` trong `node.yaml`:**

```yaml
credential_store_path: "/var/lib/mesh/credentials.json"
```

Nếu không set → in-memory only (backward compat).

**Architecture: Write-through với atomic flush**

```
In-memory dict  ─── fast reads, all operations
      │
      │  dirty=True khi merge/clear/touch
      │
Background flush task  (mỗi FLUSH_INTERVAL_SECONDS = 5s)
      │  if dirty: serialize → write .tmp → os.replace(.tmp, path)
      ▼
/var/lib/mesh/credentials.json  (JSON, ciphertext only)
```

**Atomic write pattern:**
```python
tmp = Path(path).with_suffix(".tmp")
tmp.write_text(json.dumps(data))
os.replace(tmp, path)   # POSIX atomic rename — no partial write visible to readers
```

**File format:**
```json
{
  "version": 1,
  "sessions": {
    "<session_id>": {
      "expires_at": 1234567890.123,
      "creds": {
        "crm_user_token": "base64(nonce[12]||ct||tag[16])",
        "erp_key":        "base64(nonce[12]||ct||tag[16])"
      }
    }
  }
}
```
Không có plaintext trong file. `expires_at` visible (không sensitive).

**Startup sequence:**
```python
# lifespan startup:
_cred_sessions = await credential_store.load()   # load từ file, skip expired
credential_store.start_flush_task()              # bắt đầu background flush

# lifespan shutdown:
await credential_store.stop_flush_task()         # cancel task + final flush
```

**`load()` behavior:**
- File không tồn tại → return 0, không error
- Version mismatch → skip, log warning, return 0
- Expired entries → skip
- Decrypt error → skip entry, log error (key change after persist)

**Dirty tracking:**
```
merge()  → dirty = True
clear()  → dirty = True (nếu entry tồn tại)
touch()  → dirty = True
flush()  → dirty = False sau khi write thành công
```

---

### 2.3 Action Specs Cascade (G3)

**Root cause:** Chuỗi propagation bị đứt ở hai chỗ:

```
node-1a registers với node-1:
  POST /nodes/register {action_specs: {get_order_info: {...}}}
  → server.py: add_sub_route("node-1a", ["get_order_info"])   ← specs DROPPED

node-1 re-advertises lên node-0:
  POST /nodes/register {capabilities: {"node-1a": ["get_order_info"]}}  ← specs NOT INCLUDED
  → node-0 NodeRegistry: node-1a entry, action_specs={}                  ← EMPTY
```

**Fix — ba thay đổi đồng bộ:**

**(A) `SubRouteInfo` dataclass thay thế `list[str]`:**
```python
@dataclass
class SubRouteInfo:
    actions: list[str]
    action_specs: dict[str, dict] = field(default_factory=dict)

# Before (v5.12): _sub_routes: dict[str, list[str]]
# After  (v5.13): _sub_routes: dict[str, SubRouteInfo]
```

**(B) `add_sub_route()` nhận `action_specs`:**
```python
async def add_sub_route(
    self,
    sub_node_id: str,
    sub_actions: list[str],
    action_specs: dict[str, dict] | None = None,   # v5.13
) -> None:
    self._sub_routes[sub_node_id] = SubRouteInfo(
        actions=list(sub_actions),
        action_specs=dict(action_specs or {}),
    )
```

**(C) `_register()` advertise specs qua `sub_route_specs` field:**
```python
sub_specs: dict[str, dict] = {}
for sub_id, info in sub_routes_snapshot.items():
    cap[sub_id] = info.actions
    if info.action_specs:
        sub_specs[sub_id] = info.action_specs

payload["capabilities"] = cap
if sub_specs:
    payload["sub_route_specs"] = sub_specs   # NEW field
```

**(D) `NodeRegistrationRequest.sub_route_specs` field mới:**
```python
class NodeRegistrationRequest(BaseModel):
    ...
    sub_route_specs: dict[str, dict] = Field(default_factory=dict)
    # {sub_node_id: {action_name: ActionSpec dict}}
```

**(E) `NodeRegistry.register()` nhận và apply `sub_route_specs`:**
```python
for sub_id in (advertise_routes or []):
    sub_specs = (sub_route_specs or {}).get(sub_id, {})   # v5.13
    if sub_existing:
        if sub_specs:
            sub_existing.action_specs = dict(sub_specs)
    else:
        _entries[sub_id] = _NodeEntry(action_specs=dict(sub_specs), ...)
```

**(F) `server.py /nodes/register` gọi `add_sub_route()` với specs + registry với sub_route_specs:**
```python
# Khi có WorkerAgent: propagate upward
_sub_node_specs = req.sub_route_specs.get(advertised_id)
if advertised_id == req.node_id:
    _sub_node_specs = _sub_node_specs or req.action_specs or None
await agent.add_sub_route(advertised_id, advertised_actions, action_specs=_sub_node_specs)

# Khi update NodeRegistry directly:
await node_registry.register(..., sub_route_specs=req.sub_route_specs or None)
```

**Flow đầy đủ sau v5.13:**

```
node-1a → POST node-1/nodes/register {
    action_specs: {"get_order_info": {description, params_schema, caller_credentials}}
}

node-1's server.py:
  1. node_registry.register("node-1a", action_specs={...}) → stored in node-1's registry ✓
  2. agent.add_sub_route("node-1a", actions, action_specs={...}) → SubRouteInfo stored ✓

node-1 periodic re-register → POST node-0/nodes/register {
    capabilities: {"node-1a": ["get_order_info"]},
    sub_route_specs: {"node-1a": {"get_order_info": {description, params_schema, ...}}}
}

node-0's server.py:
  node_registry.register("node-1", ..., sub_route_specs={"node-1a": {...}})
  → sub_id="node-1a": _entries["node-1a"].action_specs = specs ✓

GET /capabilities on node-0:
  node-1a.action_specs = {"get_order_info": {description, params_schema, caller_credentials}} ✓

LLM system prompt on node-0:
  - node-1a via node-1
    ┌─ get_order_info: Retrieve order details from CRM
    │  param order_id (string): Order ID, e.g. ORD-123
    │  ⚠ caller_credential crm_user_token (required): CRM API key  ✓
```

---

## 3. API Changes

### 3.1 node.yaml — new fields

```yaml
# v5.13 — Credential store

# Dedicated AES encryption key. If set, auth_token rotation doesn't
# invalidate stored credentials. If unset, falls back to auth_token (v5.12 compat).
credential_encryption_key: "base64-or-any-string-32-bytes-recommended"

# Path to persist credentials across restarts. If unset, in-memory only.
credential_store_path: "/var/lib/mesh/credentials.json"
```

### 3.2 POST /nodes/register — extended request body

```json
{
  "node_id": "node-1",
  "actions": ["read_file"],
  "advertise_routes": ["node-1a"],
  "capabilities": {"node-1a": ["get_order_info"]},
  "action_specs": {
    "read_file": {"description": "...", "params_schema": {}, "caller_credentials": {}, "async_action": false}
  },
  "sub_route_specs": {
    "node-1a": {
      "get_order_info": {
        "description": "Get order from CRM",
        "params_schema": {"order_id": {"type": "string"}},
        "caller_credentials": {"crm_user_token": {"description": "...", "required": true, "hint": "..."}},
        "async_action": false
      }
    }
  }
}
```

`sub_route_specs` — field mới, optional (default `{}`). Backward compatible.

### 3.3 CredentialStore API (v5.13)

```python
class CredentialStore:
    def __init__(
        self,
        encryption_key: str | None = None,   # was auth_token in v5.12
        ttl_seconds: int = 3600,
        store_path: str | None = None,        # NEW: path for JSON persistence
    )

    async def load() -> int        # NEW: load from disk on startup
    def start_flush_task() -> None  # NEW: start background flusher
    async def stop_flush_task() -> None  # NEW: cancel + final flush on shutdown
    async def flush() -> bool      # NEW: manual flush (returns True if written)

    @property
    encryption_key_fingerprint: str  # NEW: 8-hex audit fingerprint
```

**Breaking change (v5.12 → v5.13):**
`auth_token=` parameter renamed to `encryption_key=`. Production code auto-migrated via
`server.py` logic. Tests using `CredentialStore(auth_token=...)` directly must update to
`CredentialStore(encryption_key=...)`.

---

## 4. NodeRegistry.register() — extended signature

```python
async def register(
    self,
    node_id: str,
    address: str | None = None,
    actions: list[str] | None = None,
    advertise_routes: list[str] | None = None,
    capabilities: dict[str, list[str]] | None = None,
    action_specs: dict[str, dict] | None = None,
    sub_route_specs: dict[str, dict] | None = None,  # NEW v5.13
) -> None:
```

---

## 5. Security: Separation of Concerns

| Secret | Purpose | Rotate how often | Impact of rotation |
|--------|---------|-----------------|-------------------|
| `auth_token` | Node-to-node authentication | As needed (security incident) | WorkerAgent must re-register; callers must update Bearer tokens |
| `credential_encryption_key` | Credential ciphertext | Annually or on compromise | Active sessions lose credentials (intentional) |

**Recommendation:** Set `credential_encryption_key` explicitly in production.
If unset, `auth_token` rotation accidentally invalidates credentials (v5.12 behavior preserved but discouraged).

---

## 6. Files thay đổi

| File | Loại | Thay đổi chính |
|------|------|----------------|
| `runtime/credential_store.py` | Rewritten | `encryption_key` param (was `auth_token`); `store_path` persistence; `load()`, `start_flush_task()`, `stop_flush_task()`, `flush()`; `encryption_key_fingerprint` property; dirty tracking; background flush loop |
| `runtime/config.py` | Modified | `credential_encryption_key: str | None`; `credential_store_path: str | None`; both parsed from `node.yaml` |
| `runtime/server.py` | Modified | Derive `_cred_enc_key` (enc key wins over auth_token); pass `store_path` to CredentialStore; `load()` + `start_flush_task()` in lifespan startup; `stop_flush_task()` in shutdown; pass `sub_route_specs` to `node_registry.register()` + `add_sub_route()`; version `5.13.0` |
| `runtime/node_registry.py` | Modified | `register()` accepts `sub_route_specs`; sub-entry creation applies specs from `sub_route_specs` |
| `runtime/models.py` | Modified | `NodeRegistrationRequest.sub_route_specs: dict[str, dict]` field |
| `runtime/worker_agent.py` | Modified | `SubRouteInfo` dataclass; `_sub_routes: dict[str, SubRouteInfo]`; `add_sub_route(action_specs=)` param; `_register()` builds + sends `sub_route_specs` in payload; `_register_with_live_routes()` uses `SubRouteInfo` |
| `tests/test_v513_features.py` | New | 38 tests |
| `tests/test_v510_features.py` | Modified | `_sub_routes` assertions updated for `SubRouteInfo` |
| `tests/test_v512_features.py` | Modified | `CredentialStore(auth_token=)` → `CredentialStore(encryption_key=)` |
| `docs/SPECS_V5.13.md` | New | Document này |
| `docs/WORKLOG_V5.13.md` | New | Implementation worklog |

---

## 7. Trạng thái hệ thống (v5.13)

**Đã có:**
- ✅ Bootstrap minimal + plugin actions
- ✅ Async job model, distributed routing, authentication, schema validation
- ✅ BGP-style route advertisement + multi-hop (v5.10)
- ✅ Action schema exposure + caller authorization + credential delivery (v5.11)
- ✅ Route withdrawal, result forwarding chain (v5.12)
- ✅ Session credential storage + AES-256-GCM encryption (v5.12)
- ✅ **Separate encryption key — auth_token rotation safe (v5.13)**
- ✅ **Credential persistence — atomic JSON, survive restarts (v5.13)**
- ✅ **Action specs cascade — full specs propagate through multi-hop (v5.13)**

**Chưa có:**
- ❌ Credential re-encryption khi `credential_encryption_key` đổi (hiện tại: old ciphertexts silently dropped)
- ❌ Job queue persistence (SQLite)
- ❌ Streaming intent response (SSE)
- ❌ Container isolation (non-root + ulimit)
- ❌ Graceful shutdown route withdrawal (explicit WITHDRAW trước khi tắt)

---

*Spec: SPECS_V5.13.md | Mesh Runtime v5.13 | Repository: ai-infra-runtime-v2*
