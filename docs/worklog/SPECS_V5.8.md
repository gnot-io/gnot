# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v5.8
### (File Transfer — Upload / Download)

---

## 1. Thay đổi so với v5.7

| Thành phần | v5.7 | v5.8 |
|-----------|------|------|
| File transfer | Không có — read_file/write_file qua JSON payload, giới hạn vài KB | `POST /upload` + `GET /download/{file_id}` — binary stream, lên đến 512 MB |
| File inventory | Không có | `GET /files` — list staged uploads với metadata |
| Cleanup | Không có | `DELETE /files/{file_id}` + lazy TTL + startup sweep |
| Dep mới | — | `python-multipart` (FastAPI multipart/form-data) |

---

## 2. Vấn đề được giải quyết

Use case thực chiến: **backup database trên máy sau NAT → transfer qua gateway → restore trên máy khác sau NAT**.

```
Company X LAN                 Internet / Debian Gateway        Company Y LAN
┌─────────────┐  outbound    ┌───────────────────────────┐    ┌─────────────┐
│  CentOS     │─────────────▶│  node-0  203.x.x.x:8080   │◀───│  AlmaLinux  │
│  DB server  │  pull mode   │  POST /upload              │    │  restore    │
│             │              │  GET  /download/{id}        │    │  pull mode  │
└─────────────┘              └───────────────────────────┘    └─────────────┘
```

Cả hai worker đều sau NAT — gateway là trung gian duy nhất. File **không** đi qua JSON payload (không thể với file GB), mà được stream qua HTTP multipart/octet-stream.

---

## 3. Kiến trúc UploadManager

```
Gateway node (node-0)
  ┌─────────────────────────────────────────┐
  │ UploadManager                           │
  │  upload_dir: /tmp/mesh-uploads/         │
  │  max_size:   512 MB (configurable)      │
  │  ttl:        3600s  (configurable)      │
  │                                         │
  │  store(filename, bytes) → _UploadEntry  │
  │    → file_id_{safe_name} on disk        │
  │    → entry in _meta dict                │
  │                                         │
  │  get(file_id) → (Path, entry) | None    │
  │    → lazy TTL check                     │
  │                                         │
  │  sweep_expired() → int                  │
  │    → called on startup                  │
  │    → removes expired + orphan files     │
  └─────────────────────────────────────────┘
```

**Lazy TTL:** không có background sweep loop. TTL được kiểm tra tại `get()` và `list_files()`. Nếu file đã quá hạn → xóa ngay và trả `None`. Consistent với pattern của v5.7 (lazy staleness check, lazy timeout check).

**File ID:** `uuid4().hex[:16]` — không content-addressed. Hai lần upload cùng file → hai file_id độc lập, hai TTL độc lập.

**Filename sanitization:** strip path traversal (`../../etc/passwd` → `passwd`), replace ký tự đặc biệt, giới hạn 128 chars.

---

## 4. API Endpoints (v5.8)

### POST /upload

```
Content-Type: multipart/form-data
Field: file (UploadFile)
Header (optional): X-Node-ID: centos-dbserver   ← để track uploader

Response 201:
{
  "file_id":      "a3f9b2c1d4e5f678",
  "filename":     "backup_20260304.sql.gz",
  "size_bytes":   1073741824,
  "ttl_seconds":  3600,
  "download_url": "/download/a3f9b2c1d4e5f678"
}

Response 413: {"error": "FILE_TOO_LARGE", "max_size_mb": 512}
Response 400: {"error": "EMPTY_FILE"}
```

Cách worker gọi qua `execute_command`:
```bash
curl -s -X POST http://203.x.x.x:8080/upload \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Node-ID: centos-dbserver" \
  -F "file=@/tmp/backup_20260304.sql.gz"
```

### GET /download/{file_id}

```
Response 200: application/octet-stream (FileResponse — streamed, không load vào RAM)
  Content-Disposition: attachment; filename="backup_20260304.sql.gz"

Response 404: {"error": "FILE_NOT_FOUND", "file_id": "..."}
              (cũng trả 404 nếu TTL hết hạn)
```

Cách worker gọi:
```bash
curl -s -OJ http://203.x.x.x:8080/download/a3f9b2c1d4e5f678 \
  -H "Authorization: Bearer $TOKEN"
# -O: save to file, -J: dùng Content-Disposition làm filename
```

### GET /files

```
Response 200:
{
  "files": [
    {
      "file_id":    "a3f9b2c1d4e5f678",
      "filename":   "backup_20260304.sql.gz",
      "size_bytes": 1073741824,
      "created_at": 1741056000.0,
      "age_seconds": 42.3,
      "uploader":   "centos-dbserver"
    }
  ],
  "total": 1
}
```

### DELETE /files/{file_id}

```
Response 200: {"file_id": "...", "deleted": true}
Response 404: {"error": "FILE_NOT_FOUND", "file_id": "..."}
```

---

## 5. Config (node.yaml)

```yaml
# Gateway node — node-0/node.yaml
node_id: node-0
listen: 0.0.0.0:8080
trusted_nodes:
  - centos-dbserver
  - almalinux-restore

# v5.8 — upload settings (all optional, defaults shown)
upload_dir: /tmp/mesh-uploads         # where files are stored on gateway disk
upload_max_size_mb: 512               # max single file size
upload_ttl_seconds: 3600              # file auto-expires after 1 hour
```

---

## 6. Full Workflow — DB Backup/Restore (use case thực chiến)

```
Step 1: Claude verifies mesh health
  GET /health → queue_depths, jobs_active

Step 2: Backup trên CentOS  [pull mode]
  POST /action → centos-dbserver → execute_command
    "mysqldump -u root -p$PASS mydb | gzip > /tmp/backup_$(date +%Y%m%d).sql.gz"
  → poll /result/{job_id} → completed

Step 3: Upload backup → gateway  [pull mode]
  POST /action → centos-dbserver → execute_command
    "curl -s -X POST $GW/upload -H 'Authorization: Bearer $TOKEN'
     -H 'X-Node-ID: centos-dbserver'
     -F 'file=@/tmp/backup_20260304.sql.gz'"
  → poll → completed
  → parse stdout → {"file_id": "a3f9...", "download_url": "/download/a3f9..."}

Step 4: Download backup → AlmaLinux  [pull mode]
  POST /action → almalinux-restore → execute_command
    "curl -s -OJ $GW/download/a3f9b2c1d4e5f678
     -H 'Authorization: Bearer $TOKEN'"
  → poll → completed

Step 5: Restore trên AlmaLinux  [pull mode]
  POST /action → almalinux-restore → execute_command
    "gunzip -c /tmp/backup_20260304.sql.gz | mysql -u root -p$PASS targetdb"
  → poll → completed

Step 6: Cleanup
  DELETE /files/a3f9b2c1d4e5f678
  → {"deleted": true}
```

Toàn bộ workflow do Claude orchestrate. User chỉ cần phát một lệnh.

---

## 7. Trạng thái hiện tại (v5.8)

**Đã có:**
- ✅ Bootstrap minimal (3 seed actions)
- ✅ Plugin-based node runtime
- ✅ Async job model (job_id-based)
- ✅ Distributed routing (mesh + loop protection)
- ✅ Skill discovery (Markdown)
- ✅ Authentication layer (Bearer token)
- ✅ Action schema validation
- ✅ Job TTL & cleanup
- ✅ Auto rollback (bootstrap)
- ✅ One-liner setup.sh
- ✅ Push/Pull delivery (NAT support)
- ✅ NodeRegistry + WorkerAgent + JobQueue
- ✅ Optional task_id + trace (v5.6)
- ✅ Claude Web curl-native workflow (v5.6)
- ✅ Lazy node staleness check (v5.7)
- ✅ Pull job timeout — lazy check (v5.7)
- ✅ FastAPI lifespan migration (v5.7)
- ✅ Queue depth in /health (v5.7)
- ✅ **File upload/download — POST /upload, GET /download/{id} (v5.8)**
- ✅ **File listing — GET /files (v5.8)**
- ✅ **Explicit cleanup — DELETE /files/{id} (v5.8)**
- ✅ **UploadManager: lazy TTL, configurable dir/size/ttl (v5.8)**

**Chưa có:**
- ❌ Container isolation per-node (non-root user + ulimit minimum)
- ❌ Job queue persistence (SQLite — nếu cần)
- ❌ Idempotency key (Claude tự handle ở tầng logic)
- ❌ Upload resumption / chunked transfer (cho file > available RAM)
- ❌ File ACL (ai có thể download file của ai)

---

*Spec: SPECS_V5.8.md | Mesh Runtime v5.8 | Repository: ai-infra-runtime-v2*
