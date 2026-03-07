# Hướng dẫn: Sử dụng Claude Web làm Orchestrator

**Project:** ai-infra-runtime-v2
**Version:** v5.4+
**Audience:** Operator dùng Claude.ai Pro/Max để điều phối mesh

---

## 1. Tư duy kiến trúc

### Trước (v5.4 — phức tạp thừa)

```
User → CloudPlanner → gọi OpenAI API → LLM ra quyết định → MeshClient → Gateway
                         ↑ tốn tiền, thêm latency, cần config LLM API key
```

### Sau (v5.4+ — đúng và đơn giản)

```
User → Claude Web (Pro/Max) → mesh_ctl.py → Gateway
            ↑
     Claude IS the LLM — không cần gọi API nào thêm.
     Reasoning, planning, error handling đều built-in.
```

**`planner/planner.py` và `planner/cli.py` là thừa** khi dùng Claude Web.
Giữ lại `mesh_ctl.py` — đây là "tay" để Claude gọi gateway.

---

## 2. Setup (một lần)

### 2.1 Trên máy Debian (gateway)

```bash
# Cài httpx cho mesh_ctl
pip install httpx

# Set env vars (hoặc dùng --gateway / --token flags mỗi lần)
export MESH_GATEWAY=http://localhost:8080
export MESH_TOKEN=your-secret-token

# Verify gateway đang chạy
python mesh_ctl.py health
```

Output:
```json
{
  "node_id": "node-0",
  "status": "healthy",
  "uptime_seconds": 142.3,
  "actions_loaded": 7,
  "jobs_active": 0
}
```

### 2.2 Chạy nodes (như hướng dẫn v5.3)

```bash
# Debian — Gateway
python node_runtime.py --config node-0/node.yaml

# CentOS — chạy lệnh này trên máy CentOS
curl -sSL http://DEBIAN_IP:8080/setup.sh | bash -s -- --node-id node-1 \
    --port 8080 --gateway http://DEBIAN_IP:8080 --auth-token your-secret-token --systemd

# AlmaLinux — chạy lệnh này trên máy AlmaLinux
curl -sSL http://DEBIAN_IP:8080/setup.sh | bash -s -- --node-id node-2 \
    --port 8080 --gateway http://DEBIAN_IP:8080 --auth-token your-secret-token --systemd
```

### 2.3 Fetch skills của các nodes

Trước khi nói chuyện với Claude, lấy skills để biết node nào có action gì:

```bash
python mesh_ctl.py skills node-0
python mesh_ctl.py skills node-1
python mesh_ctl.py skills node-2
```

Copy output vào context khi mở conversation với Claude.

---

## 3. Cách làm việc với Claude Web

### 3.1 Prompt mở đầu (template)

Paste đoạn này vào đầu conversation với Claude:

```
Tôi có một Execution Mesh gồm 3 nodes. Bạn sẽ đóng vai orchestrator —
lần lượt ra lệnh cho tôi chạy `mesh_ctl.py` và dựa vào output để quyết định bước tiếp theo.

**Cách hoạt động:**
1. Bạn đưa ra lệnh `python mesh_ctl.py ...` (một lệnh mỗi lần)
2. Tôi chạy và paste kết quả JSON vào đây
3. Bạn đọc kết quả, reasoning, và đưa ra lệnh tiếp theo
4. Lặp lại cho đến khi task hoàn thành

**Thông tin hệ thống:**
- Gateway: node-0 (Debian, localhost:8080)
- node-1: CentOS, IP công khai, có MySQL, database "mydb" cần backup
- node-2: AlmaLinux, IP công khai, đích restore

**Skills của node-0:**
[paste output của: python mesh_ctl.py skills node-0]

**Skills của node-1:**
[paste output của: python mesh_ctl.py skills node-1]

**Skills của node-2:**
[paste output của: python mesh_ctl.py skills node-2]

**Nodes đang online:**
[paste output của: python mesh_ctl.py nodes]

---

**Task:** Backup database "mydb" trên node-1 (CentOS), lưu file về node-0 (Debian /backups/),
rồi restore trên node-2 (AlmaLinux). Verify số rows sau restore.
```

### 3.2 Vòng lặp làm việc

Sau prompt trên, Claude sẽ tự động dẫn dắt:

---

**Claude:**
> Tôi sẽ bắt đầu bằng việc dump database trên node-1. Chạy lệnh sau:
>
> ```bash
> python mesh_ctl.py run node-1 execute_command \
>   '{"command":"mysqldump -u root mydb > /tmp/mydb_$(date +%Y%m%d).sql","timeout_seconds":300}'
> ```

**Bạn:** *(chạy lệnh, paste output)*
```json
{
  "status": "completed",
  "job_id": "node1-job-a1b2c3d4",
  "output": {
    "exit_code": 0,
    "stdout": "",
    "stderr": ""
  }
}
```

**Claude:**
> Dump thành công. Tiếp theo verify file đã tạo và lấy tên chính xác:
>
> ```bash
> python mesh_ctl.py run node-1 execute_command \
>   '{"command":"ls -lh /tmp/mydb_*.sql | tail -1"}'
> ```

**Bạn:** *(paste output)*
```json
{
  "status": "completed",
  "output": {
    "exit_code": 0,
    "stdout": "-rw-r--r-- 1 root root 24M Mar 04 10:05 /tmp/mydb_20260304.sql\n"
  }
}
```

**Claude:**
> File 24MB tại `/tmp/mydb_20260304.sql`. Bây giờ đọc file dưới dạng Base64 để transfer:
>
> ```bash
> python mesh_ctl.py run node-1 read_file_b64 \
>   '{"path":"/tmp/mydb_20260304.sql"}'
> ```

*(... và cứ tiếp tục)*

---

### 3.3 Xử lý lỗi — Claude tự xử lý

Khi có lỗi, bạn chỉ cần paste output và Claude tự điều chỉnh:

```json
{
  "status": "failed",
  "output": {
    "exit_code": 1,
    "stderr": "mysqldump: Got error: 1045: Access denied for user 'root'@'localhost'"
  }
}
```

Claude sẽ tự đề xuất lệnh thay thế (thêm `-p`, dùng user khác, v.v.) mà không cần bạn can thiệp.

---

## 4. mesh_ctl.py — Reference

### Cú pháp

```bash
python mesh_ctl.py [--gateway URL] [--token TOKEN] <command> [args]
```

### Biến môi trường

| Biến | Default | Mô tả |
|------|---------|-------|
| `MESH_GATEWAY` | `http://localhost:8080` | Gateway URL |
| `MESH_TOKEN` | *(empty)* | Bearer auth token |
| `MESH_POLL_INTERVAL` | `3` | Giây giữa mỗi lần poll |
| `MESH_POLL_TIMEOUT` | `600` | Timeout tối đa chờ async job |

### Commands

#### `run` — Chạy action trên node

```bash
# Chạy và CHỜ kết quả (mặc định)
python mesh_ctl.py run <node_id> <action> '<params_json>'

# Chạy và trả ngay job_id (không chờ)
python mesh_ctl.py run <node_id> <action> '<params_json>' --no-wait
```

**Khi nào dùng `--no-wait`:** Action dự kiến chạy rất lâu (vài phút) và bạn muốn
làm việc khác song song, rồi poll sau. Thông thường không cần.

**Output khi hoàn thành:**
```json
{
  "status": "completed",
  "task_id": "claude-a1b2c3d4",
  "job_id": "node1-job-xxxxxxxx",
  "output": { ... }
}
```

**Output khi thất bại:**
```json
{
  "status": "failed",
  "job_id": "node1-job-xxxxxxxx",
  "error": "exit_code=1: command not found"
}
```

#### `result` — Poll job theo job_id

```bash
python mesh_ctl.py result <job_id>
```

Dùng khi đã chạy với `--no-wait` trước đó.

#### `nodes` — Danh sách nodes

```bash
python mesh_ctl.py nodes
```

```json
{
  "nodes": [
    { "node_id": "node-0", "status": "online",  "address": null, "last_heartbeat": null },
    { "node_id": "node-1", "status": "online",  "address": "http://1.2.3.4:8080", "last_heartbeat": 1709550000.0 },
    { "node_id": "node-2", "status": "unreachable", "address": "http://5.6.7.8:8080", "last_heartbeat": 1709549900.0 }
  ]
}
```

#### `health` — Health gateway

```bash
python mesh_ctl.py health
```

#### `skills` — Skills của node

```bash
python mesh_ctl.py skills node-0
```

Trả Markdown text trong JSON. Claude đọc phần `skills` để biết actions và params.

### Actions thường dùng (cho tham khảo khi nói chuyện với Claude)

| Action | Node | Params quan trọng |
|--------|------|-------------------|
| `execute_command` | any | `command`, `timeout_seconds` |
| `read_file` | any | `path` |
| `write_file` | any | `path`, `content` |
| `read_file_b64` | any | `path` |
| `write_file_b64` | any | `path`, `content_b64` |

---

## 5. Use Case đầy đủ — Backup/Transfer/Restore

### Prompt cho Claude

```
**Hệ thống:**
- node-0: Debian gateway, localhost:8080, auth token: your-secret-token
- node-1: CentOS, có MySQL, database "mydb"
- node-2: AlmaLinux, đích restore

**Task:** Backup "mydb" từ node-1, lưu về /backups/mydb_{date}.sql trên node-0,
restore trên node-2, verify số rows.

Hãy bắt đầu. Mỗi lần đưa cho tôi một lệnh mesh_ctl.py để chạy.
```

### Sequence Claude sẽ tự dẫn dắt

```
Lệnh 1: run node-1 execute_command 'dump DB'
  → exit_code: 0

Lệnh 2: run node-1 execute_command 'ls -lh dump file'
  → filename + size confirmed

Lệnh 3: run node-1 read_file_b64 '{path: dump file}'
  → {content_b64: "...", size_bytes: 25165824}

Lệnh 4: run node-0 write_file_b64 '{path: /backups/..., content_b64: <từ lệnh 3>}'
  → {success: true}

Lệnh 5: run node-2 write_file_b64 '{path: /tmp/..., content_b64: <từ lệnh 3>}'
  → {success: true}

Lệnh 6: run node-2 execute_command 'mysql restore'
  → exit_code: 0

Lệnh 7: run node-2 execute_command 'SELECT COUNT(*)'
  → stdout: "42000"

Claude: "✅ Hoàn thành. Backup 24MB, restore thành công, 42,000 rows verified."
```

Toàn bộ workflow **không cần LLM API, không cần config gì thêm** ngoài Claude.ai tab đang mở.

---

## 6. Tips

**Luôn paste raw JSON output** — đừng tóm tắt hay filter. Claude cần đọc toàn bộ
(đặc biệt stderr khi lỗi).

**File lớn (>50MB):** `content_b64` sẽ rất dài. Claude có context window đủ để xử lý,
nhưng nếu gặp vấn đề, dùng `execute_command` với `scp` thay thế:
```bash
# Trên node-1 (nếu đã setup SSH key sang node-0):
python mesh_ctl.py run node-1 execute_command \
  '{"command":"scp /tmp/backup.sql debian@DEBIAN_IP:/backups/"}'
```

**Nhiều tasks song song:** Mở nhiều tab Claude, mỗi tab một workflow riêng,
dùng node khác nhau.

**Debug:** Thêm `--log-level DEBUG` để xem request/response chi tiết:
```bash
# Không có flag đó trong mesh_ctl, nhưng dùng curl trực tiếp:
curl -s -H "Authorization: Bearer your-token" http://localhost:8080/nodes | python3 -m json.tool
```

---

*Document: CLAUDE_WEB_WORKFLOW.md | Mesh Runtime v5.4 | Repository: ai-infra-runtime-v2*
