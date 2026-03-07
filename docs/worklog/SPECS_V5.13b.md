# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v5.13b
### (Per-Node Auth Tokens · AuthMiddleware Wire-up · Cluster Deployment Model)

---

## 1. Bối cảnh

v5.13 (credential store, key rotation, specs cascade) giả định tất cả nodes dùng chung một
`auth_token`. Khi deploy cluster thực tế với nhiều máy khác nhau, mô hình này có nhược điểm:

- **Blast radius lớn**: lộ token của một worker → tất cả workers bị ảnh hưởng
- **Không audit được**: gateway không phân biệt request đến từ cen-0 hay cfsnk-0
- **Revocation thô**: muốn thu hồi quyền của một worker → phải đổi token toàn cluster

Ngoài ra, sau khi review code phát hiện `AuthMiddleware` đã được import trong `server.py`
nhưng **chưa bao giờ được wire vào app** — mọi endpoint thực tế đang unprotected dù config
có `auth_token`. Bug này được fix đồng thời trong phiên bản này.

---

## 2. Mô hình per-node token

### 2.1 Thiết kế

Mỗi worker có một Bearer token riêng biệt. Gateway (deb-0) chấp nhận toàn bộ danh sách này.

```
┌─────────────────────────────────────────────────┐
│  deb-0/node.yaml                                │
│                                                 │
│  allowed_tokens:                                │
│    - "tok-cen-0-abc..."    ← token của cen-0    │
│    - "tok-alm-0-def..."    ← token của alm-0    │
│    - "tok-cfsnk-0-ghi..."  ← token của cfsnk-0  │
│    - "tok-external-xyz..."  ← external callers  │
└─────────────────────────────────────────────────┘

┌──────────────────────────────┐
│  cen-0/node.yaml             │
│                              │
│  auth_token: "tok-cen-0-abc" │    ← incoming requests đến cen-0
│  gateway_auth_token:         │
│    "tok-cen-0-abc"           │    ← outbound requests lên deb-0
└──────────────────────────────┘
```

**Quy ước:** `auth_token` = token node này NHẬN; `gateway_auth_token` = token node này GỬI
khi gọi lên gateway. Trong hầu hết deployments cả hai giống nhau — tách biệt để cho phép
các trường hợp đặc biệt (ví dụ: worker nhận token nội bộ khác với token nó dùng ra ngoài).

### 2.2 So sánh với v5.12

| | v5.12 | v5.13b |
|--|-------|--------|
| Auth model | Single shared `auth_token` | Per-node `allowed_tokens` list |
| Worker gọi gateway | Dùng `auth_token` | Dùng `gateway_auth_token` (fallback `auth_token`) |
| AuthMiddleware | Import nhưng chưa wire | Wired vào app |
| Token revocation | Đổi token toàn cluster | Xóa một token khỏi `allowed_tokens` |
| Audit | Không phân biệt được source | Log có thể map token → node (future) |
| Backward compat | N/A | `auth_token` đơn vẫn hoạt động |

---

## 3. Code changes

### 3.1 `runtime/auth.py` — Multi-token AuthMiddleware

**Trước (v5.12):** `AuthMiddleware` nhận một `auth_token: str | None`, so sánh
constant-time với token trong request.

**Sau (v5.13b):** Nhận thêm `allowed_tokens: list[str] | None`. Internally build
`frozenset[str]` từ cả hai nguồn. Dispatch check dùng `any(secrets.compare_digest(...))`.

```python
# v5.13b signature
def __init__(
    self,
    app: Any,
    auth_token: str | None = None,         # single token (backward compat)
    allowed_tokens: list[str] | None = None,  # per-node tokens (v5.13b)
    exempt_paths: frozenset[str] | None = None,
) -> None:
    # Hợp nhất cả hai nguồn vào frozenset
    self._allowed_tokens: frozenset[str] = frozenset(
        ([auth_token] if auth_token else []) +
        (allowed_tokens or [])
    )
```

**Constant-time check trên nhiều tokens:**
```python
if not any(
    secrets.compare_digest(provided_token, t)
    for t in self._allowed_tokens
):
    return JSONResponse({"error": "FORBIDDEN"}, status_code=403)
```

`any()` dừng sớm khi tìm được match — tuy nhiên `secrets.compare_digest` vẫn
constant-time per-comparison, đủ an toàn cho số lượng token nhỏ (<100).

**Exempt paths không thay đổi:**
```
/health, /ping, /skills, /setup.sh, /runtime-bundle, /docs, /openapi.json
```

### 3.2 `runtime/config.py` — Hai field mới

```python
# v5.13b — Per-node auth tokens
allowed_tokens: tuple = field(default_factory=tuple)
# tuple[str] — accepted Bearer tokens; gateway sets this to list of all worker tokens

gateway_auth_token: str | None = None
# Token this worker presents to its gateway; overrides auth_token for outbound calls.
# When None, falls back to auth_token (backward compat with v5.12).
```

Parse từ `node.yaml`:
```python
allowed_tokens=tuple(raw.get("allowed_tokens", [])),
gateway_auth_token=raw.get("gateway_auth_token"),
```

`allowed_tokens` dùng `tuple` (immutable, hashable) thay vì `list` để nhất quán
với `caller_policies` trong `NodeConfig`.

### 3.3 `runtime/server.py` — Wire AuthMiddleware + fix bug

**Bug fix:** `AuthMiddleware` trước đây được import nhưng không được thêm vào app.
Tất cả endpoints (trừ `/ping`) thực tế không được bảo vệ.

```python
# v5.13b — Auth middleware (WIRED, không chỉ import)
_allowed = list(config.allowed_tokens) if config.allowed_tokens else None
app.add_middleware(
    AuthMiddleware,
    auth_token=config.auth_token,
    allowed_tokens=_allowed,
)
```

Middleware được add trước CORS để auth check xảy ra trước khi CORS headers được
inject vào response — tránh lộ CORS info với unauthenticated requests.

### 3.4 `runtime/worker_agent.py` — gateway_auth_token

```python
# v5.13b: gateway_auth_token takes precedence over auth_token for outbound calls
_outbound_token = getattr(config, 'gateway_auth_token', None) or config.auth_token
if _outbound_token:
    self._auth_headers["Authorization"] = f"Bearer {_outbound_token}"
```

`getattr(..., None)` để tương thích ngược với các test fixture mock config object.

---

## 4. node.yaml schema (v5.13b)

### Gateway (deb-0)

```yaml
node_id: deb-0
listen: 0.0.0.0:8080

# Per-node tokens — liệt kê token của MỌI caller được phép
allowed_tokens:
  - "<TOKEN_CEN_0>"
  - "<TOKEN_ALM_0>"
  - "<TOKEN_CFSNK_0>"
  - "<TOKEN_EXTERNAL>"     # external callers: curl, Claude Web, v.v.

trusted_nodes:
  - cen-0
  - alm-0
  - cfsnk-0

# auth_token: không cần khi đã có allowed_tokens
```

### Worker (cen-0 / alm-0 / cfsnk-0)

```yaml
node_id: cen-0
listen: 0.0.0.0:8080

# Token của NODE NÀY (incoming requests)
auth_token: "<TOKEN_CEN_0>"

# Token dùng khi gọi lên deb-0 (outbound)
# Thường giống auth_token; tách biệt để linh hoạt
gateway_auth_token: "<TOKEN_CEN_0>"

gateway_node_id: deb-0
gateway_address: "https://deb-0.vietml.com"
```

---

## 5. Backward compatibility

| Scenario | Behavior |
|---------|---------|
| `auth_token` set, `allowed_tokens` empty | Hoạt động như v5.12 — single token |
| `allowed_tokens` set, `auth_token` empty | Multi-token mode |
| Cả hai set | Union: cả hai đều valid |
| Cả hai empty | Open mode (no auth) |
| `gateway_auth_token` không set | Worker dùng `auth_token` khi gọi gateway |

Không có breaking change với v5.12 deployments đang chạy.

---

## 6. Security notes

**Về timing attack:** `any(secrets.compare_digest(t, token) for t in allowed_tokens)`
dừng sớm khi tìm match (early exit) → attacker có thể đo thời gian phản hồi để suy ra
số token trong list. Tuy nhiên:
- Số token nhỏ (< 10 trong hầu hết deployments)
- Latency variation từ network >> timing difference từ early exit
- Nếu cần strict constant-time với nhiều tokens, dùng HMAC-based comparison thay thế

**Token generation:** Mỗi token nên là `openssl rand -hex 32` (256 bits entropy).
Không dùng UUID (chỉ 122 bits, dễ đoán hơn).

**Token storage:** `tokens.env` file trên máy operator không nên commit vào git.
Thêm vào `.gitignore`:
```
tokens.env
*.env
```

---

## 7. Files thay đổi

| File | Thay đổi |
|------|---------|
| `runtime/auth.py` | `AuthMiddleware.__init__` nhận `allowed_tokens`; dispatch check dùng `frozenset` + `any(compare_digest)` |
| `runtime/config.py` | `NodeConfig.allowed_tokens: tuple`; `NodeConfig.gateway_auth_token: str \| None` |
| `runtime/server.py` | **Bug fix**: wire `AuthMiddleware` vào app; pass `allowed_tokens` |
| `runtime/worker_agent.py` | Outbound requests dùng `gateway_auth_token` nếu set |
| `tests/test_v512_features.py` | 2 ASGI tests thêm `Authorization` header (previously passed do auth chưa wire) |
| `docs/SPECS_V5.13b.md` | Document này |
| `docs/WORKLOG_V5.13b.md` | Implementation worklog |
| `deploy/` | Scripts + node configs cho cluster 4 nodes (deb-0, cen-0, alm-0, cfsnk-0) |

---

## 8. Trạng thái hệ thống (v5.13b)

**Auth:**
- ✅ Single shared token (`auth_token`) — v5.12
- ✅ Per-node tokens (`allowed_tokens`) — v5.13b
- ✅ Separate outbound token (`gateway_auth_token`) — v5.13b
- ✅ AuthMiddleware actually enforced — v5.13b (bug fix)
- ❌ Token-to-node audit logging (future)
- ❌ Token rotation without restart (future — hot-reload config)

**Deployment:**
- ✅ One-liner worker install via `GET /setup.sh` + `GET /runtime-bundle`
- ✅ Cloudflared tunnel (deb-0.vietml.com)
- ✅ Systemd service per node
- ✅ Per-node config templates
- ✅ Token generator script
- ✅ Cluster test suite

---

*Spec: SPECS_V5.13b.md | Mesh Runtime v5.13b | Repository: ai-infra-runtime-v2*
