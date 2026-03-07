# Worklog — Execution Mesh v5.5 (Claude Web as Orchestrator)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-04
**Base version:** v5.4 → **v5.5**

---

## 1. Vấn đề với v5.4 Planner

`planner/planner.py` implement ReAct loop bằng cách gọi LLM API ngoài:

```python
# planner.py — gọi OpenAI để reasoning
resp = await httpx.post(llm_base_url + "/chat/completions", json={
    "model": config.llm_model,
    "messages": [...],
    ...
})
```

Đây là **anti-pattern** khi dùng Claude Web làm orchestrator, vì:

| Vấn đề | Hệ quả |
|--------|--------|
| Gọi LLM API ngoài trong khi Claude Web đã là LLM | Redundant — reinventing Claude inside Claude |
| Cần `llm_api_key`, `llm_base_url` trong config | Thêm friction, thêm cost |
| Thêm một HTTP roundtrip nữa | Tăng latency, thêm điểm failure |
| System prompt hardcoded trong Python | Khó tùy chỉnh, khó debug |

**Đúng hơn:** Claude Web *là* reasoning engine. Nó chỉ cần một tool đơn giản để
gọi gateway và lấy kết quả — không cần Python orchestrate lại LLM calls.

## 2. Thay đổi

### Deprecated (2 files)

| File | Lý do |
|------|-------|
| `planner/planner.py` → `.deprecated` | Redundant khi dùng Claude Web |
| `planner/cli.py` → `.deprecated` | Entry point cho planner — không cần |

Đổi sang `.deprecated` thay vì xóa để preserve lịch sử. Giữ `config.py`,
`mesh_client.py`, `skills_cache.py` phòng khi cần headless automation sau này.

### Mới (3 files)

**`mesh_ctl.py`** (319 LOC) — CLI tool đơn giản, Claude Web dùng như "tay":

```
mesh_ctl.py run <node> <action> '<params>'   # execute + poll
mesh_ctl.py run ... --no-wait                # execute, trả job_id ngay
mesh_ctl.py result <job_id>                  # poll job cụ thể
mesh_ctl.py nodes                            # list nodes
mesh_ctl.py health                           # gateway health
mesh_ctl.py skills <node_id>                 # fetch node skills
```

Output luôn là JSON — Claude đọc và quyết định bước tiếp theo.
Config qua env vars (`MESH_GATEWAY`, `MESH_TOKEN`) hoặc flags.

**`SYSTEM_PROMPT.md`** — Template sẵn dùng. Anh chỉ cần:
1. Điền IP nodes, token, tên database
2. Paste vào Claude Web
3. Thêm task → Claude bắt đầu orchestrate

**`planner/README.md`** — Ghi rõ file nào deprecated, file nào còn dùng được, và tại sao.

**`tests/test_mesh_ctl.py`** (9 tests) — Cover cmd_run, cmd_result (poll/timeout/fail), cmd_nodes, cmd_health.

## 3. Kiến trúc sau v5.5

```
Trước (v5.4):                        Sau (v5.5):

User                                  User
 │                                     │
 ▼                                     ▼
mesh_ctl.py                        Claude Web
 │                                  (Pro/Max)
 ▼                                     │
CloudPlanner ──► LLM API           mesh_ctl.py
    (redundant)        │                │
                       ▼               ▼
                   Reasoning       Gateway (node-0)
                                        │
                                   node-1  node-2
```

## 4. Test suite

| File | Tests | Status |
|------|-------|--------|
| test_mesh_ctl.py | 9 | ✅ NEW |
| (73 tests tổng từ v5.4) | 73 | ✅ |
| **Tổng** | **73** | **73/73 Pass** |

*test_planner.py giữ lại vì test MeshClient + SkillsCache còn dùng được.*

---

*Document: WORKLOG_V5.5.md | Mesh Runtime v5.5 | Repository: ai-infra-runtime-v2*
