# Worklog — Execution Mesh v5.4 (Transfer Actions + Cloud AI Planner)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-04
**Author:** Claude (AI Coding Agent)
**Base version:** v5.3 → **v5.4**

---

## 1. Tổng quan

### Vấn đề

v5.3 đã giải quyết NAT/connectivity nhưng còn hai gap chặn mọi use case thực tế:

**Gap 1 — Binary file transfer:**
`read_file` dùng `read_text(encoding="utf-8")` → `UnicodeDecodeError` với database dump,
compressed archive, bất kỳ file binary nào. Không có cách nào ghi binary bytes qua JSON.
Hệ quả: không thể thực hiện backup/restore database qua mesh.

**Gap 2 — Không có orchestration:**
LLM biết từng action riêng lẻ nhưng không có gì tự động gọi chúng theo chuỗi.
Backup/restore là workflow 6–8 bước tuần tự, mỗi bước phụ thuộc output của bước trước.
Không có Planner → phải viết script Python tay cho từng use case.

### Giải pháp implement

| Feature | Files | LOC |
|---------|-------|-----|
| `read_file_b64` action | `seed/actions/read_file_b64.py` + schema | 59 |
| `write_file_b64` action | `seed/actions/write_file_b64.py` + schema | 68 |
| Cloud AI Planner | `planner/` (6 files) | 750 |
| Tests | `tests/test_transfer_actions.py`, `tests/test_planner.py` | 421 |
| **Tổng** | **12 files** | **~1,300** |

---

## 2. Feature A — Binary Transfer Actions

### 2.1 `read_file_b64`

Thay vì `read_text()` dễ vỡ, action mới dùng pipeline:

```
Path.read_bytes() → base64.b64encode() → .decode("ascii") → JSON string
```

ASCII output an toàn trong JSON — không bao giờ có character encoding issue.
Hoạt động với mọi loại file không phân biệt content.

**Output fields:**
- `content_b64` — Base64 string
- `size_bytes` — kích thước file gốc (để Planner verify)
- `path` — absolute path đã resolved (để Planner log chính xác)

### 2.2 `write_file_b64`

```
content_b64 → base64.b64decode(validate=True) → Path.write_bytes()
```

`validate=True` trong `b64decode` reject immediately nếu có character không hợp lệ —
tránh ghi ra file corrupt mà không biết. `Path.parent.mkdir(parents=True, exist_ok=True)`
tự tạo directory tree, Planner không cần gọi `mkdir` riêng.

### 2.3 Transfer pattern

Hai actions này hoạt động như một cặp. LLM sẽ tự học pattern này từ skills.md:

```
Step N:   read_file_b64 {path}              → {content_b64, size_bytes}
Step N+1: write_file_b64 {path, content_b64} → {success, size_bytes}
```

`content_b64` từ Step N nằm trong conversation history dưới dạng OBSERVATION, LLM
tự đưa vào params của Step N+1 mà không cần hướng dẫn thêm.

### 2.4 `node-0/skills.md` cập nhật

Thêm documentation cho `read_file_b64` và `write_file_b64` vào skills Markdown của node-0,
kèm ví dụ transfer pattern rõ ràng để LLM học được cách dùng đúng.

---

## 3. Feature B — Cloud AI Planner

### 3.1 Thiết kế tổng thể

Planner được thiết kế theo nguyên tắc **minimal coupling**:
- Không biết gì về topology mesh, địa chỉ worker, hay delivery mode (push/pull)
- Chỉ nói chuyện với gateway qua 2 endpoints: `POST /action` và `GET /result/{job_id}`
- Tất cả business logic nằm trong LLM + system prompt — không hardcode workflow nào

Điều này đồng nghĩa Planner tự động hoạt động cho bất kỳ use case nào miễn là
node skills được mô tả đúng trong `skills.md`.

### 3.2 `config.py`

`PlannerConfig` dataclass với tất cả config typed, load từ `planner.yaml`.

Hai điểm thiết kế quan trọng:

**`NodeEntry.address` vs `NodeEntry.skills_text`:**
- `address` — để `SkillsCache` fetch `/skills` trực tiếp. Dùng cho node có public IP.
- `skills_text` — Markdown cứng trong config. Dùng cho node sau NAT mà `/skills` không
  accessible từ máy chạy Planner.

**LLM provider agnostic:**
`llm_base_url` chấp nhận bất kỳ OpenAI-compatible endpoint nào. Planner tự động
hoạt động với OpenAI, Anthropic (qua LiteLLM proxy), Ollama local, vLLM self-hosted.

### 3.3 `skills_cache.py`

`SkillsCache.load_all()` chạy một lần trước khi bắt đầu loop. Fetch strategy
theo thứ tự ưu tiên: HTTP fetch → `skills_text` fallback → bỏ qua (warning).

`build_prompt_section()` tạo Markdown block với tất cả skills, mỗi node cách nhau
bằng `---` để LLM dễ phân biệt context.

Việc inject skills vào system prompt thay vì user message là có chủ ý — system prompt
không thay đổi giữa các iteration, tiết kiệm token và giữ instructions ổn định.

### 3.4 `planner.py` — ReAct Engine

#### Vòng lặp

M��i iteration: gọi LLM → parse JSON → dispatch (action / done / error) → inject observation.

LLM nhận full conversation history mỗi lần — context tích lũy theo từng bước,
LLM "nhớ" được mọi output đã nhận và ra quyết định dựa trên đó.

#### JSON mode

`response_format: { type: "json_object" }` ép LLM trả JSON thuần. Điều này loại bỏ
trường hợp LLM bọc JSON trong markdown fence ` ```json ``` ` rồi `json.loads()` fail.
Nếu LLM vẫn trả non-JSON (lỗi hiếm), Planner catch `JSONDecodeError`, inject
`[PARSE ERROR]` và tiếp tục — không crash.

#### Temperature 0.2

Thấp hơn mặc định (0.7–1.0). LLM cần ra quyết định nhất quán khi gọi tool —
không cần "sáng tạo". Temperature thấp giảm hallucination và inconsistent JSON format.

#### Observation format

```
[OBSERVATION] Action succeeded. Output: {"exit_code": 0, "stdout": "..."}
```

Prefix `[OBSERVATION]` giúp LLM phân biệt kết quả thực tế vs instructions trong system prompt.
Quan trọng khi conversation dài (20+ iterations) — LLM cần scan ngược lại tìm output cụ thể.

#### Error resilience

Planner không crash khi action thất bại — inject observation thất bại và để LLM quyết định:
retry với command khác, hoặc signal `error` nếu không khắc phục được.
Đây là thiết kế đúng — Planner không tự ý retry, để LLM (có context đầy đủ) quyết định.

### 3.5 `mesh_client.py`

`execute()` transparent với sync/async actions. Planner chỉ gọi một method, không cần
biết action nào return ngay, action nào cần poll.

Polling loop dùng `deadline = time.time() + poll_max_wait_seconds` thay vì `range(n)`
để timeout chính xác theo thời gian thực, không bị ảnh hưởng bởi latency của từng poll.

### 3.6 `cli.py`

Hai modes:

**Single task** — dùng trong script, CI/CD, cron:
```bash
python -m planner.cli --config planner/planner.yaml "task description"
echo $?  # 0 = success, 1 = failure
```

**Interactive REPL** — dùng khi debug, khám phá capabilities:
```bash
python -m planner.cli --config planner/planner.yaml --interactive
```

Gateway health check ngay khi start — fail fast nếu gateway không accessible
thay vì chạy vài iteration rồi mới timeout.

---

## 4. Files thay đổi

### Files mới (10)

| File | LOC | Mô tả |
|------|-----|-------|
| `seed/actions/read_file_b64.py` | 59 | Binary-safe file read → Base64 |
| `seed/actions/read_file_b64.schema.json` | 14 | Schema validation |
| `seed/actions/write_file_b64.py` | 68 | Base64 → write raw bytes |
| `seed/actions/write_file_b64.schema.json` | 17 | Schema validation |
| `planner/__init__.py` | 0 | Package marker |
| `planner/config.py` | 76 | PlannerConfig + load_config() |
| `planner/mesh_client.py` | 166 | MeshClient: execute + poll |
| `planner/skills_cache.py` | 74 | SkillsCache: fetch + build prompt |
| `planner/planner.py` | 279 | CloudPlanner: ReAct engine |
| `planner/cli.py` | 155 | CLI entry point |
| `planner/planner.yaml` | 55 | Config template |
| `tests/test_transfer_actions.py` | 136 | 12 tests: read/write/roundtrip |
| `tests/test_planner.py` | 285 | 12 tests: skills cache + ReAct loop + mesh client |

### Files sửa đổi (1)

| File | Thay đổi |
|------|---------|
| `node-0/skills.md` | Thêm documentation cho `read_file_b64`, `write_file_b64` kèm transfer pattern example |

---

## 5. Test Suite

### `test_transfer_actions.py` (12 tests)

| Test | Kiểm tra |
|------|---------|
| `test_read_text_file_returns_base64` | File text → Base64 encode đúng |
| `test_read_binary_file_returns_valid_base64` | File binary (all 256 bytes) → decode lại khớp |
| `test_read_missing_path_raises_value_error` | Thiếu param `path` |
| `test_read_nonexistent_file_raises_file_not_found` | File không tồn tại |
| `test_read_returns_resolved_path` | Path trong output là absolute |
| `test_write_text_file` | Ghi text và verify nội dung |
| `test_write_binary_file_roundtrip` | Ghi 256-byte binary và verify byte-for-byte |
| `test_write_creates_parent_directories` | Tự tạo `a/b/c/` nếu chưa có |
| `test_write_missing_path_raises_value_error` | Thiếu param `path` |
| `test_write_missing_content_raises_value_error` | Thiếu param `content_b64` |
| `test_write_invalid_base64_raises_value_error` | Content không phải Base64 hợp lệ |
| `test_full_roundtrip_read_then_write` | **Integration**: read_file_b64 → write_file_b64, binary payload khớp 100% |

### `test_planner.py` (12 tests)

| Test | Kiểm tra |
|------|---------|
| `test_skills_cache_fetches_from_address` | Fetch `/skills` qua HTTP thành công |
| `test_skills_cache_falls_back_to_manual_text` | Dùng `skills_text` khi không có address |
| `test_skills_cache_skips_unreachable_no_fallback` | Node không reachable và không có fallback → bỏ qua |
| `test_planner_done_on_first_response` | LLM signal done ngay iteration 1 |
| `test_planner_single_action_then_done` | 1 action → done (2 iterations) |
| `test_planner_multi_step_workflow` | 3 actions → done (4 iterations), call count đúng |
| `test_planner_error_signal` | LLM signal error → PlannerResult.success=False |
| `test_planner_action_failure_feeds_back` | JobFailedError được inject vào conversation, LLM được gọi lại |
| `test_planner_invalid_json_from_llm_continues` | Non-JSON từ LLM không crash loop |
| `test_planner_max_iterations` | Hết max_iterations → failure với message rõ ràng |
| `test_mesh_client_sync_action` | Response không có job_id → return output trực tiếp |
| `test_mesh_client_error_response_raises` | Response có "error" key → raise MeshError |

### Tổng kết test

| Test file | Tests | v5.3 | v5.4 |
|-----------|-------|------|------|
| test_node_registry.py | 10 | ✅ | ✅ |
| test_job_queue.py | 10 | ✅ | ✅ |
| test_gateway_router.py | 11 | ✅ | ✅ |
| test_v53_integration.py | 9 | ✅ | ✅ |
| **test_transfer_actions.py** | **12** | — | **✅ NEW** |
| **test_planner.py** | **12** | — | **✅ NEW** |
| **Tổng** | **64** | 40 | **64** |

**64/64 Pass ✅**

---

## 6. Metrics

| Metric | v5.3 | v5.4 | Delta |
|--------|------|------|-------|
| Files code | 47 | 59 | +12 |
| Lines of code | ~5,600 | ~6,900 | +1,300 |
| Test cases | 40 | 64 | +24 |
| Test pass rate | 100% | 100% | — |
| Seed actions | 5 | **7** | +2 |
| Endpoints | 14 | 14 | — |
| Planner package | ❌ | **✅** | |
| Binary transfer | ❌ | **✅** | |

---

## 7. Remaining (từ v5.3, vẫn còn)

| Priority | Item | Ghi chú |
|----------|------|---------|
| P2 | Heartbeat stale-checker background task | `mark_stale_nodes_unreachable()` đã có, chưa có task gọi định kỳ |
| P2 | Container isolation per-node | Bootstrap tạo process, chưa Docker |
| P3 | Persistent job store | Queue mất khi gateway restart |
| P3 | Pull job timeout | Job trong queue không có deadline |
| P3 | Planner task history persistence | Mỗi run là stateless |
| P3 | Queue depth trong `/health` | `all_queue_depths()` có sẵn, chưa expose |
| P3 | Idempotency key | LLM có thể retry cùng action khi timeout |
| P4 | FastAPI lifespan migration | `on_event` deprecated warning |

---

*Document: WORKLOG_V5.4.md | Mesh Runtime v5.4 | Repository: ai-infra-runtime-v2*
