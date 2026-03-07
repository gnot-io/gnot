# Worklog — Execution Mesh v5.8 (File Transfer)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-04
**Base version:** v5.7 → v5.8

---

## 1. Vấn đề / Context

Use case thực chiến được đưa ra để test hệ thống: backup database trên CentOS (Company X LAN, sau NAT) → transfer về gateway (Debian, public IP) → restore trên AlmaLinux (Company Y LAN, sau NAT).

Phân tích v5.7 cho thấy hệ thống cover được 5/6 bước. Gap duy nhất:

**File transfer binary qua HTTP.**

`read_file` + `write_file` hiện tại truyền nội dung qua JSON string. Pattern này không dùng được với file lớn vì:
1. JSON string encoding tăng kích thước ~33% (base64) hoặc corrupt với binary data
2. Toàn bộ file phải load vào RAM của server trước khi respond
3. Không có streaming — timeout với file GB
4. Context window của Claude không thể chứa 1GB base64 string

Hệ quả: không thể orchestrate bất kỳ workflow nào liên quan đến file lớn (backup, artifact, model checkpoint, log archive...).

---

## 2. Phân tích Alternative

### Vấn đề: Cơ chế transfer file binary giữa nodes sau NAT

**Option A — SCP/SFTP qua SSH:**

Worker dùng `execute_command` gọi `scp` để push file trực tiếp từ node này sang node kia.

```
centos → ssh → almalinux  (nếu reachable)
centos → ssh → gateway    (push lên gateway)
almalinux → ssh → gateway (pull từ gateway)
```

Ưu điểm: không cần code mới, dùng tooling chuẩn.

Nhược điểm:
- Cần setup SSH key pair trước (tốn effort, không zero-config)
- Port 22 thường bị firewall chặn ở corporate LAN
- Không phù hợp với mô hình "mọi thứ qua HTTP gateway"
- Hai node sau NAT khác nhau không thể SSH trực tiếp — vẫn phải relay qua gateway

**Option B — Gateway làm HTTP relay, worker dùng `curl` upload/download:**

Thêm `POST /upload` (multipart) và `GET /download/{file_id}` vào gateway. Worker gọi từ `execute_command` bằng `curl` — công cụ có sẵn trên mọi Linux distro.

```
centos → curl POST /upload → gateway lưu file → curl GET /download → almalinux
```

Ưu điểm:
- Zero pre-config (không cần SSH key)
- Tất cả traffic qua port 8080 đã mở sẵn (HTTP)
- `curl` có trên mọi môi trường: CentOS, AlmaLinux, Debian
- Consistent với kiến trúc mesh (mọi thứ qua HTTP)
- FastAPI `FileResponse` stream file từ disk → không load toàn bộ vào RAM
- Claude có thể tự orchestrate hoàn toàn: upload → lấy file_id → pass cho download command

Nhược điểm:
- File tạm thời lưu trên gateway disk (cần đủ space)
- Cần thêm dependency: `python-multipart`
- Không phải end-to-end transfer (file phải qua gateway)

**Quyết định: Option B.**

Lý do: zero-config > SSH setup. Tất cả worker đã có `curl`. Phù hợp với architectural principle. Disk usage có thể kiểm soát qua TTL và explicit delete.

**Option C — Chunked upload (cho file > RAM):**

Thêm chunked upload protocol để xử lý file lớn hơn RAM server.

**Quyết định: Defer.** Use case hiện tại (DB backup vài trăm MB) không cần. FastAPI đọc multipart từ disk buffer — không hoàn toàn in-memory với file lớn. Thêm vào backlog khi thực sự cần.

---

## 3. Chi tiết Implementation

### 3.1 UploadManager (`runtime/upload_manager.py`) — New

Core component. Hoàn toàn độc lập với gateway router, job queue, node registry.

**Storage layout:**
```
/tmp/mesh-uploads/
  {file_id}_{safe_filename}   ← flat structure, không subdirectory
```

Flat layout đơn giản hơn hierarchical (`{year}/{month}/...`) vì không cần quản lý directory cleanup, và số lượng file đồng thời trên gateway thường thấp (mỗi workflow xài 1-2 file).

**Lazy TTL — consistent với v5.7 pattern:**

v5.7 đã áp dụng lazy evaluation cho node staleness và pull job timeout. v5.8 tiếp tục pattern này cho file TTL: không có background cleanup loop, check tại `get()` và `list_files()`. Gọi `sweep_expired()` một lần khi startup để dọn orphan từ restart trước.

**Filename sanitization:**

Path traversal là attack vector thực. `../../etc/passwd` phải trở thành `passwd`.
Dùng `Path(name).name` để lấy chỉ phần cuối, sau đó replace ký tự không safe.

**File ID:**

`uuid4().hex[:16]` = 16 chars hex. Đủ entropy cho use case (2^64 possibilities).
Không dùng content hash vì: hai upload cùng file cần TTL độc lập.

### 3.2 Config (`runtime/config.py`) — Modified

Ba fields mới:
```python
upload_dir: str = "/tmp/mesh-uploads"
upload_max_size_mb: int = 512
upload_ttl_seconds: int = 3600
```

Thêm derived property `upload_max_size_bytes` để tránh nhân `* 1024 * 1024` mọi chỗ.

### 3.3 Models (`runtime/models.py`) — Modified

Ba models mới: `UploadResponse`, `FileInfo`, `FileListResponse`.

### 3.4 Server (`runtime/server.py`) — Modified

Bốn endpoints mới: `POST /upload`, `GET /download/{file_id}`, `GET /files`, `DELETE /files/{file_id}`.

Lý do dùng `FileResponse` thay vì `StreamingResponse`:
- `FileResponse` của FastAPI tự động set `Content-Disposition`, `Content-Length`, `ETag`
- Streaming từ disk (không load toàn bộ vào RAM) — quan trọng với file lớn
- httpx test client handle `FileResponse` đúng trong ASGI mode

`UploadFile` (FastAPI) cần `python-multipart`. Đây là dep bắt buộc — ghi vào requirements.

Lifespan update: gọi `await upload_manager.sweep_expired()` khi startup để dọn orphaned files từ restart trước.

---

## 4. Files thay đổi

| File | Loại | Thay đổi chính |
|------|------|---------------|
| `runtime/upload_manager.py` | **New** | UploadManager: store/get/delete/list/sweep, lazy TTL, filename sanitization |
| `runtime/models.py` | Modified | Thêm `UploadResponse`, `FileInfo`, `FileListResponse` |
| `runtime/config.py` | Modified | Thêm `upload_dir`, `upload_max_size_mb`, `upload_ttl_seconds`, `upload_max_size_bytes` |
| `runtime/server.py` | Modified | Version 5.8.0; `UploadFile`/`FileResponse` imports; UploadManager init; 4 endpoints mới; lifespan sweep |
| `tests/test_v58_features.py` | **New** | 28 tests |
| `docs/SPECS_V5.8.md` | **New** | Spec v5.8 |
| `docs/WORKLOG_V5.8.md` | **New** | Document này |

---

## 5. Test Suite

### Mới (v5.8)

| Class | Tests | Coverage |
|-------|-------|---------|
| `TestSanitizeFilename` | 5 | Normal, path traversal, spaces, long name, empty |
| `TestUploadManagerStore` | 4 | Creates file on disk, correct metadata, oversized raises, unique IDs |
| `TestUploadManagerGet` | 3 | Found, not found, expired → delete lazily |
| `TestUploadManagerDelete` | 2 | Removes file, nonexistent returns False |
| `TestUploadManagerList` | 3 | Empty, populated, expired filtered |
| `TestUploadManagerSweep` | 2 | Removes expired entries, removes orphan disk files |
| `TestUploadEndpoint` | 3 | 201 success, 413 too large, 400 empty |
| `TestDownloadEndpoint` | 2 | 200 bytes match, 404 unknown |
| `TestFileListEndpoint` | 2 | Empty list, populated list |
| `TestDeleteFileEndpoint` | 2 | Delete success + verify 404, delete nonexistent 404 |
| **Tổng mới** | **28** | |

### Tổng kết

| Version | Tests | Pass |
|---------|-------|------|
| v5.7 | 43 | 43 |
| **v5.8** | **71** | **71** |

---

## 6. Dependency mới

```
python-multipart   (FastAPI multipart/form-data support)
```

Thêm vào `requirements.txt` của project:
```
python-multipart>=0.0.9
```

---

## 7. Remaining Items (cập nhật)

| Priority | Item | Status | Ghi chú |
|----------|------|--------|---------|
| P2 | Lazy node staleness check | ✅ Done (v5.7) | |
| P2 | Container isolation (non-root + ulimit) | ⏸ Deferred | Không cần cho use case hiện tại |
| P3 | Pull job timeout | ✅ Done (v5.7) | |
| P3 | FastAPI lifespan migration | ✅ Done (v5.7) | |
| P3 | Queue depth in /health | ✅ Done (v5.7) | |
| P3 | **File upload/download** | ✅ Done (v5.8) | |
| P3 | Job queue persistence (SQLite) | ⏸ Deferred | Claude retry pattern đủ dùng |
| P4 | Idempotency key | ⏸ Deferred | Claude tự handle tầng logic |
| P3 | **Upload chunked/resumable** | 🆕 New | Cho file > available RAM; defer đến có use case cụ thể |
| P3 | **File ACL** | 🆕 New | Kiểm soát ai download được file của ai; defer đến multi-tenant |

---

*Document: WORKLOG_V5.8.md | Mesh Runtime v5.8 | Repository: ai-infra-runtime-v2*
