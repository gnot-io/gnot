# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v5.12
### (Route Withdrawal · Result Forwarding Chain · Session Credential Storage · Credential Encryption)

---

## 1. Vấn đề giải quyết trong v5.12

### 1.1 Gap từ v5.11

**G1 — Stale routes khi node disconnect:**
Khi `node-1a` chết, `node-0` không biết. Entry trong NodeRegistry vẫn tồn tại với
status ONLINE. LLM đọc `GET /capabilities` → thấy `node-1a` khả dụng → gọi action →
job được enqueue → timeout sau 300s → caller chờ 5 phút mới biết lỗi.

**G2 — Pull-mode multi-hop trả kết quả sai:**
Khi cả node-1 và node-1a đều là pull-only (NAT), `_handle_mesh_forward` trên node-1
nhận `AsyncActionResponse{job_id: "j2"}` từ downstream nhưng không chờ kết quả thực.
Trả về ngay `{forwarded: true, downstream_job_id: "j2"}`. Node-0 mark job `j1` là
COMPLETED với output vô nghĩa. IntentHandler nhận `{forwarded: true}` thay vì kết quả.

**G3 — User phải cung cấp credentials mỗi request:**
v5.11 lưu `caller_credentials` trong `IntentRequest` nhưng không persist vào session.
Mỗi lần gọi qua POST /intent, caller phải gửi lại key. Telegram bot không cache được
credentials giữa các turns.

**G4 — Credentials in-memory dạng plaintext:**
Nếu lưu credentials vào Session (ConversationStore), chúng xuất hiện plaintext trong
`session.messages`, bị expose qua `GET /sessions/{id}`, bị dump trong debug logs.

---

## 2. Kiến trúc giải pháp

### 2.1 Route Withdrawal (BGP WITHDRAW Cascade)

**Cơ chế:** Node cấp trung gian (node-1) định kỳ re-register với gateway (node-0).
Trong mỗi lần re-register, chỉ advertise các sub-nodes đang ONLINE. Gateway so sánh
advertisement mới với cũ → các route không còn xuất hiện → withdraw.

```
Timeline:
  T=0:   node-1a running, heartbeat to node-1 every 10s
  T=30:  node-1a crashes
  T=60:  node-1 lazy-check: node-1a heartbeat stale → mark UNREACHABLE
  T=60:  node-1 periodic re-registration (every 60s = 6×heartbeat_interval)
            → advertise_routes: ["node-1b"]   (node-1a excluded)
            → POST /nodes/register to node-0
  T=60:  node-0 withdraw_routes("node-1", keep=["node-1b"])
            → removes "node-1a" from _entries
  T=60+: GET /capabilities: node-1a GONE ✓
         LLM sees node-1a GONE → không cố gọi
```

**Hai thành phần:**

**(A) `NodeRegistry.withdraw_routes(via_node_id, keep_routes)`:**
```python
async def withdraw_routes(self, via_node_id: str, keep_routes: list[str]) -> list[str]:
    # Remove entries where next_hop == via_node_id AND not in keep_routes
    withdrawn = [nid for nid, e in _entries.items()
                 if e.next_hop == via_node_id and nid not in keep_routes]
    for nid in withdrawn:
        del _entries[nid]
    return withdrawn
```
Called by `/nodes/register` handler mỗi lần receive registration.

**(B) `WorkerAgent._reregister_loop()`:**
```python
# Runs every heartbeat_interval × RE_REGISTER_INTERVAL_MULTIPLIER (default 60s)
async def _reregister_loop(self):
    while running:
        await sleep(heartbeat_interval * 6)
        await _register_with_live_routes()

async def _register_with_live_routes(self):
    # Filter sub_routes to ONLINE-only (lazy staleness check via local NodeRegistry)
    live = {id: actions for id, actions in sub_routes.items()
            if not local_registry.status(id) == UNREACHABLE}
    # Re-register with filtered list → gateway's withdraw_routes() handles the rest
    await _register(advertise_routes=list(live.keys()))
```

**Status trong capability tree:**
`build_capability_tree()` gọi `_apply_lazy_staleness()` cho mỗi entry trước khi render.
`CapabilityNode.status` field mới → exposed qua `GET /capabilities`.

**System prompt:**
```
  - node-1a via node-1 [UNREACHABLE — do not call]
    ┌─ get_order_info: Retrieve order details
```
LLM thấy marker → tự skip node đó.

---

### 2.2 Result Forwarding Chain (Poll-and-Relay)

**Vấn đề cũ:**
```
node-0 → [pull j1] → node-1 → _handle_mesh_forward()
  → POST node-1/action {target: node-1a}
  → node-1 router: enqueue j2 for node-1a
  → returns AsyncActionResponse{job_id: "j2"}
  → _handle_mesh_forward returns SyncActionResponse{output: {forwarded: true}}  ← WRONG
node-0: j1 = COMPLETED with {forwarded: true}   ← WRONG
```

**Fix — Poll-and-Relay:**
```python
async def _handle_mesh_forward(self, job):
    data = await self._post_to_local_action(original_target, original_action, params)

    if "output" in data and "job_id" not in data:
        # Sync result — pass through directly (unchanged)
        return SyncActionResponse(output=data["output"])

    # v5.12: Async downstream — poll until real result, then relay
    if "job_id" in data:
        output, error = await self._await_downstream_job(data["job_id"])
        if error:
            raise RuntimeError(f"Downstream job failed: {error}")
        return SyncActionResponse(output=output or {})
```

`_await_downstream_job(job_id)`:
- Polls `GET {self_url}/result/{job_id}` (node-1's own gateway endpoint)
- Backoff: 1s per poll
- Max wait: `pull_job_timeout_seconds` (default 300s)
- Returns `(output, error)` tuple

**Kết quả sau fix:**
```
node-0 → [pull j1] → node-1 → _handle_mesh_forward()
  → POST node-1/action → enqueue j2 for node-1a
  → _await_downstream_job("j2")
      → poll j2 until COMPLETED
      → got {"result": "real_data"}
  → returns SyncActionResponse{output: {"result": "real_data"}}  ← CORRECT
node-0: j1 = COMPLETED with {"result": "real_data"}              ← CORRECT
```

**Non-blocking:** `_await_downstream_job` là `async` — dùng `await asyncio.sleep()` nên
không block event loop. Node-1 vẫn xử lý heartbeats và other jobs trong lúc chờ.

---

### 2.3 Session Credential Storage

**Design:** `CredentialStore` — class riêng biệt, hoàn toàn tách khỏi `ConversationStore`.

```
ConversationStore: { session_id → Session{messages: [...]} }
CredentialStore:   { session_id → {expires_at, creds: {key: encrypted_value}} }
```

Credentials **không bao giờ** xuất hiện trong `session.messages`.

**Lifecycle trong IntentHandler.handle():**
```python
async def handle(self, req: IntentRequest):
    session = await store.get_or_create(req.session_id)
    caller_credentials = dict(req.caller_credentials)

    if credential_store:
        # 1. Persist new credentials from this request
        if req.caller_credentials:
            await credential_store.merge(session.session_id, req.caller_credentials)
        # 2. Load all stored credentials for this session
        stored = await credential_store.get(session.session_id)
        # 3. Merge: request creds take precedence (allow update)
        caller_credentials = {**stored, **caller_credentials}
        # 4. Extend TTL on activity
        await credential_store.touch(session.session_id)

    # caller_credentials now contains: stored + request creds
    # Forward to every tool call in the loop
```

**UX flow:**
```
Turn 1: POST /intent {prompt: "Xem order #123", caller_credentials: {crm_user_token: "sk-abc"}}
  → merge("sess-1", {crm_user_token: "sk-abc"}) → encrypted at rest
  → action gọi với crm_user_token

Turn 2: POST /intent {prompt: "Xem order #456"}   ← KHÔNG cần gửi lại key
  → get("sess-1") → {crm_user_token: "sk-abc"} decrypted
  → action gọi với crm_user_token ✓

Turn 3: POST /intent {prompt: "Update key", caller_credentials: {crm_user_token: "sk-new"}}
  → merge overwrites old value
  → action gọi với sk-new ✓
```

---

### 2.4 Credential Encryption (AES-256-GCM)

**Key derivation:**
```python
key = SHA-256(auth_token + ":" + session_id)  # 32 bytes = AES-256
```
- Deterministic: same inputs → same key
- Per-session: different session_id → different key → compromising one session ≠ all sessions
- Uses node's `auth_token` as root secret (already exists in config)

**Encryption:**
```python
nonce = os.urandom(12)          # 12 bytes, GCM standard, fresh per call
ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
stored = base64.b64encode(nonce + ct).decode()  # nonce || ciphertext+tag
```
- AEAD: tampering detected via authentication tag (16 bytes appended by GCM)
- Same plaintext → different ciphertext (nonce randomness)

**Stored format:**
```
base64( nonce[12 bytes] || ciphertext || GCM_tag[16 bytes] )
```

**Decryption:**
```python
raw = base64.b64decode(stored)
nonce, ct = raw[:12], raw[12:]
plaintext = AESGCM(key).decrypt(nonce, ct, None).decode()
```

**Fallback (no `cryptography` package):**
```
stored = "b64:" + base64.b64encode(plaintext)
```
Not encrypted but at least separated from messages. Warning logged.

**Security properties:**

| Property | Status |
|----------|--------|
| Confidentiality at rest | ✓ AES-256-GCM |
| Integrity / anti-tampering | ✓ GCM auth tag |
| Per-session isolation | ✓ session_id in key material |
| Not in conversation history | ✓ Separate store |
| Not in GET /sessions response | ✓ Never serialized to messages |
| Not logged | ✓ Logging only shows key names, not values |

**Limitations (known, acceptable for v5.12):**
- Key rotation not supported: changing auth_token invalidates all sessions
- If auth_token compromised: all session credentials compromised
- In-memory only: process restart clears all credentials
- No KMS / HSM integration (out of scope)

---

## 3. New APIs & Models

### 3.1 CapabilityNode (extended)

```python
class CapabilityNode(BaseModel):
    node_id: str
    actions: list[str]
    action_specs: dict[str, ActionSpec] = {}
    next_hop: str | None = None
    status: str = "unknown"           # v5.12: "online" | "unreachable" | "unknown"
    reachable: dict[str, CapabilityNode] = {}
```

### 3.2 GET /capabilities — status field example

```json
{
  "node_id": "node-0",
  "actions": ["execute_command"],
  "reachable": {
    "node-1": {
      "node_id": "node-1",
      "actions": ["read_file"],
      "status": "online",
      "next_hop": null
    },
    "node-1a": {
      "node_id": "node-1a",
      "actions": ["get_order_info"],
      "status": "unreachable",
      "next_hop": "node-1"
    }
  }
}
```

### 3.3 CredentialStore (new module)

```python
class CredentialStore:
    def __init__(self, auth_token: str | None, ttl_seconds: int)

    async def merge(session_id: str, credentials: dict[str, str]) -> None
    # Upsert credentials. Existing keys overwritten, others kept.

    async def get(session_id: str) -> dict[str, str]
    # Return all plaintext credentials. {} if not found or expired.

    async def touch(session_id: str) -> None
    # Extend TTL on session activity.

    async def clear(session_id: str) -> None
    # Remove all credentials for session (e.g. on logout).

    async def purge_expired() -> int
    # Remove expired entries. Returns count.
```

### 3.4 NodeRegistry — new method

```python
async def withdraw_routes(
    self,
    via_node_id: str,
    keep_routes: list[str],
) -> list[str]:
    """Remove indirect routes advertised via via_node_id that are not in keep_routes.
    Returns list of withdrawn node_ids."""
```

### 3.5 WorkerAgent — new methods

```python
async def _reregister_loop(self) -> None:
    # Runs every heartbeat_interval × 6 seconds (default: 60s)
    # Calls _register_with_live_routes()

async def _register_with_live_routes(self) -> None:
    # Re-register advertising only ONLINE sub-routes
    # Triggers withdraw_routes() on gateway

async def _await_downstream_job(
    self,
    job_id: str,
    poll_interval: float = 1.0,
    max_wait: float | None = None,
) -> tuple[dict | None, str | None]:
    # Poll local gateway's /result/{job_id} until done
    # Returns (output, error)
```

---

## 4. System Prompt — v5.12 changes

Các nodes UNREACHABLE được đánh dấu rõ ràng:

```
## Mesh topology — nodes, actions, and requirements
  - node-0 [gateway, THIS NODE]
    ┌─ execute_command: Execute a shell command
  - node-1 (direct)
    ┌─ read_file: Read file content
  - node-1a via node-1 [UNREACHABLE — do not call]
    ┌─ get_order_info: Retrieve order details
    │  ⚠ caller_credential crm_user_token (required): CRM API key
```

LLM đọc marker `[UNREACHABLE — do not call]` → skip node đó, tìm alternative hoặc
report không khả dụng. Không cố gọi action rồi chờ timeout 5 phút.

---

## 5. Files thay đổi

| File | Loại | Thay đổi chính |
|------|------|----------------|
| `runtime/credential_store.py` | **New** | `CredentialStore` class, AES-256-GCM encrypt/decrypt, TTL, merge/get/touch/clear/purge |
| `runtime/models.py` | Modified | `CapabilityNode.status: str = "unknown"` |
| `runtime/node_registry.py` | Modified | `withdraw_routes()` method; `build_capability_tree()` calls `_apply_lazy_staleness` + sets `status` |
| `runtime/worker_agent.py` | Modified | `_reregister_loop()`, `_register_with_live_routes()`, `_await_downstream_job()`; `_handle_mesh_forward()` poll-and-relay fix; `RE_REGISTER_INTERVAL_MULTIPLIER = 6` |
| `runtime/intent_handler.py` | Modified | Constructor: `credential_store` param; `handle()`: merge + retrieve credentials from store |
| `runtime/server.py` | Modified | Create `CredentialStore`; pass to `IntentHandler`; wire `withdraw_routes()` in `/nodes/register`; expose `node_registry` on `app.state`; version `5.12.0` |
| `mesh/node_runtime.py` | Modified | Inject `_local_node_registry` into `WorkerAgent` after startup |
| `tests/test_v512_features.py` | **New** | 39 tests |
| `docs/SPECS_V5.12.md` | **New** | Document này |
| `docs/WORKLOG_V5.12.md` | **New** | Implementation worklog |

---

## 6. Trạng thái hệ thống (v5.12)

**Đã có:**
- ✅ Bootstrap minimal + plugin actions (v5.0)
- ✅ Async job model (v5.0)
- ✅ Distributed routing push/pull/NAT (v5.3–v5.6)
- ✅ Authentication Bearer token (v5.3)
- ✅ Action schema validation (v5.3)
- ✅ NodeRegistry + WorkerAgent + JobQueue (v5.3)
- ✅ Claude Web curl-native (v5.6)
- ✅ Lazy staleness + pull job timeout (v5.7)
- ✅ File upload/download (v5.8)
- ✅ POST /intent — ReAct agent loop (v5.9)
- ✅ BGP-style route advertisement + multi-hop (v5.10)
- ✅ GET /capabilities — capability tree (v5.10)
- ✅ Action schema exposure — description + params (v5.11)
- ✅ Caller authorization — per-token action allowlist (v5.11)
- ✅ Caller credential delivery — secure, not logged (v5.11)
- ✅ **Route withdrawal — BGP WITHDRAW cascade (v5.12)**
- ✅ **Node status in capability tree (v5.12)**
- ✅ **UNREACHABLE marker in LLM system prompt (v5.12)**
- ✅ **Result forwarding chain — poll-and-relay (v5.12)**
- ✅ **Session credential storage — persistent across turns (v5.12)**
- ✅ **Credential encryption — AES-256-GCM at rest (v5.12)**

**Chưa có:**
- ❌ Key rotation for credential store (auth_token change invalidates all sessions)
- ❌ Credential store persistence (restart clears all)
- ❌ Route advertisement with spec (action_specs not carried through cascade)
- ❌ Job queue persistence (SQLite)
- ❌ Streaming intent response / SSE
- ❌ Container isolation

---

*Spec: SPECS_V5.12.md | Mesh Runtime v5.12 | Repository: ai-infra-runtime-v2*
