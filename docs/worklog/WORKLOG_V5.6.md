# Worklog — Execution Mesh v5.6 (Claude Web as Native Orchestrator)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-04
**Base version:** v5.5 → **v5.6**

---

## 1. Vấn đề với v5.5

v5.5 deprecated `planner/planner.py` (Python LLM loop) và giới thiệu `mesh_ctl.py`
— một CLI wrapper giúp Claude Web gọi gateway bằng 1 lệnh đơn.

Tuy nhiên `mesh_ctl.py` vẫn là **indirection không cần thiết**:

| Việc `mesh_ctl.py` làm | Claude có thể tự làm bằng curl |
|------------------------|-------------------------------|
| Build JSON envelope | Viết JSON inline trong `-d '...'` |
| POST `/action` | `curl -X POST` |
| Poll `/result/{job_id}` | `curl` + lặp lại khi cần |
| Print JSON output | `curl -s` output thẳng là JSON |

Ngoài ra, `mesh_ctl.py` còn có 1 anti-pattern quan trọng: **blocking poll loop**.
Với `--wait` (mặc định), nó block terminal cho đến khi job xong — đây là hành vi
Claude không nên bị ép buộc. Claude hoàn toàn có thể:

1. Gửi action → nhận `job_id`.
2. Làm việc khác (bootstrap node khác, install package, hỏi user, v.v.).
3. Quay lại poll khi cần.

`mesh_ctl.py` không hỗ trợ tốt workflow async tự nhiên này.

---

## 2. Thay đổi

### 2.1 Deprecated

| File | Lý do |
|------|-------|
| `mesh_ctl.py` → stub + `.deprecated` | Claude dùng curl trực tiếp, wrapper thừa |

`mesh_ctl.py` giữ lại dưới dạng stub (raise SystemExit với hướng dẫn) để không
break người dùng cũ ngay lập tức. Code gốc preserve trong `mesh_ctl.py.deprecated`.

### 2.2 Sửa đổi: `runtime/models.py`

**`ActionRequest.task_id`** — từ `str` (bắt buộc) thành `str | None` (optional).

Nếu caller không cung cấp, server tự sinh `task-<uuid4[:12]>`.

**`ActionRequest.trace`** — từ `TraceInfo` (default_factory) thành `TraceInfo | None`
(optional). Nếu caller không cung cấp, server tự default `TraceInfo(hop_count=0, route_path=[])`.

Implementation dùng Pydantic v2 `@model_validator(mode="after")` — clean, không cần
override `__init__`.

Kết quả: Claude (và mọi caller) giờ chỉ cần gửi **2 fields**:

```json
{
  "target_node_id": "node-1",
  "payload": {"action": "execute_command", "params": {"command": "df -h"}}
}
```

**Backward compatible hoàn toàn** — envelope đầy đủ từ v5.0-v5.5 vẫn hoạt động.

### 2.3 Sửa đổi: `runtime/server.py`

- Version string: `5.3.0` → `5.6.0`.
- Docstring cập nhật ghi rõ v5.6 changes.
- `action_endpoint` thêm debug log khi `task_id` được auto-generate (tracing).

### 2.4 Mới: `SYSTEM_PROMPT.md`

Template system prompt cho Claude Web Project. Nội dung:

- Giải thích vai trò orchestrator.
- Pattern curl chuẩn cho tất cả operations (action, poll, read/write, inspect, bootstrap).
- **Async workflow pattern**: hướng dẫn cụ thể cách Claude xử lý `job_id` mà không bị block.
- Reference envelope tối giản (chỉ 2 fields).
- Reasoning rules (khi nào hỏi user, khi nào cần confirm trước khi destroy).

---

## 3. Async Workflow — Nguyên tắc cốt lõi

v5.6 làm rõ nguyên tắc mà v5.5 chưa document rõ:

```
Claude gửi action → nhận job_id
     │
     ├── Nếu có việc khác → làm việc khác trước
     │
     ├── Sau estimated_completion_seconds → poll /result/{job_id}
     │        │
     │        ├── running → tiếp tục làm việc khác, poll lại sau
     │        │
     │        └── completed/failed → proceed
     │
     └── Nếu không có việc khác → poll ngay (shell while loop)
```

Claude **không** bao giờ bị block hoàn toàn bởi 1 job. Đây là điểm khác biệt
so với `mesh_ctl.py --wait`.

---

## 4. Kiến trúc sau v5.6

```
User
  │
  ▼
Claude Web (Pro/Max)
  │  reads SYSTEM_PROMPT.md
  │  uses bash tool
  │
  ├── curl POST $GATEWAY/action  → {"job_id": "..."}  (async)
  ├── [làm việc khác]
  ├── curl GET  $GATEWAY/result/$JOB_ID
  ├── curl POST $GATEWAY/action  (action tiếp theo)
  └── ... cho đến khi hoàn tất
        │
        ▼
   Gateway (node-0)
        │
   node-1  node-2  ...
```

**User không cần làm gì** ngoài đưa ra yêu cầu ban đầu và trả lời câu hỏi nếu
Claude cần thông tin bổ sung.

---

## 5. Files thay đổi

| File | Loại | Thay đổi |
|------|------|---------|
| `runtime/models.py` | Modified | `task_id` optional + auto-gen; `trace` optional + auto-default |
| `runtime/server.py` | Modified | Version 5.6.0; docstring; debug log task_id |
| `mesh_ctl.py` | Deprecated → stub | Giữ lại dưới dạng stub + `.deprecated` |
| `SYSTEM_PROMPT.md` | New | Template cho Claude Web Project |
| `tests/test_v56_features.py` | New | 10 tests |
| `docs/WORKLOG_V5.6.md` | New | Document này |
| `docs/SPECS_V5.6.md` | New | Spec chính thức v5.6 |

---

## 6. Test Suite

### Mới (v5.6)

| Class | Tests | Coverage |
|-------|-------|---------|
| `TestActionRequestDefaults` | 8 | task_id auto-gen, uniqueness, explicit preserve; trace auto-default, explicit preserve; minimal dict parse; JSON round-trip; full envelope backward compat |
| `TestAsgiMinimalEnvelope` | 2 | POST /action minimal envelope via ASGI; POST /action full envelope backward compat |
| **Tổng mới** | **10** | |

### Tổng kết

| Version | Tests | Pass |
|---------|-------|------|
| v5.5 | 73 | 73 |
| **v5.6** | **83** | **83** |

---

## 7. Remaining Items (giữ nguyên từ v5.3)

| Priority | Item |
|----------|------|
| P2 | Background task gọi `mark_stale_nodes_unreachable()` định kỳ |
| P2 | Container isolation per-node (Docker) |
| P3 | Job queue persistence (SQLite/Redis) |
| P3 | Pull job timeout |
| P3 | Queue depth trong `/health` |
| **P3** | **Migrate `on_event` → `lifespan` (FastAPI deprecation)** |
| P4 | Idempotency key |

---

*Document: WORKLOG_V5.6.md | Mesh Runtime v5.6 | Repository: ai-infra-runtime-v2*
