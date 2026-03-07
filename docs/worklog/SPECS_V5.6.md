# AI-Orchestrated Self-Bootstrapping Execution Mesh

## Architecture Specification v5.6

### (Claude Web as Native Orchestrator)

---

## 1. Thay đổi so với v5.5

| Thành phần | v5.5 | v5.6 |
|-----------|------|------|
| Orchestrator tool | `mesh_ctl.py` (CLI wrapper) | `curl` trực tiếp |
| Request envelope | `task_id` + `trace` bắt buộc | `task_id` + `trace` optional |
| Async workflow | Blocking poll (`--wait`) | Non-blocking — Claude tự quyết định khi nào poll |
| Entry point cho Claude | `python mesh_ctl.py run ...` | `curl -X POST $GATEWAY/action ...` |

---

## 2. Triết lý thiết kế (bổ sung v5.6)

> Claude Web là LLM và là orchestrator đồng thời.
> Nó không cần wrapper — nó nói chuyện trực tiếp với gateway qua HTTP.

Hệ thống được thiết kế để Claude có thể:

1. Gửi action bất kỳ với envelope tối giản nhất có thể.
2. Nhận `job_id` cho async actions và **tiếp tục làm việc khác**.
3. Poll kết quả khi cần — không bị block.
4. Hỏi user chỉ khi thật sự cần thêm thông tin.
5. Tự bootstrap node mới khi cần thêm capability.

---

## 3. Minimal Request Envelope (v5.6)

Chỉ 2 fields bắt buộc:

```json
{
  "target_node_id": "node-1",
  "payload": {
    "action": "execute_command",
    "params": {
      "command": "df -h /",
      "timeout_seconds": 30
    }
  }
}
```

Server tự fill các fields còn lại:

| Field | Default nếu thiếu |
|-------|------------------|
| `task_id` | `"task-<uuid4[:12]>"` (auto-generated) |
| `trace.hop_count` | `0` |
| `trace.route_path` | `[]` |

**Backward compatible:** Envelope đầy đủ từ v5.0–v5.5 vẫn hoạt động.

---

## 4. Async Workflow Pattern

Khi action trả về `job_id`:

```
POST /action
  → {"job_id": "node1-job-xyz", "status": "accepted", "estimated_completion_seconds": 60}

[Claude tiếp tục làm việc khác trong khi chờ]

GET /result/{job_id}
  → {"status": "running", "progress": 45}      ← chưa xong, làm tiếp

GET /result/{job_id}
  → {"status": "completed", "output": {...}}    ← xong, proceed
```

Claude **không bao giờ bị ép block** chờ 1 job. Đây là điểm cốt lõi của thiết kế.

Ngoại lệ duy nhất: Nếu bước tiếp theo phụ thuộc hoàn toàn vào kết quả của job hiện
tại **và** không có việc nào khác có thể làm song song, Claude có thể poll loop ngay:

```bash
while true; do
  STATUS=$(curl -s "$GATEWAY/result/$JOB_ID" -H "Authorization: Bearer $TOKEN" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] && break
  sleep 5
done
```

---

## 5. Claude Web Orchestration Loop

```
User đưa ra yêu cầu
     │
     ▼
Claude fetch /skills từ gateway      ← hiểu mesh có gì
     │
     ▼
Claude reasoning → quyết định action
     │
     ├─ POST /action
     │      │
     │      ├── sync  → output ngay → continue reasoning
     │      │
     │      └── async → job_id
     │                    │
     │                    ├── làm việc khác
     │                    └── poll /result khi cần
     │
     ├─ Cần info từ user? → hỏi → nhận info → continue
     │
     ├─ Cần node mới? → bootstrap via execute_command
     │
     └─ Hoàn tất → báo cáo kết quả cho user
```

---

## 6. Core Endpoints (không thay đổi từ v5.0)

| Endpoint | Method | Mô tả |
|----------|--------|-------|
| `/action` | POST | Gửi action — sync hoặc async |
| `/result/{job_id}` | GET | Poll job status |
| `/resolve/{node_id}` | GET | Resolve node address |
| `/skills` | GET | Node capabilities (Markdown) |
| `/health` | GET | Node health |
| `/ping` | GET | Reachability probe (v5.3) |

---

## 7. Worker Endpoints (v5.3, không thay đổi)

| Endpoint | Method | Mô tả |
|----------|--------|-------|
| `/nodes/register` | POST | Worker đăng ký với gateway |
| `/nodes/{node_id}/heartbeat` | POST | Heartbeat |
| `/nodes` | GET | List trusted nodes |
| `/jobs/poll` | GET | Worker poll jobs |
| `/jobs/{job_id}/claim` | POST | Atomic claim |
| `/jobs/{job_id}/result` | POST | Report result |

---

## 8. Action Request Model (v5.6)

```python
class ActionRequest(BaseModel):
    target_node_id: str                 # required
    payload: ActionPayload              # required
    task_id: str | None = None          # optional — auto-gen if absent
    trace: TraceInfo | None = None      # optional — auto-default if absent

    @model_validator(mode="after")
    def _fill_defaults(self) -> "ActionRequest":
        if self.task_id is None:
            self.task_id = f"task-{uuid.uuid4().hex[:12]}"
        if self.trace is None:
            self.trace = TraceInfo()
        return self
```

---

## 9. SYSTEM_PROMPT.md

File `SYSTEM_PROMPT.md` là entry point cho Claude Web. Operator:

1. Copy nội dung file.
2. Điền `GATEWAY` và `TOKEN`.
3. Paste vào Claude Project (Pro/Max) system prompt.
4. Đưa ra task — Claude tự xử lý.

---

## 10. Trạng thái hiện tại (v5.6)

**Đã có:**

- ✅ Bootstrap minimal (3 seed actions)
- ✅ Plugin-based node runtime
- ✅ Async job model (job_id-based)
- ✅ Distributed routing (mesh + loop protection)
- ✅ Skill discovery (Markdown)
- ✅ Resolve mechanism
- ✅ Authentication layer (Bearer token)
- ✅ Action schema validation (JSON Schema)
- ✅ Job TTL & cleanup
- ✅ Auto rollback (bootstrap)
- ✅ One-liner setup.sh
- ✅ Push/Pull delivery (NAT support)
- ✅ NodeRegistry + WorkerAgent + JobQueue
- ✅ **Optional task_id + trace (v5.6)**
- ✅ **Claude Web curl-native workflow (v5.6)**

**Chưa có:**

- ❌ Background stale-node checker loop
- ❌ Container isolation per-node (Docker)
- ❌ Job queue persistence
- ❌ Pull job timeout
- ❌ Queue depth trong /health
- ❌ FastAPI lifespan migration (on_event deprecated)
- ❌ Idempotency key

---

*Spec: SPECS_V5.6.md | Mesh Runtime v5.6 | Repository: ai-infra-runtime-v2*
