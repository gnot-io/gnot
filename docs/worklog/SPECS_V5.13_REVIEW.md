# GNOT Codebase Review
**Đối chiếu Specs v5.13 Full ↔ Implementation**
*Ngày review: 06/03/2026 | Runtime: v5.13b*

---

## Tổng quan kết quả

> ✅ **Kết luận:** Codebase khớp với Specs ở mức độ cao. Toàn bộ kiến trúc cốt lõi được implement đúng. Có 6 điểm sai lệch nhỏ giữa Specs và code — không ảnh hưởng chức năng chính, nhưng cần ghi chú khi viết whitepaper.

| Nhóm tính năng | Trạng thái | Khớp Specs | Ghi chú |
|---|---|---|---|
| Node Anatomy & Runtime Startup | ✅ Đúng | 100% | Khớp hoàn toàn |
| Action Model (Seed actions, Plugin contract) | ✅ Đúng | 100% | Khớp hoàn toàn |
| Request/Response Envelope | ⚠️ Nhỏ | 95% | ErrorResponse thiếu field `detail` |
| Job Lifecycle & States | ✅ Đúng | 100% | Khớp hoàn toàn |
| Routing Engine (GatewayRouter, BGP) | ✅ Đúng | 100% | Khớp hoàn toàn |
| Push/Pull Delivery & WorkerAgent | ⚠️ Nhỏ | 95% | Default intervals khác nhỏ |
| Authentication & Authorization | ✅ Đúng | 100% | Khớp hoàn toàn |
| Intent Agent Loop (ReAct) | ✅ Đúng | 100% | Khớp hoàn toàn |
| Session Management | ✅ Đúng | 100% | Khớp hoàn toàn |
| File Transfer System | ✅ Đúng | 100% | file_id: 16 hex chars — đúng specs |
| Credential System (AES-256-GCM) | ✅ Đúng | 100% | Khớp hoàn toàn |
| Self-Bootstrapping Mechanism | ✅ Đúng | 100% | Khớp hoàn toàn |
| Data Models (QueuedJob, CallerPolicy...) | ⚠️ Nhỏ | 95% | `claimed_by` → `claimed: bool` trong code |
| Error Codes | ⚠️ Nhỏ | 90% | MAX_HOP_EXCEEDED, LOOP_DETECTED dùng free-text |
| Configuration Defaults (NodeConfig) | ⚠️ Nhỏ | 90% | `heartbeat_timeout`: 30s code vs 120s specs |

---

## Chi tiết 6 điểm sai lệch

### ① ErrorResponse — thiếu field `detail`

| | Specs nói | Code thực tế |
|---|---|---|
| **File** | `models.py` | `models.py` |
| **Schema** | `{ "error": "CODE", "detail": "Human-readable..." }` | `class ErrorResponse: error: str; node_id: str \| None` |

Code không có field `detail`. Thay vào đó có `node_id` — phục vụ mục đích debug khác.

💡 **Khuyến nghị cho whitepaper:** Cập nhật specs §5.4 để reflect field `node_id`, hoặc thêm `detail` vào code cho nhất quán.

---

### ② Heartbeat Timeout Default — 30s vs 120s

| | Specs §18.1 | Code thực tế |
|---|---|---|
| `heartbeat_timeout_seconds` | `120` | `DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 30` |
| `heartbeat_interval_seconds` | `15` | `DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10` |
| `intent_max_turns` | `20` | `DEFAULT_INTENT_MAX_TURNS = 10` |

Cả `config.py` lẫn `node_registry.py` đều dùng 30s. Specs §23.1 version history cũng ghi default là 10 cho `intent_max_turns`, nên specs §18.1 có thể là outdated.

💡 **Khuyến nghị:** Đây là discrepancy đáng chú ý nhất. Nên đồng bộ specs §18.1 theo code thực tế trước khi publish whitepaper.

---

### ③ Error Code Format — MAX_HOP_EXCEEDED, LOOP_DETECTED

| | Specs §5.4 | Code thực tế (`gateway_router.py`) |
|---|---|---|
| Hop exceeded | `"error": "MAX_HOP_EXCEEDED"` | `"error": "Max hop count exceeded: 11 > 10"` |
| Loop detected | `"error": "LOOP_DETECTED"` | `"error": "Routing loop detected: node-0 already in route_path"` |

Code dùng free-text descriptive string thay vì SCREAMING_SNAKE_CASE error codes như specs định nghĩa.

💡 **Khuyến nghị:** Cần chuẩn hóa nếu muốn client-side error handling theo specs. Với whitepaper, nên chọn một convention và document nhất quán.

---

### ④ QueuedJob — `claimed_by` field

| | Specs §20.6 | Code thực tế (`models.py`) |
|---|---|---|
| Field | `claimed_by: str \| None = None` | `claimed: bool = False` + `claimed_at: float \| None = None` |

Specs mô tả `claimed_by` lưu node_id của worker đã claim. Code thực tế dùng boolean `claimed` + timestamp, không lưu node identity.

💡 **Khuyến nghị:** Cập nhật specs §20.6 cho đúng với implementation. `claimed: bool` là đủ vì context đã biết worker nào claim qua poll endpoint.

---

### ⑤ ACTION_NOT_FOUND — exception vs error code

| | Specs §5.4 | Code thực tế |
|---|---|---|
| Khi action không tìm thấy | `"error": "ACTION_NOT_FOUND"`, HTTP 400 | `raise KeyError("Action not found: X")` → bắt ở `gateway_router.py` → `ErrorResponse` |

Functionally đúng — `KeyError` được bắt và convert sang `ErrorResponse`. Nhưng error message là free-text, không phải code chuẩn `ACTION_NOT_FOUND`.

---

### ⑥ NodeConfig: `intent_max_turns` default 10 vs 20

| | Specs §18.1 | Code (`config.py`) |
|---|---|---|
| `intent_max_turns` | `20` | `DEFAULT_INTENT_MAX_TURNS: int = 10` |

Lưu ý: Specs §23.1 version history ghi `intent_max_turns` là 10 từ v5.9 — vậy specs §18.1 mới là chỗ sai, không phải code.

---

## Các phần đã xác nhận hoàn toàn đúng

### Kiến trúc cốt lõi ✅

- **GatewayRouter decision tree (§7.1):** Đúng hoàn toàn — hop guard → loop guard → local exec → next-hop routing → trusted check → push/pull, đúng thứ tự
- **BGP-style Route Advertisement (§9):** `NodeRegistry.register()` và `withdraw_routes()` implement đúng BGP semantics — quảng bá sub-routes qua `advertise_routes`, rút route khi re-register
- **Push/Pull Delivery Model (§8):** `WorkerAgent` với 3 asyncio tasks (`_heartbeat_loop`, `_poll_loop`, `_reregister_loop`) — đúng specs §8.4
- **Lazy Staleness Detection (§17.2):** `_apply_lazy_staleness()` trong `node_registry.py` — không có background poller, check lazy mỗi khi routing decision xảy ra
- **Pull Job Timeout (§6.5):** Lazy check trong `route_result()` khi job ở trạng thái `QUEUED` — đúng "checked lazily at GET /result time"
- **Transparent Push→Pull Fallback (§8.3):** Nếu push fail → fallback pull, caller không biết — đúng

### Authentication & Authorization ✅

- **AuthMiddleware (§11.1):** Exempt paths đúng — `/health`, `/ping`, `/skills`, `/setup.sh`, `/runtime-bundle`, `/docs`, `/openapi.json`
- **Per-node tokens (§11.2):** `allowed_tokens` (frozenset), `gateway_auth_token` — đúng specs v5.13b
- **Constant-time comparison:** `secrets.compare_digest()` — đúng specs §11.1, chống timing attack
- **Caller Policies (§11.3):** `CallerPolicy` dataclass với `allows()` method, `check_caller_policy()` — đúng hoàn toàn kể cả wildcard `"*"`
- **Caller Credential (§11.4):** `x-caller-credentials` trong schema JSON, `MISSING_CALLER_CREDENTIAL` error — đúng

### Credential System AES-256-GCM ✅

- **Key derivation:** `SHA-256(encryption_key + ':' + session_id)` — đúng specs §15.3
- **Wire format:** `AESGCM`, nonce 12 bytes, `base64(nonce + ciphertext + tag)` — đúng
- **Separate `credential_encryption_key` (§D5):** Decouple từ `auth_token` — implement đúng thiết kế
- **Atomic flush:** write `.tmp` → `os.replace()` — đúng specs §15.4
- **Lifecycle:** `load()` + `start_flush_task()` + `stop_flush_task()` trong FastAPI lifespan — đúng

### Intent Handler ReAct Loop ✅

- **Single `mesh_action` tool (§D3):** Đúng thiết kế — 1 tool generic thay vì N×M tools
- **System prompt dynamic** từ live capability tree — đúng specs §12.4
- **Loop termination:** plain text response hoặc `max_turns` hit → `truncated: true` — đúng §12.2
- **Async job polling trong loop:** exponential backoff `[1, 2, 4, 5, 5, ...]` seconds
- **Credential merge per turn:** `stored_creds` merge với `request.caller_credentials`, request creds take precedence — đúng §13.3

### File Transfer System ✅

- **`file_id`:** `uuid4().hex[:16]` = 16 hex chars — khớp specs "16-hex-char UUID fragment"
- **`FileResponse` streaming** — không load vào memory — đúng §14.3
- **Lazy TTL:** sweep on listing, check on get — đúng §14.2
- **`FILE_TOO_LARGE` error** với `max_size_mb` — đúng specs response format

### Data Models ✅

- **`ActionRequest`:** `model_validator` auto-fill `task_id` và `trace` defaults — đúng §5.1
- **`NodeRegistrationRequest`:** `sub_route_specs` field cho action specs cascade — đúng §9.6
- **`CapabilityNode`:** recursive `reachable` dict với forward ref `model_rebuild()` — đúng §20.3
- **`ActionSpec` + `CallerCredentialSpec`** — đúng §20.4, 20.5
- **`JobStatus` enum:** `accepted`, `queued`, `running`, `completed`, `failed` — đúng §6.1

---

## Gợi ý cho Whitepaper

### Nên đồng bộ trước khi submit

1. Cập nhật default values trong §18.1:
   - `heartbeat_timeout_seconds` = **30** (không phải 120)
   - `heartbeat_interval_seconds` = **10** (không phải 15)
   - `intent_max_turns` = **10** (không phải 20)
2. Cập nhật `ErrorResponse` schema: thay `detail` bằng `node_id`, hoặc thêm cả hai vào code
3. Chuẩn hóa error codes `MAX_HOP_EXCEEDED` và `LOOP_DETECTED` sang screaming_snake_case trong code, hoặc document rõ là free-text trong specs

### Điểm mạnh nổi bật để highlight

- **LLM as Control Plane** — GNOT đặt LLM bên trong execution path, không phải bên ngoài. Đây là đóng góp khoa học lớn nhất so với LangChain, AutoGen, CrewAI
- **BGP-style NAT traversal** — Áp dụng BGP semantics (next-hop, route advertisement, route withdrawal) cho LLM mesh routing — hoàn toàn novel trong LLM orchestration context
- **Lazy evaluation pattern** — Staleness detection, pull job timeout, upload TTL đều dùng lazy evaluation thay vì background polling — giảm complexity, dễ test, deterministic
- **Transparent delivery abstraction** — Push/pull mode transparent với LLM caller: cùng interface, routing tự động theo topology thực tế
- **Minimal seed bootstrap** — 3 primitives đủ để tạo toàn bộ mesh — concept "generative" trong tên GNOT
- **Per-session AES-256-GCM key derivation** — Credential isolation by design: `session_id` trong key material

### Cấu trúc whitepaper gợi ý

1. **Abstract** — LLM-native distributed execution, seed-to-mesh bootstrapping
2. **Introduction** — Vấn đề của LLM orchestration frameworks hiện tại
3. **GNOT Architecture** — Node anatomy, action model, request envelope
4. **Routing Engine** — Decision tree, BGP analogy, multi-hop NAT traversal
5. **LLM Control Plane** — ReAct loop design, single tool abstraction, system prompt generation
6. **Security Model** — Auth layers, AES-256-GCM credentials, per-node tokens
7. **Self-Bootstrapping** — Seed → L0 → L3 evolution model
8. **Evaluation** — Latency, scalability, NAT traversal depth
9. **Related Work** — LangChain, AutoGen, CrewAI, ray.io so sánh
10. **Conclusion & Future Work**

**Venue recommendations:** arXiv `cs.DC` (Distributed Computing) hoặc `cs.AI`; SSRN Computer Science Network; ResearchGate. Conference venues: SoCC, EuroSys, HotOS nếu muốn peer review.

---

*Review thực hiện bởi Claude | GNOT Specs v5.13 Full | 06/03/2026*