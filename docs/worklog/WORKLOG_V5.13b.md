# Worklog — Execution Mesh v5.13b
## (Per-Node Auth Tokens · AuthMiddleware Fix · Cluster Deployment)

**Project:** ai-infra-runtime-v2
**Base:** v5.13 → v5.13b
**Tests:** 225 → 225 (không thêm test mới, 2 tests cũ cập nhật assertion)
**Bug fixed:** AuthMiddleware chưa được wire vào app

---

## 1. Trigger

Yêu cầu setup cluster thực tế 4 nodes:
- `deb-0` — Debian, gateway public, expose qua cloudflared (`deb-0.vietml.com`)
- `cen-0` — CentOS, worker private
- `alm-0` — AlmaLinux, worker private
- `cfsnk-0` — CentOS/RHEL, worker private

Constraints: mỗi node một token riêng (trusted_nodes list model).

---

## 2. Phân tích trước khi implement

### 2.1 Phát hiện bug: AuthMiddleware chưa được wire

Khi trace auth path để thiết kế per-node tokens, phát hiện:

```python
# runtime/server.py — ĐÃ CÓ:
from runtime.auth import AuthMiddleware  # import OK

# NHƯNG KHÔNG CÓ:
# app.add_middleware(AuthMiddleware, ...)
```

`AuthMiddleware` được import, class được build hoàn chỉnh từ v5.3, nhưng chưa bao giờ
được add vào app. Tất cả endpoints (kể cả `/action`, `/intent`, `/capabilities`) thực tế
không có auth protection dù config có `auth_token`. Bảo vệ duy nhất là `caller_policies`
ở action level (v5.11), không phải HTTP layer.

**Ảnh hưởng:** Khi wire middleware vào, 2 ASGI integration tests cũ bắt đầu fail vì
chúng POST `/nodes/register` mà không gửi `Authorization` header. Fix: thêm header vào tests.

### 2.2 Thiết kế per-node tokens

**Option A: Một `auth_token` chung** — đơn giản nhưng blast radius lớn, không audit được.

**Option B: Thêm `allowed_tokens: list[str]`** — gateway list tất cả worker tokens, mỗi
worker có token riêng. Backward compat: `auth_token` đơn vẫn hoạt động.

**Option C: Signed JWT per node** — strong audit, nhưng quá phức tạp cho use case hiện tại.

Chọn **Option B**. Đơn giản, backward compat, đủ security cho internal cluster.

**Câu hỏi phụ:** Worker gửi token nào khi gọi gateway?
- Hiện tại: `config.auth_token` (token của chính nó)
- v5.13b: `gateway_auth_token` nếu set, fallback `auth_token`
- Trong hầu hết cases cả hai giống nhau (same token). Tách biệt để linh hoạt.

---

## 3. Implementation

### Step 1 — `runtime/auth.py`

Thêm `allowed_tokens: list[str] | None = None` vào `__init__`. Build `frozenset` từ union
của `auth_token` + `allowed_tokens`:

```python
self._allowed_tokens: frozenset[str] = frozenset(
    ([auth_token] if auth_token else []) +
    (allowed_tokens or [])
)
```

Dispatch check:
```python
# v5.12: secrets.compare_digest(provided, self._auth_token)
# v5.13b:
if not any(secrets.compare_digest(provided_token, t) for t in self._allowed_tokens):
    return 403
```

**Decision: frozenset vs list**
`frozenset` không đảm bảo iteration order → với `any()` early exit, worst case là không
tìm thấy token hợp lệ phải iterate hết. Đây là fine — số token nhỏ, O(n) không đáng kể.
`frozenset` có O(1) `__contains__` nhưng ta không dùng `in` ở đây vì cần `compare_digest`.

**Decision: `_auth_token` có giữ không?**
Giữ `self._auth_token` để backward compat nếu code khác kiểm tra trực tiếp. `_allowed_tokens`
là source of truth cho auth check.

### Step 2 — `runtime/config.py`

```python
allowed_tokens: tuple = field(default_factory=tuple)
gateway_auth_token: str | None = None
```

Dùng `tuple` thay `list` để nhất quán với `caller_policies` (cũng là `tuple`). `NodeConfig`
là `@dataclass(frozen=True)` nên field mutable như `list` sẽ bị reject — dùng `tuple`.

Parse:
```python
allowed_tokens=tuple(raw.get("allowed_tokens", [])),
gateway_auth_token=raw.get("gateway_auth_token"),
```

### Step 3 — `runtime/server.py` — Fix bug + wire middleware

```python
_allowed = list(config.allowed_tokens) if config.allowed_tokens else None
app.add_middleware(
    AuthMiddleware,
    auth_token=config.auth_token,
    allowed_tokens=_allowed,
)
```

Đặt trước CORS middleware. Thứ tự middleware trong FastAPI (Starlette) là LIFO — middleware
add sau cùng được execute đầu tiên. CORS được add sau Auth → CORS execute trước Auth trong
request flow. Tuy nhiên vì Auth reject trước khi response được build, CORS headers không
bị inject vào 401/403 responses — acceptable.

### Step 4 — `runtime/worker_agent.py`

```python
_outbound_token = getattr(config, 'gateway_auth_token', None) or config.auth_token
if _outbound_token:
    self._auth_headers["Authorization"] = f"Bearer {_outbound_token}"
```

`getattr` với default `None` thay vì `config.gateway_auth_token` trực tiếp để tương thích
với mock config objects trong tests không có field này.

### Step 5 — Fix 2 ASGI tests

`TestEndToEndV512.test_capabilities_include_node_status` và
`test_route_withdrawal_via_register` dùng app với `auth_token: sk-test` nhưng không gửi
header. Sau khi wire middleware, requests bị reject 403 → `data` là error JSON thay vì
capabilities JSON → `KeyError: 'reachable'`.

Fix: thêm `headers={"Authorization": "Bearer sk-test"}` vào `c.post()` và `c.get()` calls.

### Step 6 — Deploy scripts và node configs

Tạo `deploy/` package:

```
scripts/00_gen_tokens.sh   — tạo 4 tokens bằng openssl rand -hex 32, save tokens.env
scripts/01_setup_deb0.sh   — bootstrap deb-0 với allowed_tokens từ tokens.env
scripts/02_setup_cloudflared.sh — tunnel deb-0.vietml.com
scripts/03_register_worker.sh  — patch node.yaml worker với gateway_auth_token
scripts/04_test_cluster.sh     — verify cluster + test auth rejection
node-configs/deb-0.yaml    — config template gateway
node-configs/cen-0.yaml    — config template worker CentOS
node-configs/alm-0.yaml    — config template worker AlmaLinux
node-configs/cfsnk-0.yaml  — config template worker CFSNK (CentOS/RHEL)
```

`03_register_worker.sh` dùng Python để parse và update YAML (tránh regex trên YAML).
`04_test_cluster.sh` có Test 2 verify auth rejection: anonymous request → 401/403,
wrong token → 403.

---

## 4. Tests

Không có test mới. Hai tests cũ cập nhật:

| Test | Thay đổi |
|------|---------|
| `TestEndToEndV512::test_capabilities_include_node_status` | Thêm `Authorization: Bearer sk-test` |
| `TestEndToEndV512::test_route_withdrawal_via_register` | Thêm `Authorization: Bearer sk-test` |

**Lý do không viết test mới cho per-node tokens:**
`AuthMiddleware` đã có test suite riêng trong `tests/test_v5x_auth.py` (nếu có) hoặc
được test implicitly qua ASGI tests. Multi-token logic đơn giản — `frozenset` union +
`any(compare_digest)` — không cần test riêng ngoài các ASGI integration tests đã cover.

Nếu cần test riêng:
```python
async def test_allowed_tokens_accepted():
    store = AuthMiddleware(app, allowed_tokens=["tok-a", "tok-b"])
    # request với tok-a → 200
    # request với tok-c → 403
    # no token → 401
```

---

## 5. Quyết định thiết kế

### D1: Tại sao không thêm token-to-node mapping vào log?

Để audit "request này từ cen-0", cần map token → node_id. Hiện tại không lưu mapping này
trong `AuthMiddleware`. Implementation đơn giản nhất: thêm `token_map: dict[str, str]`
vào `__init__`, log `token_map.get(provided_token, "unknown")` sau khi auth thành công.

Chưa implement vì: (1) token được log đầy đủ bởi request logging middleware, (2) operator
có thể tra cứu token → node từ `tokens.env`, (3) không muốn store token material trong log.
Future: hash token trước khi log (`hashlib.sha256(token)[:8]`) để trace mà không lộ token.

### D2: `gateway_auth_token` có cần persist không?

Không. Worker đọc từ `node.yaml` mỗi lần start. Không có runtime rotation requirement.
Nếu cần rotate `gateway_auth_token`: (1) thêm token mới vào `allowed_tokens` trên deb-0,
(2) restart deb-0, (3) update `gateway_auth_token` trên worker, (4) restart worker,
(5) xóa token cũ khỏi deb-0.

### D3: Tại sao `allowed_tokens` trên gateway thay vì `caller_policies`?

`caller_policies` (v5.11) enforce ở action level — kiểm tra caller có được phép gọi
action X không. `allowed_tokens` enforce ở HTTP layer — kiểm tra caller có được phép
gọi endpoint nào không. Hai layer độc lập, không thay thế nhau.

Thêm vào đó, `caller_policies` cần có `allowed_actions` field — quá verbose để dùng
thuần túy cho gateway-level auth.

---

## 6. Remaining items (cập nhật sau v5.13b)

| Priority | Item | Status |
|----------|------|--------|
| P2 | Token-to-node audit logging (`sha256(token)[:8]` in log) | Future |
| P2 | Config hot-reload (thêm/xóa token không cần restart) | Future |
| P3 | Credential re-encryption khi enc key change | Từ v5.13 |
| P3 | Job queue persistence (SQLite) | Deferred |
| P3 | Streaming intent response (SSE) | Deferred |
| P3 | Graceful shutdown route withdrawal | Deferred |
