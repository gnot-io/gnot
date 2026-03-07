# Worklog — Execution Mesh v5.12
## (Route Withdrawal · Result Forwarding Chain · Session Credential Storage · Credential Encryption)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-05
**Base version:** v5.11 → v5.12

---

## 1. Scope & Phân tích

### 1.1 Bốn vấn đề cần giải quyết

| # | Vấn đề | Impact | Root cause |
|---|--------|--------|------------|
| G1 | Stale routes khi node chết | LLM gọi dead node → 5min timeout | NodeRegistry không có route removal mechanism |
| G2 | Pull-mode multi-hop trả kết quả sai | Caller nhận `{forwarded: true}` thay vì real output | `_handle_mesh_forward` không poll downstream job |
| G3 | User phải gửi credentials mỗi request | UX kém, không thể dùng với Telegram bot | IntentHandler không persist credentials vào session |
| G4 | Credentials plaintext in-memory | Security risk nếu session dump / debug | Chưa có encryption layer |

### 1.2 Approach đã chọn (từ design session)

- G1: **Heartbeat cascade** — node-1 định kỳ re-register với advertise_routes filtered to ONLINE only. Gateway reconcile → withdraw stale routes. Không cần new endpoint.
- G2: **Poll-and-relay** — `_handle_mesh_forward` block (async) cho đến khi downstream job complete, relay real result. Reuse `_await_local_job` pattern đã có.
- G3: **Separate CredentialStore** — không lẫn vào session.messages, có TTL đồng bộ session.
- G4: **AES-256-GCM** với key derive từ `auth_token + session_id`. No KMS, no external deps ngoài `cryptography`.

---

## 2. Implementation

### Step 1 — `runtime/credential_store.py` (new file)

**Module đứng độc lập.** Không import từ `runtime.models` hay các module khác — tránh circular import.

Crypto functions (`_derive_key`, `_encrypt`, `_decrypt`) là module-level, testable riêng lẻ.

```python
def _derive_key(auth_token: str | None, session_id: str) -> bytes:
    material = f"{auth_token or 'no-auth'}:{session_id}"
    return hashlib.sha256(material.encode()).digest()  # 32 bytes

def _encrypt(plaintext: str, key: bytes) -> str:
    nonce = os.urandom(12)  # fresh nonce per call
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()

def _decrypt(token: str, key: bytes) -> str:
    raw = base64.b64decode(token)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ct, None).decode()
```

**Decision: Fallback mode khi không có `cryptography`:**
Dùng `"b64:" + base64(plaintext)` thay vì raise error. Lý do: Không muốn block startup
nếu operator chưa install package. Warning logged. Production operator sẽ install đúng.

**Decision: Key derive dùng SHA-256 thay vì PBKDF2:**
PBKDF2 là password hashing — slow by design để chống offline attack. Ở đây:
- Input (`auth_token`) không phải user password — đã có entropy cao
- Không có offline attack vector (attacker cần cả ciphertext + derived key)
- SHA-256 nhanh, không cần iterate
→ PBKDF2 là over-engineering ở đây.

**CredentialStore internal structure:**
```python
_store: dict[str, {
    "expires_at": float,
    "creds": {credential_key: encrypted_value}
}]
```
Intentionally không dùng `dataclass` hay `pydantic` cho internal entry — đơn giản hơn,
không cần serialize/deserialize, không bao giờ expose ra ngoài class.

### Step 2 — `runtime/models.py`: CapabilityNode.status

Thêm `status: str = "unknown"`. Default `"unknown"` thay vì `None` để:
- Luôn có giá trị valid để serialize (JSON không cần nullable handling)
- Consumer code không cần null check
- Consistent với NodeStatus enum values (`.value` trả `str`)

### Step 3 — `runtime/node_registry.py`: withdraw_routes + status in tree

**`withdraw_routes(via_node_id, keep_routes)`:**
```python
async with self._lock:
    to_remove = [
        nid for nid, entry in self._entries.items()
        if entry.next_hop == via_node_id and nid not in keep_routes
    ]
    for nid in to_remove:
        del self._entries[nid]
```

**Decision: Xóa hẳn vs đánh dấu WITHDRAWN:**
Nếu đánh dấu (WITHDRAWN status), entry vẫn tồn tại → build_capability_tree vẫn phải
filter → complexity. Xóa hẳn là cleaner. Nếu node-1a comes back online → nó sẽ
re-register → entry mới được tạo. Không mất gì.

**build_capability_tree với status:**
```python
self._apply_lazy_staleness(entry)  # check heartbeat age trước khi render
cap = CapabilityNode(
    ...
    status=entry.status.value,
)
```
`_apply_lazy_staleness` đã có từ v5.7 — dùng lại, không viết mới.

### Step 4 — `runtime/worker_agent.py`: 3 changes

**(A) `_reregister_loop()` + `_register_with_live_routes()`:**

Chạy như task 3 trong `start()`. Interval = `heartbeat_interval × RE_REGISTER_INTERVAL_MULTIPLIER`.
Với default 10s heartbeat → re-register mỗi 60s.

`_register_with_live_routes()` consults `self._local_node_registry` (injected từ `node_runtime.py`
sau startup). Nếu registry không inject được → safe fallback: advertise đầy đủ (không filter) →
không fail, chỉ không có withdrawal.

**Decision: Tại sao không check directly trong `_register()`?**
`_register()` được gọi cả trong retry (startup) và khi sub-node mới join (add_sub_route).
Không muốn filter trong những paths đó — khi startup chưa có heartbeat data, khi sub-node
mới join thì nó chắc chắn ONLINE. Tách `_register_with_live_routes()` riêng = clean separation.

**(B) `_await_downstream_job(job_id)`:**
Poll `GET {self_url}/result/{job_id}` — dùng HTTP đến self (node-1's gateway) thay vì
in-process JobManager lookup. Lý do: downstream job `j2` được tạo bởi node-1's
GatewayRouter, store trong node-1's JobManager → accessible qua `/result/{job_id}`.
Approach này consistent với cách caller poll result thông thường.

**(C) `_handle_mesh_forward()` poll-and-relay:**
```python
# Trước (v5.11):
return SyncActionResponse(output={"forwarded": True, "downstream_job_id": data.get("job_id")})

# Sau (v5.12):
if "job_id" in data:
    output, error = await self._await_downstream_job(data["job_id"])
    if error:
        raise RuntimeError(f"Downstream job failed: {error}")
    return SyncActionResponse(output=output or {})
```

**Tại sao không block event loop?**
`_await_downstream_job` dùng `await asyncio.sleep(poll_interval)` — yields control mỗi giây.
Node-1 tiếp tục xử lý other coroutines trong lúc chờ.

**Failure propagation:** Nếu downstream fail → raise RuntimeError → `_claim_and_execute`
catch và report job `j1` as FAILED với error message. Đúng semantics.

### Step 5 — `runtime/intent_handler.py`: credential_store integration

Constructor thêm `credential_store: CredentialStore | None = None`.

Trong `handle()`:
```python
caller_credentials = dict(req.caller_credentials)

if self._credential_store is not None:
    if req.caller_credentials:
        await self._credential_store.merge(session.session_id, req.caller_credentials)
    stored_creds = await self._credential_store.get(session.session_id)
    # Request creds take precedence (allow update)
    caller_credentials = {**stored_creds, **caller_credentials}
    await self._credential_store.touch(session.session_id)
```

**Decision: Merge order `{**stored, **request}` (request wins):**
User có thể cần update key (expired CRM token). Nếu stored wins → update không có tác dụng.
Request wins = user luôn có thể override. Updated value tự động được persist vào store qua `merge`.

**TTL touch:** Mỗi active turn extend TTL. Credentials expire cùng lúc với inactivity timeout,
không cần separate TTL management.

### Step 6 — `runtime/server.py`: 3 wiring changes

**(A) CredentialStore creation:**
```python
credential_store = CredentialStore(
    auth_token=config.auth_token,
    ttl_seconds=config.session_ttl_seconds,
)
```
TTL đồng bộ với ConversationStore — consistent expiry.

**(B) withdraw_routes trong `/nodes/register`:**
```python
withdrawn = await node_registry.withdraw_routes(
    via_node_id=req.node_id,
    keep_routes=req.advertise_routes,
)
```
Chỉ gọi nếu `req.advertise_routes is not None` — backward compat với clients không gửi field.

**(C) `app.state.node_registry`:**
Expose để `node_runtime.py` inject vào WorkerAgent sau startup.
Dùng `app.state` (FastAPI's state mechanism) thay vì pass trực tiếp — consistent với
cách `worker_agent_ref` đã được expose.

### Step 7 — `mesh/node_runtime.py`: inject local NodeRegistry

```python
if hasattr(app.state, "node_registry"):
    agent._local_node_registry = app.state.node_registry
```

Dynamic attribute injection (không trong `__init__`) để:
1. WorkerAgent không depend on NodeRegistry (worker không phải gateway)
2. Only worker nodes that ARE also partial gateways (have sub-nodes) need this
3. Backward compatible — code checks `getattr(self, "_local_node_registry", None)`

---

## 3. Tests

| Class | Tests | Covers |
|-------|-------|--------|
| `TestCredentialEncryption` | 9 | Key derivation, roundtrip, nonce randomness, wrong-key error, plaintext not in ciphertext |
| `TestCredentialStore` | 12 | merge/get/clear, TTL expiry, touch extends TTL, session isolation, purge_expired, merge empty noop, ciphertext stored (not plaintext) |
| `TestCredentialStoreIntegration` | 3 | Credentials stored on first turn; retrieved on subsequent turn (no creds in request); request overrides stored |
| `TestRouteWithdrawal` | 5 | withdraw removes stale route; empty keep removes all via-node routes; other nodes unaffected; no-op when all kept; direct nodes not withdrawn |
| `TestStatusInCapabilityTree` | 4 | Online status; unknown (no heartbeat); unreachable (stale heartbeat); unreachable shown in system prompt |
| `TestResultForwardingChain` | 3 | Sync passthrough; async poll-and-relay returns real result; downstream failure raises |
| `TestEndToEndV512` | 3 | Capabilities include status; route withdrawal via re-register HTTP; /intent accepts caller_credentials |
| **Tổng mới** | **39** | |

**v5.11: 148 tests → v5.12: 187 tests (+39) — 187/187 pass**

---

## 4. Decisions Log

### D1: Tại sao không dùng explicit WITHDRAW endpoint?

BGP có WITHDRAW message — node gửi explicit "I'm removing route X".

Với explicit approach: node-1a graceful shutdown → gửi `DELETE /nodes/register` → node-1
xóa entry → node-1 gửi withdrawal upward. Clean và immediate.

**Rejected vì:** Node crash không graceful. Node-1a crash không gửi được withdrawal.
Vẫn cần fallback timeout-based detection. Khi đã có fallback, explicit withdrawal chỉ
là optimization — giảm lag từ 60s xuống ~0s khi graceful. Không đủ value để thêm
endpoint mới + change shutdown lifecycle.

Heartbeat cascade đủ tốt: max lag = `heartbeat_interval × 6 = 60s`. Trong 60s đó,
caller sẽ timeout với job của họ (5min) — không thêm latency, chỉ là route entry stale
thêm chút thôi.

### D2: `_await_downstream_job` dùng HTTP thay vì in-process JobManager

Hai cách tiếp cận:
1. Direct JobManager lookup: `self._executor._job_manager.get_job(j2)`
2. HTTP GET: `GET {self_url}/result/j2`

Approach 1 dùng internal reference `_executor._job_manager` — coupling với executor internals.
Nhưng `j2` không được tạo bởi executor — nó được tạo bởi GatewayRouter (khi node-1 đóng
vai intermediate gateway). GatewayRouter có JobManager riêng.

Approach 2 consistent với cách caller bình thường poll result. `/result/{job_id}` trên
node-1 sẽ lookup trong cả local JobManager và forward cho worker nếu cần (existing logic).

### D3: Tại sao session_id làm authentication data cho credential store?

`session_id` là UUID4 auto-generated (48 bits entropy hex). Nếu attacker guess được
session_id → họ có thể request credentials cho session đó qua `/intent`.

Nhưng: để lấy được credentials từ `CredentialStore`, cần call `credential_store.get(session_id)`
— đây là server-side operation, không exposed qua any API. `GET /sessions/{id}` chỉ trả
messages, không trả credentials.

Vậy: session_id là internal routing key, không phải authentication factor. Encryption
với session_id trong key material là để đảm bảo different sessions có different keys —
thêm per-session isolation, không phải security boundary.

### D4: Tại sao không encrypt session messages?

Session messages (`ConversationStore`) chứa plain conversation text — nội dung user
gõ. Không có sensitive credentials ở đó (credentials không bao giờ được thêm vào messages).

`GET /sessions/{id}` intentionally expose messages (operator monitoring, debugging).
Encrypting messages sẽ break endpoint này. Ngoài ra messages không có value bảo mật cao —
they're conversation text, not secrets.

**Rule:** Chỉ encrypt data thực sự secret. Messages = không secret. Credentials = secret.

---

## 5. Remaining Items (cập nhật)

| Priority | Item | Status | Ghi chú |
|----------|------|--------|---------|
| ✅ | Route withdrawal | Done v5.12 | Heartbeat cascade |
| ✅ | Result forwarding chain | Done v5.12 | Poll-and-relay |
| ✅ | Session credential storage | Done v5.12 | CredentialStore |
| ✅ | Credential encryption | Done v5.12 | AES-256-GCM |
| P3 | Key rotation | Future | auth_token change invalidates all sessions |
| P3 | Credential store persistence | Future | Restart clears credentials |
| P3 | Graceful shutdown withdrawal | Future | node sends explicit WITHDRAW before exit |
| P3 | Route advertisement cascade with action_specs | Future | specs not carried through multi-hop re-advertisement |
| P3 | Job queue persistence | Deferred | SQLite backend |
| P3 | Streaming intent response | Deferred | SSE |
| P2 | Container isolation | Deferred | non-root + ulimit |
