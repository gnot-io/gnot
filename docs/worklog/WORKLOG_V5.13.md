# Worklog — Execution Mesh v5.13
## (Key Rotation · Credential Persistence · Action Specs Cascade)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-05
**Base version:** v5.12 → v5.13
**Tests:** v5.12: 187 → v5.13: 225 (+38) — 225/225 pass

---

## 1. Scope

Implement ba feature đã được phân tích và thiết kế trong planning session:

| # | Feature | Approach được chọn |
|---|---------|-------------------|
| G1 | Key rotation an toàn | Separate `credential_encryption_key` trong config, tách khỏi `auth_token` |
| G2 | Credential persistence qua restart | Atomic JSON file với write-through cache + background flush task |
| G3 | Action specs cascade qua multi-hop | `SubRouteInfo` dataclass + `sub_route_specs` field trong registration payload |

---

## 2. Implementation

### Step 1 — `runtime/config.py`: two new fields

```python
# v5.13 — Credential store
credential_encryption_key: str | None = None
credential_store_path: str | None = None
```

`credential_encryption_key`: nếu set → dùng làm AES key root, auth_token rotation không ảnh hưởng.
`credential_store_path`: nếu set → enable JSON persistence, mặc định `None` (in-memory only = v5.12 compat).

Cả hai được parse từ `node.yaml` với default `None`.

---

### Step 2 — `runtime/credential_store.py`: full rewrite

**Breaking change:** `__init__(auth_token=)` → `__init__(encryption_key=)`.

`server.py` đã handle migration:
```python
_cred_enc_key = config.credential_encryption_key or config.auth_token
CredentialStore(encryption_key=_cred_enc_key, ...)
```
→ Backward compat: không set `credential_encryption_key` → tiếp tục dùng `auth_token` như v5.12.

**Key derivation logic:**
```python
def _derive_key(encryption_key: str | None, session_id: str) -> bytes:
    material = f"{encryption_key or 'no-auth'}:{session_id}"
    return hashlib.sha256(material.encode()).digest()
```
Không thay đổi crypto primitive, chỉ thay input. Existing ciphertext từ v5.12 sẽ không decrypt được nếu `credential_encryption_key ≠ auth_token` (intentional — operator migration path: set `credential_encryption_key = auth_token` trước để preserve sessions, rồi rotate auth_token sau).

**Persistence architecture:**

```
merge()/clear()/touch()
    ↓ dirty = True
    ↓ in-memory dict updated immediately

Background _flush_loop() [asyncio task, every 5s]
    ↓ if dirty:
    ↓     _purge_expired()
    ↓     serialize to dict
    ↓     write to .tmp file
    ↓     os.replace(.tmp, path)   ← POSIX atomic
    ↓     dirty = False
```

**Decision: 5s flush interval vs write-on-every-merge:**
Write-on-every-merge: worst case 1 write per LLM tool call (high frequency trong agent loop).
5s batch: max data loss window = 5s, acceptable vì credentials có thể re-enter.
Background task không block event loop (uses `asyncio.sleep`).

**Decision: `_purge_expired()` trước khi flush:**
Không muốn expired sessions accumulate trong file. Purge trước → file luôn clean.

**`load()` method:**
- Version check trước: version mismatch → skip toàn bộ file, log warning
- Per-entry expiry check: `expires_at <= now` → skip entry
- Decrypt error không xảy ra ở đây (ciphertext opaque — load chỉ copy bytes)
- Returns count of loaded sessions

**`stop_flush_task()`:** Cancel background task → `finally: await flush()` → guaranteed final write trước khi process exit.

**`encryption_key_fingerprint` property:**
```python
hashlib.sha256(self._enc_key.encode()).hexdigest()[:8]
```
8 hex chars → 32-bit identifier. Safe to log (không thể reverse). Operator dùng để verify key đúng sau rotation mà không cần expose key trong logs.

---

### Step 3 — `runtime/worker_agent.py`: SubRouteInfo + specs in payload

**`SubRouteInfo` dataclass:**
```python
@dataclass
class SubRouteInfo:
    actions: list[str] = field(default_factory=list)
    action_specs: dict[str, dict] = field(default_factory=dict)
```

Thay `_sub_routes: dict[str, list[str]]` thành `_sub_routes: dict[str, SubRouteInfo]`.

**`add_sub_route()` signature extended:**
```python
async def add_sub_route(
    self,
    sub_node_id: str,
    sub_actions: list[str],
    action_specs: dict[str, dict] | None = None,   # v5.13
) -> None:
```
Optional param → backward compat. Existing callers không cần update.

**`_register()` builds `sub_route_specs`:**
```python
sub_specs: dict[str, dict] = {}
for sub_id, info in sub_routes_snapshot.items():
    cap[sub_id] = info.actions
    if info.action_specs:
        sub_specs[sub_id] = info.action_specs
payload["capabilities"] = cap
if sub_specs:
    payload["sub_route_specs"] = sub_specs
```
`sub_route_specs` chỉ included khi có data → không tăng payload size với deployments không dùng specs.

**`_register_with_live_routes()` (v5.12 route withdrawal):** Updated để dùng `SubRouteInfo` type cho `live_routes` dict. Logic không thay đổi.

---

### Step 4 — `runtime/models.py`: NodeRegistrationRequest.sub_route_specs

```python
sub_route_specs: dict[str, dict] = Field(default_factory=dict)
# {sub_node_id: {action_name: ActionSpec dict}}
```
Thêm vào `NodeRegistrationRequest`. Default empty dict → backward compat với clients cũ không gửi field.

---

### Step 5 — `runtime/node_registry.py`: register() nhận sub_route_specs

```python
async def register(
    self, ...,
    sub_route_specs: dict[str, dict] | None = None,   # v5.13
) -> None:
```

Trong sub-route install loop:
```python
sub_specs = (sub_route_specs or {}).get(sub_id, {})
if sub_existing:
    if sub_specs:
        sub_existing.action_specs = dict(sub_specs)
else:
    _entries[sub_id] = _NodeEntry(action_specs=dict(sub_specs), ...)
```

Logic: nếu `sub_specs` empty dict → không overwrite existing specs (tránh regression khi re-register không kèm specs). Nếu có specs → always update (latest advertiser wins).

---

### Step 6 — `runtime/server.py`: wire everything

**(A) CredentialStore:**
```python
_cred_enc_key = config.credential_encryption_key or config.auth_token
credential_store = CredentialStore(
    encryption_key=_cred_enc_key,
    ttl_seconds=config.session_ttl_seconds,
    store_path=config.credential_store_path,
)
```

**(B) Lifespan:**
```python
# startup:
_cred_sessions = await credential_store.load()
credential_store.start_flush_task()

# shutdown:
await credential_store.stop_flush_task()
```

**(C) `/nodes/register` sub_route_specs:**
```python
# → node_registry.register()
sub_route_specs=getattr(req, 'sub_route_specs', None) or None

# → agent.add_sub_route()
_sub_node_specs = req.sub_route_specs.get(advertised_id)
if advertised_id == req.node_id:
    _sub_node_specs = _sub_node_specs or req.action_specs or None
await agent.add_sub_route(advertised_id, advertised_actions, action_specs=_sub_node_specs)
```

**(D) Version bump:** `5.12.0` → `5.13.0`

---

### Step 7 — Fix backward-compat breaks

**v5.10 tests:** `_sub_routes["node-1a"] == ["get_order_info"]`
→ `_sub_routes["node-1a"].actions == ["get_order_info"]`

Một test còn direct dict assignment:
`agent._sub_routes["node-1a"] = ["get_order_info"]`
→ `agent._sub_routes["node-1a"] = SubRouteInfo(actions=["get_order_info"])`

**v5.12 tests:** `CredentialStore(auth_token="sk-node")`
→ `CredentialStore(encryption_key="sk-node")`

---

## 3. Tests

| Class | Tests | Covers |
|-------|-------|--------|
| `TestSeparateEncryptionKey` | 6 | encryption_key used; None fallback; different keys → different ct; fingerprint; wrong key → silently empty |
| `TestKeyRotationIsolation` | 3 | auth_token change (v5.13: no effect); enc_key change (intentional invalidation); new merges after rekey |
| `TestCredentialPersistence` | 12 | flush creates file; valid JSON; load restores; expired skipped; missing file ok; wrong version ok; atomic (.tmp gone); not-dirty returns false; in-memory returns false; clear marks dirty; flush purges expired; multi-session isolation |
| `TestFlushTask` | 4 | no-op without store_path; starts with path; stop does final flush; double-start idempotent |
| `TestConfigV513` | 5 | enc key parsed; store path parsed; defaults None; server uses enc key over auth_token; fallback to auth_token |
| `TestActionSpecsCascade` | 4 | add_sub_route stores specs; without specs stores empty; _register payload includes specs; omits sub_route_specs when empty |
| `TestSpecsCascadeEndToEnd` | 4 | sub_route_specs in NodeRegistry after HTTP register; direct node specs in caps; backward compat no specs; credential_store_path config enables persistence |
| **Tổng mới** | **38** | |

**v5.12: 187 tests → v5.13: 225 tests (+38) — 225/225 pass**

---

## 4. Decisions Log

### D1: Tại sao không re-encrypt khi enc_key thay đổi?

Re-encrypt-on-key-change cần:
1. Detect khi nào key thay đổi (so sánh với stored fingerprint)
2. Decrypt all entries với old key
3. Re-encrypt với new key
4. Handle partial failures (decrypt OK nhưng re-encrypt fail)

Phức tạp và error-prone. Tradeoff hiện tại: key change = intentional credential invalidation.

**Recommended migration path cho operator:**
```
# Khi muốn rotate credential_encryption_key mà không mất sessions:
# 1. Set credential_encryption_key = old_auth_token (no-op key change)
# 2. Wait 1 session TTL (all sessions re-enter credentials)
# 3. Rotate credential_encryption_key sang giá trị mới
```

Nếu re-encryption thực sự cần thiết trong tương lai → implement `CredentialStore.rekey(old_key, new_key)` method riêng, gọi explicit.

### D2: Tại sao `os.replace()` thay vì `Path.rename()`?

`os.replace()` ghi đè destination nếu tồn tại — atomic trên POSIX.
`Path.rename()` raises `FileExistsError` trên Windows nếu destination tồn tại.

Mesh hiện tại chạy trên Linux (Ubuntu 24) nên `os.replace()` và `Path.rename()` equivalent. Dùng `os.replace()` để explicit về intent và forward-compat nếu có Windows support sau này.

### D3: `sub_route_specs` vs extend `capabilities` field

Option A: Extend `capabilities: dict[str, list[str]]` → `dict[str, NodeCapInfo]` với `NodeCapInfo = {actions, specs}`. Breaking change cho tất cả clients.

Option B: Thêm `sub_route_specs: dict[str, dict]` field song song. Clients cũ không gửi field → default `{}` → backward compat. Clients mới gửi thêm field → specs được apply.

Chọn Option B — additive, zero breaking change cho protocol.

### D4: Tại sao `if sub_specs:` trước khi overwrite `action_specs`?

Scenario: node-1a có specs. node-1 re-registers vì heartbeat (không kèm sub_route_specs). Nếu overwrite với empty → specs bị mất mỗi heartbeat cycle.

Fix: chỉ update `action_specs` khi `sub_specs` non-empty. Latest non-empty wins.

---

## 5. Remaining Items (cập nhật)

| Priority | Item | Status |
|----------|------|--------|
| ✅ | Key rotation safe | Done v5.13 |
| ✅ | Credential persistence | Done v5.13 |
| ✅ | Action specs cascade | Done v5.13 |
| P4 | Credential re-encryption on key change | Future — operator migration path documented |
| P4 | Explicit WITHDRAW trước khi graceful shutdown | Future |
| P3 | Job queue persistence (SQLite) | Deferred |
| P3 | Streaming intent response (SSE) | Deferred |
| P3 | Container isolation | Deferred |
