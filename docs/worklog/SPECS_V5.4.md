# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Specification v5.4 — Transfer Actions + Cloud AI Planner

**Project:** ai-infra-runtime-v2
**Version:** 5.4
**Date:** 2026-03-04
**Supersedes:** SPECS_V5.3.md

---

## 1. Tổng quan

### 1.1 Vị trí trong roadmap

v5.4 khép lại hai gap quan trọng còn lại từ v5.0 để hệ thống vận hành end-to-end:

| Gap | Vấn đề | Giải pháp v5.4 |
|-----|--------|----------------|
| **Binary transfer** | Không thể chuyển file binary (dump, archive) giữa các node qua JSON/UTF-8 | `read_file_b64` + `write_file_b64` — Base64 encoding |
| **Orchestration** | Không có gì tự động điều phối multi-step workflow — mọi thứ phải gọi API thủ công | Cloud AI Planner — ReAct loop với LLM |

### 1.2 Kiến trúc tổng thể sau v5.4

```
┌──────────────────────────────────────────────────────────────────┐
│                        User / Operator                           │
│              (natural language task description)                 │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Cloud AI Planner                              │
│  ┌─────────────┐   ┌──────────────┐   ┌───────────────────────┐ │
│  │SkillsCache  │   │ CloudPlanner │◄──│      LLM API          │ │
│  │ (node caps) │──►│  ReAct Loop  │   │  (OpenAI-compatible)  │ │
│  └─────────────┘   └──────┬───────┘   └───────────────────────┘ │
│                           │                                      │
│                    ┌──────▼───────┐                             │
│                    │ MeshClient   │                             │
│                    │ (HTTP async) │                             │
└────────────────────┴──────┬───────┴─────────────────────────────┘
                             │  POST /action
                             │  GET  /result/{job_id}
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                 Gateway Node (node-0, Debian)                    │
│                                                                  │
│  ┌───────────────┐  ┌───────────────┐  ┌──────────────────────┐ │
│  │ GatewayRouter │  │ NodeRegistry  │  │      JobQueue        │ │
│  │ (push/pull)   │  │ (trust + HB)  │  │  (per-node queue)    │ │
│  └───────┬───────┘  └───────────────┘  └──────────────────────┘ │
└──────────┼───────────────────────────────────────────────────────┘
           │
           ├─ Push (public IP) ──────────► node-1 (CentOS)
           │                               node-2 (AlmaLinux)
           │
           └─ Pull (NAT/LAN) ◄──────────  node-3 (polls gateway)
                 job queue                 WorkerAgent
```

---

## 2. Transfer Actions

### 2.1 Vấn đề với `read_file` / `write_file`

Actions cũ dùng `Path.read_text(encoding="utf-8")` và `content: str` — vỡ hoàn toàn với:
- Database dump (`.sql`) chứa binary data
- Compressed archive (`.sql.gz`, `.tar.gz`)
- Bất kỳ file nào có byte ngoài ASCII printable

Hệ quả: `UnicodeDecodeError` khi đọc, data corruption khi truyền qua JSON, không có cách
ghi binary bytes từ LLM output.

### 2.2 `read_file_b64`

**File:** `seed/actions/read_file_b64.py`
**Mode:** Synchronous

Đọc file bằng `Path.read_bytes()` → `base64.b64encode()` → ASCII string.
Hoạt động với mọi loại file: text, binary, compressed, encrypted.

**Input:**
```json
{ "path": "/var/backups/mydb.sql.gz" }
```

**Output:**
```json
{
  "content_b64": "H4sIAAAAAAAAA+xdW2/bOBZ+...",
  "size_bytes":  52428800,
  "path":        "/var/backups/mydb.sql.gz"
}
```

**Errors:**
- `ValueError`: `path` không được truyền vào
- `FileNotFoundError`: File không tồn tại

### 2.3 `write_file_b64`

**File:** `seed/actions/write_file_b64.py`
**Mode:** Synchronous

Nhận Base64 string → `base64.b64decode(validate=True)` → `Path.write_bytes()`.
Tự tạo parent directories nếu chưa tồn tại.
Validate Base64 trước khi ghi — lỗi rõ ràng nếu content bị corrupt.

**Input:**
```json
{
  "path":        "/backups/mydb.sql.gz",
  "content_b64": "H4sIAAAAAAAAA+xdW2/bOBZ+..."
}
```

**Output:**
```json
{
  "success":    true,
  "path":       "/backups/mydb.sql.gz",
  "size_bytes": 52428800
}
```

**Errors:**
- `ValueError`: Thiếu `path` hoặc `content_b64`
- `ValueError("Invalid Base64 content: ...")`: Content không phải Base64 hợp lệ

### 2.4 Transfer Pattern

Để chuyển file từ **node A** sang **node B**, Planner thực hiện 2 bước liên tiếp:

```
Step N   — target: node-A
           action: read_file_b64
           params: { "path": "/source/mydb.sql.gz" }
        ← output: { "content_b64": "...", "size_bytes": 52428800 }

Step N+1 — target: node-B
           action: write_file_b64
           params: {
             "path":        "/dest/mydb.sql.gz",
             "content_b64": "<Step N output: content_b64>"
           }
        ← output: { "success": true, "size_bytes": 52428800 }
```

LLM tự động re-use `content_b64` từ Step N vào params của Step N+1 vì nó nằm trong
conversation history dưới dạng OBSERVATION.

**Lưu ý về kích thước:** Base64 tăng payload ~33%. File 100 MB → JSON ~133 MB.
Với file rất lớn (>200 MB) khuyến nghị dùng `execute_command` với `scp`/`rsync`
nếu SSH key đã được setup sẵn giữa các node.

---

## 3. Cloud AI Planner

### 3.1 Tổng quan và vị trí

Planner là điểm vào duy nhất mà người dùng tương tác. Nó nhận task bằng ngôn ngữ
tự nhiên, tự động reasoning, và gọi các actions trên các node thích hợp cho đến khi
task hoàn thành — mà không cần người dùng biết gì về cấu trúc mesh bên dưới.

**Planner KHÔNG:**
- Trực tiếp SSH hay kết nối vào bất kỳ node nào
- Biết địa chỉ cụ thể của worker node (chỉ biết `node_id`)
- Chứa bất kỳ business logic cứng (hardcoded workflow) nào

**Planner CHỈ:**
- Gọi `POST /action` trên gateway
- Poll `GET /result/{job_id}` để lấy output
- Dùng LLM để quyết định action tiếp theo dựa trên output vừa nhận

### 3.2 Cấu trúc package

```
mesh/planner/
├── __init__.py        — empty
├── config.py          — PlannerConfig dataclass + load_config()
├── mesh_client.py     — MeshClient: HTTP wrapper cho gateway API
├── skills_cache.py    — SkillsCache: fetch + cache /skills từ các node
├── planner.py         — CloudPlanner: ReAct engine chính
├── cli.py             — CLI: python -m planner.cli ...
└── planner.yaml       — Config template (copy và điền trước khi dùng)
```

### 3.3 `config.py` — PlannerConfig

Load từ `planner/planner.yaml`, tạo ra `PlannerConfig` dataclass typed.

```python
@dataclass
class NodeEntry:
    node_id:     str
    address:     str | None  # HTTP address để fetch /skills
    skills_text: str | None  # Fallback Markdown nếu không fetch được

@dataclass
class PlannerConfig:
    gateway_url:            str          # URL của gateway node
    gateway_token:          str | None   # Bearer token (cùng auth_token gateway)
    llm_base_url:           str          # OpenAI-compatible endpoint
    llm_api_key:            str
    llm_model:              str          # default: "gpt-4o"
    llm_timeout_seconds:    float        # default: 120.0
    max_iterations:         int          # default: 20
    poll_interval_seconds:  float        # default: 3.0
    poll_max_wait_seconds:  float        # default: 600.0
    nodes:                  list[NodeEntry]
```

**`planner.yaml` đầy đủ:**

```yaml
# ── Gateway ──────────────────────────────────────────────────────────────────
gateway_url:   http://localhost:8080
gateway_token: your-secret-token      # khớp với auth_token trong node-0/node.yaml

# ── LLM ──────────────────────────────────────────────────────────────────────
llm:
  base_url: https://api.openai.com/v1
  api_key:  sk-...
  model:    gpt-4o
  timeout_seconds: 120

# Providers được hỗ trợ (bất kỳ OpenAI-compatible endpoint):
#   OpenAI:          base_url: https://api.openai.com/v1, model: gpt-4o
#   Anthropic qua LiteLLM: base_url: http://localhost:4000/v1, model: claude-3-5-sonnet
#   Ollama local:    base_url: http://localhost:11434/v1,  model: llama3.3
#   vLLM self-host:  base_url: http://localhost:8000/v1,  model: <model name>

# ── ReAct loop ───────────────────────────────────────────────────────────────
max_iterations:        25   # tối đa bao nhiêu iteration trước khi báo thất bại
poll_interval_seconds:  3   # tần suất poll GET /result/{job_id}
poll_max_wait_seconds: 600  # timeout tối đa cho một async job đơn lẻ

# ── Nodes ────────────────────────────────────────────────────────────────────
# address:     để fetch /skills trực tiếp — dùng cho node có public IP
# skills_text: fallback Markdown — dùng cho node sau NAT không fetch được
nodes:
  - node_id: node-0
    address:  http://localhost:8080

  - node_id: node-1
    address:  http://CENTOS_PUBLIC_IP:8080

  - node_id: node-2
    address:  http://ALMALINUX_PUBLIC_IP:8080

  # Ví dụ node sau NAT — cung cấp skills_text thủ công:
  # - node_id: node-3
  #   skills_text: |
  #     # Node: node-3 (Worker — LAN only)
  #     ## Capabilities
  #     ### Action: execute_command
  #     ...
```

### 3.4 `skills_cache.py` — SkillsCache

Skills là Markdown text mô tả capabilities của từng node. Được inject vào LLM system
prompt để LLM biết: node nào làm được gì, input/output format, sync hay async.

**Fetch strategy (theo thứ tự ưu tiên):**

```
for each node in config.nodes:
    if node.address is set:
        GET {node.address}/skills  →  HTTP 200  →  dùng response text
                                   →  HTTP 4xx/5xx hoặc timeout  →  bước tiếp
    if node.skills_text is set:
        dùng skills_text từ config
    else:
        log WARNING, bỏ qua node (không đưa vào prompt)
```

**`SkillsCache.build_prompt_section()`** — tạo block Markdown đưa vào system prompt:

```markdown
## Skills for `node-0`

# Node: node-0 (Seed Node / Gateway)

### Action: execute_command
...

---

## Skills for `node-1`

# Node: node-1 (CentOS Worker)
...
```

M��i node được phân cách bằng `---` để LLM dễ phân biệt context.

### 3.5 `planner.py` — CloudPlanner (ReAct Engine)

Đây là component cốt lõi của hệ thống. Hiện thực vòng lặp
**Reason → Act → Observe** cho đến khi hoàn thành.

#### 3.5.1 ReAct Loop — Chi tiết từng bước

```
CloudPlanner.run(task: str) → PlannerResult
│
├─ Khởi tạo
│   ├── Build system_prompt (một lần, không thay đổi suốt loop)
│   └── messages = [ { role: "user", content: task } ]
│
└─ for iteration in range(1, max_iterations + 1):
    │
    ├─ [THINK] Gọi LLM
    │   POST {llm_base_url}/chat/completions
    │   body = {
    │     model: ...,
    │     messages: [system_prompt] + messages,   ← full conversation
    │     temperature: 0.2,
    │     response_format: { type: "json_object" }
    │   }
    │   → llm_reply: str (JSON string)
    │
    ├─ [PARSE] json.loads(llm_reply)
    │   Nếu JSONDecodeError:
    │     observation = "[PARSE ERROR] ..."
    │     → append assistant+user messages
    │     → continue (không tăng action count)
    │
    ├─ [DECIDE] Kiểm tra keys trong parsed:
    │
    │   "done" == true
    │   └── return PlannerResult(success=True, summary=parsed["summary"])
    │
    │   "error" == true
    │   └── return PlannerResult(success=False, summary=parsed["message"])
    │
    │   "action" present
    │   └─ [ACT] MeshClient.execute(
    │               target_node_id = parsed["action"]["target_node_id"],
    │               action         = parsed["action"]["action"],
    │               params         = parsed["action"]["params"]
    │            )
    │            │
    │            ├── Success → output dict
    │            │   observation = "[OBSERVATION] Action succeeded. Output: {json}"
    │            │
    │            ├── JobFailedError  → observation = "[OBSERVATION] Action failed: ..."
    │            ├── JobTimeoutError → observation = "[OBSERVATION] Action failed: ..."
    │            └── Exception       → observation = "[OBSERVATION] Unexpected error: ..."
    │
    └─ [OBSERVE] Append vào conversation:
        messages.append({ role: "assistant", content: llm_reply    })
        messages.append({ role: "user",      content: observation  })
        → LLM đọc observation này ở iteration tiếp theo

Nếu hết max_iterations:
    return PlannerResult(success=False, summary="Max iterations reached")
```

#### 3.5.2 System Prompt

Built một lần trước vòng lặp. Có 5 phần:

```
┌──────────────────────────────────────────────────────────────┐
│ SYSTEM PROMPT                                                │
│                                                              │
│ 1. ROLE                                                      │
│    "You are a Cloud AI Planner controlling a distributed     │
│     Execution Mesh..."                                       │
│                                                              │
│ 2. MESH ARCHITECTURE                                         │
│    Gateway URL: {gateway_url}                                │
│    "You ONLY communicate via the gateway..."                 │
│                                                              │
│ 3. NODE SKILLS     ← từ SkillsCache.build_prompt_section()  │
│    ## Skills for `node-0`                                   │
│    ## Skills for `node-1`                                   │
│    ...                                                       │
│                                                              │
│ 4. RESPONSE FORMAT CONTRACT                                  │
│    3 JSON templates: action / done / error                   │
│                                                              │
│ 5. RULES                                                     │
│    - One action per response                                 │
│    - Always check exit_code == 0                             │
│    - File transfer: read_file_b64 → write_file_b64           │
│    - DB backup/restore commands                              │
│    - Retry once on failure                                   │
│    - Max {max_iterations} iterations                         │
└──────────────────────────────────────────────────────────────┘
```

#### 3.5.3 LLM Response Contract

Planner yêu cầu `response_format: { type: "json_object" }` — LLM PHẢI trả JSON thuần,
không có markdown fence, không có preamble.

**Type 1 — Execute action** (LLM muốn thực hiện một bước):

```json
{
  "thought": "The database needs to be dumped first using mysqldump on node-1 (CentOS). I'll use execute_command with a 5-minute timeout since the database might be large.",
  "action": {
    "target_node_id": "node-1",
    "action":         "execute_command",
    "params": {
      "command":         "mysqldump -u root mydb > /tmp/mydb_20260304.sql",
      "timeout_seconds": 300
    }
  }
}
```

**Type 2 — Signal completion** (tất cả bước đã xong):

```json
{
  "thought": "All steps have been completed successfully. The database was backed up on node-1, transferred via gateway, and restored on node-2. Row count verified at 42,000 which matches the source.",
  "done":    true,
  "summary": "Database 'mydb' (24 MB) backed up from CentOS node-1, saved to /backups/mydb_20260304.sql on gateway node-0, and restored on AlmaLinux node-2. Verified 42,000 rows."
}
```

**Type 3 — Signal error** (không thể tiếp tục):

```json
{
  "thought": "The restore command failed with 'ERROR 1049 (42000): Unknown database mydb'. The database does not exist on node-2. I cannot create it automatically without knowing the schema and charset.",
  "error":   true,
  "message": "Target database 'mydb' does not exist on node-2. Create it manually with 'CREATE DATABASE mydb;' then retry."
}
```

**Type 4 — Malformed** (không có key nào trong ba loại trên):
Planner inject: `[ERROR] Response has neither 'action', 'done', nor 'error' key.`
và tiếp tục iteration.

#### 3.5.4 Conversation State

Messages array được build incremental theo từng iteration:

```
messages = [
  { role: "user",      content: "Backup mydb on node-1, restore on node-2" },

  { role: "assistant", content: '{"thought":"Step 1: dump...","action":{...}}' },
  { role: "user",      content: "[OBSERVATION] Action succeeded. Output: {\"exit_code\":0,...}" },

  { role: "assistant", content: '{"thought":"Step 2: read file...","action":{...}}' },
  { role: "user",      content: "[OBSERVATION] Action succeeded. Output: {\"content_b64\":\"...\",\"size_bytes\":24000000}" },

  { role: "assistant", content: '{"thought":"Step 3: write to node-0...","action":{...}}' },
  { role: "user",      content: "[OBSERVATION] Action succeeded. Output: {\"success\":true}" },
  ...
]
```

Toàn bộ conversation history được gửi lên LLM mỗi iteration. LLM có full context của
mọi bước đã làm, output đã nhận, và có thể re-use output cũ trong params của action tiếp theo
(quan trọng cho file transfer — re-use `content_b64`).

#### 3.5.5 PlannerResult

```python
@dataclass
class PlannerResult:
    success:          bool            # True nếu LLM signal "done"
    summary:          str             # Message cuối (done.summary hoặc error.message)
    iterations:       int             # Số iteration đã thực hiện
    elapsed_seconds:  float           # Tổng thời gian từ đầu đến cuối
    history: list[dict] = field(...)  # Chi tiết từng iteration
```

**Cấu trúc mỗi entry trong `history`:**

```json
{
  "iteration": 3,
  "thought":   "Step 3: transfer file to gateway...",
  "action": {
    "target_node_id": "node-0",
    "action":         "write_file_b64",
    "params":         { "path": "/backups/...", "content_b64": "..." }
  },
  "output": { "success": true, "size_bytes": 24000000 }
}
```

Nếu action thất bại, `"output"` được thay bằng `"error": "JobFailedError: ..."`.

#### 3.5.6 LLM parameters

```python
{
  "model":           config.llm_model,
  "messages":        [system] + messages,
  "max_tokens":      1024,
  "temperature":     0.2,        # thấp → deterministic, ít hallucination
  "response_format": { "type": "json_object" }
}
```

`temperature: 0.2` — thấp hơn mức mặc định để LLM ra quyết định nhất quán,
không "sáng tạo" quá khi chọn action.

### 3.6 `mesh_client.py` — MeshClient

HTTP client async bọc toàn bộ giao tiếp với gateway. CloudPlanner chỉ gọi
`mesh_client.execute()` — không biết gì về HTTP, polling, hay job_id.

#### 3.6.1 `execute()` — Action execution

```
execute(target_node_id, action, params, task_id?) → dict

    ├── POST {gateway_url}/action
    │   payload = {
    │     "target_node_id": target_node_id,
    │     "task_id":        f"planner-{uuid4().hex[:8]}",
    │     "payload":        { "action": action, "params": params }
    │   }
    │
    ├── Response: { "error": "..." }
    │   └── raise MeshError("Action error: ...")
    │
    ├── Response: { "job_id": "...", "status": "accepted" }
    │   └── _poll(job_id)  ──────────────────────────────────────┐
    │                                                             │
    └── Response: { "status": "completed", "output": {...} }     │
        └── return output dict                                    │
                                                                  │
_poll(job_id):  ◄─────────────────────────────────────────────────┘
    deadline = now + poll_max_wait_seconds
    while now < deadline:
        sleep(poll_interval_seconds)
        GET {gateway_url}/result/{job_id}
        "completed" → return output dict
        "failed"    → raise JobFailedError
        other       → continue polling
    raise JobTimeoutError
```

**Từ góc nhìn CloudPlanner**: mọi action đều là `await mesh.execute(...)` → `dict`.
Không phân biệt sync hay async — MeshClient lo hết.

#### 3.6.2 Exceptions

| Exception | Khi nào | Xử lý trong Planner |
|-----------|---------|---------------------|
| `MeshError` | Gateway trả `{"error": "..."}` | Inject `[OBSERVATION] Action failed: ...` → LLM retry/error |
| `JobFailedError` | Job async chuyển sang `failed` | Inject `[OBSERVATION] Action failed: ...` → LLM retry/error |
| `JobTimeoutError` | Polling vượt `poll_max_wait_seconds` | Inject `[OBSERVATION] Action failed: ...` → LLM signal error |
| `Exception` (unexpected) | Network error, JSON parse... | Inject `[OBSERVATION] Unexpected error: ...` |

#### 3.6.3 Các methods khác

```python
fetch_skills(node_address: str) → str    # GET {addr}/skills → Markdown text
list_nodes() → list[dict]                # GET {gateway}/nodes → node list
health() → dict                          # GET {gateway}/health → health info
```

### 3.7 `cli.py` — CLI Entry Point

**Single task mode:**

```bash
python -m planner.cli --config planner/planner.yaml \
  "Backup database mydb on CentOS (node-1), save to /backups/ on Debian (node-0), restore on AlmaLinux (node-2)"
```

**Interactive REPL:**

```bash
python -m planner.cli --config planner/planner.yaml --interactive
```

```
🌐 Execution Mesh — Cloud AI Planner (interactive)
   Gateway : http://localhost:8080
   Model   : gpt-4o

🔍 Loading node skills...
   Nodes   : ['node-0', 'node-1', 'node-2']

task> backup mydb from node-1 and restore on node-2
🚀 Task: backup mydb from node-1...
──────────────────────────────────────────────────
2026-03-04 10:00:01 [INFO] ── Iteration 1/25 ──
2026-03-04 10:00:03 [INFO] → node-1/execute_command {...}
2026-03-04 10:00:03 [INFO] ⏳ polling job node1-job-a1b2 ...
2026-03-04 10:00:19 [INFO] ← job node1-job-a1b2 completed
...
──────────────────────────────────────────────────
✅ SUCCESS (7 iterations, 52.4s)
   Database 'mydb' backed up from node-1, transferred, restored on node-2.

task> check disk space on all nodes
...
```

**Options:**

| Flag | Default | Mô tả |
|------|---------|-------|
| `--config PATH` | `planner/planner.yaml` | Config file path |
| `--interactive` / `-i` | off | Bật REPL |
| `--log-level LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `task` (positional) | — | Task string (bắt buộc nếu không `--interactive`) |

**Exit codes:** `0` = success, `1` = failure (dùng được trong shell scripts).

---

## 4. Use Case End-to-End: Database Backup / Transfer / Restore

### 4.1 Topology

```
Internet
    ├── CentOS   (public IP) → node-1  — source database
    └── AlmaLinux (public IP) → node-2  — target (restore)

LAN
    └── Debian (gateway)     → node-0  — Planner chạy ở đây
```

### 4.2 Setup

**1. Start gateway (Debian):**

```bash
cd ~/ai-infra-runtime-v2/mesh
pip install -r requirements.txt
python node_runtime.py --config node-0/node.yaml
```

**2. Deploy node-1 (CentOS — gõ trên máy CentOS):**

```bash
curl -sSL http://DEBIAN_IP:8080/setup.sh | bash -s -- \
    --node-id node-1 \
    --port 8080 \
    --gateway http://DEBIAN_IP:8080 \
    --auth-token your-secret-token \
    --systemd
```

**3. Deploy node-2 (AlmaLinux — gõ trên máy AlmaLinux):**

```bash
curl -sSL http://DEBIAN_IP:8080/setup.sh | bash -s -- \
    --node-id node-2 \
    --port 8080 \
    --gateway http://DEBIAN_IP:8080 \
    --auth-token your-secret-token \
    --systemd
```

**4. Verify nodes registered:**

```bash
curl -s -H "Authorization: Bearer your-secret-token" \
     http://localhost:8080/nodes | python3 -m json.tool
# → node-1: online, node-2: online
```

**5. Configure Planner (`planner/planner.yaml`):**

```yaml
gateway_url:   http://localhost:8080
gateway_token: your-secret-token
llm:
  api_key: sk-...
  model:   gpt-4o
nodes:
  - node_id: node-0
    address:  http://localhost:8080
  - node_id: node-1
    address:  http://CENTOS_IP:8080
  - node_id: node-2
    address:  http://ALMALINUX_IP:8080
```

**6. Run task:**

```bash
python -m planner.cli --config planner/planner.yaml \
  "Backup database mydb on node-1 (CentOS), save the backup file to /backups/ on node-0 (Debian), then restore it on node-2 (AlmaLinux). Verify row count after restore."
```

### 4.3 Planner execution trace (expected)

```
Iteration 1 — Dump database
  → node-1 / execute_command
    command: "mysqldump -u root mydb > /tmp/mydb_20260304.sql"
    timeout: 300s
  ← { exit_code: 0, stdout: "", stderr: "" }

Iteration 2 — Verify dump
  → node-1 / execute_command
    command: "ls -lh /tmp/mydb_20260304.sql"
  ← { exit_code: 0, stdout: "-rw-r--r-- 1 root root 24M ..." }

Iteration 3 — Read dump file (Base64)
  → node-1 / read_file_b64
    path: "/tmp/mydb_20260304.sql"
  ← { content_b64: "...", size_bytes: 25165824 }

Iteration 4 — Write dump to gateway
  → node-0 / write_file_b64
    path: "/backups/mydb_20260304.sql"
    content_b64: <reuse from iteration 3>
  ← { success: true, size_bytes: 25165824 }

Iteration 5 — Write dump to AlmaLinux
  → node-2 / write_file_b64
    path: "/tmp/mydb_20260304.sql"
    content_b64: <reuse from iteration 3>
  ← { success: true, size_bytes: 25165824 }

Iteration 6 — Restore database
  → node-2 / execute_command
    command: "mysql -u root mydb < /tmp/mydb_20260304.sql"
    timeout: 300s
  ← { exit_code: 0, stdout: "", stderr: "" }

Iteration 7 — Verify row count
  → node-2 / execute_command
    command: "mysql -u root -sN -e 'SELECT COUNT(*) FROM mydb.users;'"
  ← { exit_code: 0, stdout: "42000" }

Iteration 8 — Signal done
  ← done: true
     summary: "Database 'mydb' (24 MB) backed up from CentOS (node-1),
               saved to /backups/mydb_20260304.sql on gateway (node-0),
               and restored on AlmaLinux (node-2). Verified: 42,000 rows."

✅ SUCCESS (8 iterations, 61.2s)
```

---

## 5. Node Configuration Reference

### 5.1 Gateway node (`node-0/node.yaml`)

```yaml
node_id: node-0
listen:  0.0.0.0:8080
nodes:
  node-0: http://127.0.0.1:8080
default_resolver: node-0
auth_token:       your-secret-token
trusted_nodes:
  - node-1
  - node-2
heartbeat_timeout_seconds: 60
ping_timeout_seconds:       5
```

### 5.2 Worker node — public IP

```yaml
node_id:       node-1
listen:        0.0.0.0:8080
self_address:  http://CENTOS_PUBLIC_IP:8080   # gateway sẽ dùng push mode
gateway_node_id:  node-0
gateway_address:  http://DEBIAN_IP:8080
auth_token:       your-secret-token
heartbeat_interval_seconds: 10
poll_interval_seconds:       5
```

### 5.3 Worker node — sau NAT

```yaml
node_id:       node-3
listen:        0.0.0.0:8080
# self_address không set → luôn pull mode
gateway_node_id:  node-0
gateway_address:  http://DEBIAN_IP:8080
auth_token:       your-secret-token
heartbeat_interval_seconds: 10
poll_interval_seconds:       5
```

---

## 6. Seed Actions Reference (v5.4 đầy đủ)

| Action | Mode | Mô tả |
|--------|------|-------|
| `write_file` | Sync | Ghi text file |
| `read_file` | Sync | Đọc text file (UTF-8 only) |
| `read_file_b64` | Sync | **NEW** — Đọc bất kỳ file, trả Base64 |
| `write_file_b64` | Sync | **NEW** — Nhận Base64, ghi raw bytes |
| `execute_command` | Async | Shell command với timeout |
| `generate_image` | Async | Text-to-image qua DALL-E |
| `llm_chat` | Async | LLM completion |

---

## 7. API Reference đầy đủ (v5.4)

| Endpoint | Method | Auth | Từ version |
|----------|--------|------|-----------|
| `POST /action` | POST | ✅ | v5.0 |
| `GET /result/{job_id}` | GET | ✅ | v5.0 |
| `GET /resolve/{node_id}` | GET | ✅ | v5.0 |
| `GET /skills` | GET | ❌ | v5.0 |
| `GET /health` | GET | ❌ | v5.0 |
| `POST /bootstrap` | POST | ✅ | v5.1 |
| `GET /setup.sh` | GET | ❌ | v5.2 |
| `GET /runtime-bundle` | GET | ❌ | v5.2 |
| `GET /ping` | GET | ❌ | v5.3 |
| `POST /nodes/register` | POST | ✅ | v5.3 |
| `POST /nodes/{id}/heartbeat` | POST | ✅ | v5.3 |
| `GET /nodes` | GET | ✅ | v5.3 |
| `GET /jobs/poll` | GET | ✅ | v5.3 |
| `POST /jobs/{id}/claim` | POST | ✅ | v5.3 |
| `POST /jobs/{id}/result` | POST | ✅ | v5.3 |

---

## 8. Trạng thái hệ thống

### Đã hoàn thành (v5.0 → v5.4)

- ✅ Seed node bootstrap + 7 actions
- ✅ Plugin-based action loader
- ✅ Async job model (job_id polling)
- ✅ Distributed routing với hop/loop protection
- ✅ Skill discovery (`/skills` Markdown)
- ✅ Auth Bearer token toàn hệ thống
- ✅ JSON Schema validation per-action
- ✅ Job TTL & automatic cleanup
- ✅ Bootstrap workflow + auto rollback
- ✅ Universal `setup.sh` (mọi Linux distro)
- ✅ LLM client (OpenAI-compatible, shared injection)
- ✅ Push/Pull job model (NAT support)
- ✅ Node registry + heartbeat tracking
- ✅ Binary-safe file transfer (v5.4)
- ✅ Cloud AI Planner — ReAct loop (v5.4)

### Roadmap (chưa implement)

| Priority | Item |
|----------|------|
| P2 | Heartbeat stale-checker background task |
| P2 | Container isolation per-node (Docker) |
| P3 | Persistent job store (SQLite/Redis) |
| P3 | Pull job timeout |
| P3 | Planner task history persistence |
| P3 | Queue depth trong `/health` |
| P3 | Idempotency key (tránh double-enqueue khi LLM retry) |
| P4 | FastAPI lifespan migration (thay `on_event`) |
| P4 | OpenTelemetry distributed tracing |

---

## 9. Changelog

| Version | Ngày | Nội dung |
|---------|------|---------|
| v5.0 | 2026-03-01 | Core runtime, 3 seed actions, async jobs, routing mesh |
| v5.1 | 2026-03-01 | Auth, schema validation, job TTL, bootstrap + auto rollback |
| v5.2 | 2026-03-01 | setup.sh, /runtime-bundle, shared LLM client |
| v5.3 | 2026-03-04 | Push/Pull model, node registry, heartbeat, job queue |
| **v5.4** | **2026-03-04** | **Binary transfer actions + Cloud AI Planner** |

---

*Document: SPECS_V5.4.md | Mesh Runtime v5.4 | Repository: ai-infra-runtime-v2*
