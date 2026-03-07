"""Tests for v5.8 — File Upload / Download.

Covers:
  1. UploadManager.store — happy path, file-too-large, sanitize filename
  2. UploadManager.get   — found, not found, expired (lazy TTL)
  3. UploadManager.delete — found, not found
  4. UploadManager.list_files — empty, populated, expired entries filtered
  5. UploadManager.sweep_expired — removes expired + orphan disk files
  6. _sanitize_filename helper — path traversal, special chars, length
  7. POST /upload   — success 201, file too large 413, empty file 400
  8. GET  /download/{file_id} — success 200, not found 404, expired 404
  9. GET  /files    — empty list, populated list
  10. DELETE /files/{file_id} — success, not found 404
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.upload_manager import FileTooLargeError, UploadManager, _sanitize_filename


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(tmpdir: str, max_mb: int = 10, ttl: int = 3600) -> UploadManager:
    return UploadManager(
        upload_dir=tmpdir,
        max_size_bytes=max_mb * 1024 * 1024,
        ttl_seconds=ttl,
    )


# ---------------------------------------------------------------------------
# 1–6: Unit tests — UploadManager & _sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:

    def test_normal_filename_unchanged(self):
        assert _sanitize_filename("backup.sql.gz") == "backup.sql.gz"

    def test_path_traversal_stripped(self):
        result = _sanitize_filename("../../etc/passwd")
        assert "/" not in result
        assert ".." not in result
        assert result == "passwd"

    def test_spaces_replaced(self):
        result = _sanitize_filename("my backup file.sql")
        assert " " not in result

    def test_long_name_truncated(self):
        name = "a" * 200 + ".sql"
        result = _sanitize_filename(name)
        assert len(result) <= 128

    def test_empty_name_returns_upload(self):
        assert _sanitize_filename("") == "upload"


class TestUploadManagerStore:

    @pytest.mark.asyncio
    async def test_store_creates_file_on_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            entry = await mgr.store("test.txt", b"hello world", uploader="node-1")
            stored_path = mgr.upload_dir / f"{entry.file_id}_{entry.filename}"
            assert stored_path.exists()
            assert stored_path.read_bytes() == b"hello world"

    @pytest.mark.asyncio
    async def test_store_returns_correct_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            data = b"x" * 100
            entry = await mgr.store("data.bin", data, uploader="node-1")
            assert entry.size_bytes == 100
            assert entry.filename == "data.bin"
            assert entry.uploader == "node-1"
            assert len(entry.file_id) == 16

    @pytest.mark.asyncio
    async def test_store_raises_for_oversized_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir, max_mb=1)
            with pytest.raises(FileTooLargeError):
                await mgr.store("big.bin", b"x" * (2 * 1024 * 1024))

    @pytest.mark.asyncio
    async def test_two_uploads_get_unique_file_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            e1 = await mgr.store("a.txt", b"aaa")
            e2 = await mgr.store("a.txt", b"aaa")  # same content, same name
            assert e1.file_id != e2.file_id


class TestUploadManagerGet:

    @pytest.mark.asyncio
    async def test_get_returns_path_and_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            entry = await mgr.store("f.txt", b"content")
            result = await mgr.get(entry.file_id)
            assert result is not None
            path, info = result
            assert path.exists()
            assert info.file_id == entry.file_id

    @pytest.mark.asyncio
    async def test_get_returns_none_for_unknown_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            assert await mgr.get("nonexistent0000") is None

    @pytest.mark.asyncio
    async def test_get_returns_none_and_deletes_expired_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir, ttl=1)  # 1 second TTL
            entry = await mgr.store("f.txt", b"data")
            # Manually age the entry
            mgr._meta[entry.file_id].created_at = time.time() - 5

            result = await mgr.get(entry.file_id)
            assert result is None
            # File should be deleted from disk
            stored = mgr.upload_dir / f"{entry.file_id}_{entry.filename}"
            assert not stored.exists()


class TestUploadManagerDelete:

    @pytest.mark.asyncio
    async def test_delete_removes_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            entry = await mgr.store("del.txt", b"bye")
            assert await mgr.delete(entry.file_id) is True
            assert await mgr.get(entry.file_id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            assert await mgr.delete("nope000000000000") is False


class TestUploadManagerList:

    @pytest.mark.asyncio
    async def test_list_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            assert await mgr.list_files() == []

    @pytest.mark.asyncio
    async def test_list_returns_all_active(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            await mgr.store("a.txt", b"a")
            await mgr.store("b.txt", b"b")
            files = await mgr.list_files()
            assert len(files) == 2

    @pytest.mark.asyncio
    async def test_list_filters_expired(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir, ttl=1)
            e1 = await mgr.store("fresh.txt", b"x")
            e2 = await mgr.store("stale.txt", b"y")
            # Age one entry
            mgr._meta[e2.file_id].created_at = time.time() - 10

            files = await mgr.list_files()
            ids = [f.file_id for f in files]
            assert e1.file_id in ids
            assert e2.file_id not in ids


class TestUploadManagerSweep:

    @pytest.mark.asyncio
    async def test_sweep_removes_expired_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir, ttl=1)
            entry = await mgr.store("old.txt", b"data")
            mgr._meta[entry.file_id].created_at = time.time() - 10
            removed = await mgr.sweep_expired()
            assert removed >= 1

    @pytest.mark.asyncio
    async def test_sweep_removes_orphaned_disk_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _make_manager(tmpdir)
            # Create orphan file directly (not tracked in metadata)
            orphan = mgr.upload_dir / "orphanid_orphan.txt"
            orphan.write_bytes(b"orphan")
            removed = await mgr.sweep_expired()
            assert removed >= 1
            assert not orphan.exists()


# ---------------------------------------------------------------------------
# 7–10: ASGI integration tests
# ---------------------------------------------------------------------------

try:
    from httpx import AsyncClient, ASGITransport
    from runtime.action_loader import load_actions
    from runtime.config import load_config
    from runtime.server import create_app

    INTEGRATION_AVAILABLE = True
except ImportError:
    INTEGRATION_AVAILABLE = False


def _make_app(tmpdir: str):
    yaml_path = os.path.join(tmpdir, "node.yaml")
    with open(yaml_path, "w") as f:
        f.write(
            f"node_id: node-test\n"
            f"listen: 0.0.0.0:8080\n"
            f"upload_dir: {tmpdir}/uploads\n"
            f"upload_max_size_mb: 1\n"
            f"upload_ttl_seconds: 3600\n"
        )
    config = load_config(yaml_path)
    seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
    registry = load_actions(seed_dir)
    return create_app(config, registry)


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestUploadEndpoint:

    async def test_upload_returns_201_with_file_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/upload",
                    files={"file": ("backup.sql.gz", b"fake backup content", "application/gzip")},
                )
            assert resp.status_code == 201
            data = resp.json()
            assert "file_id" in data
            assert data["filename"] == "backup.sql.gz"
            assert data["download_url"].startswith("/download/")
            assert data["size_bytes"] == len(b"fake backup content")

    async def test_upload_too_large_returns_413(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(tmpdir)  # max 1 MB
            big_data = b"x" * (2 * 1024 * 1024)  # 2 MB
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/upload",
                    files={"file": ("big.bin", big_data, "application/octet-stream")},
                )
            assert resp.status_code == 413
            assert "FILE_TOO_LARGE" in resp.json().get("error", "")

    async def test_upload_empty_file_returns_400(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/upload",
                    files={"file": ("empty.txt", b"", "text/plain")},
                )
            assert resp.status_code == 400


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestDownloadEndpoint:

    async def test_download_returns_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(tmpdir)
            payload = b"database backup content here"
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                upload = await c.post(
                    "/upload",
                    files={"file": ("db.sql", payload, "application/octet-stream")},
                )
                file_id = upload.json()["file_id"]
                dl = await c.get(f"/download/{file_id}")
            assert dl.status_code == 200
            assert dl.content == payload

    async def test_download_unknown_id_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/download/doesnotexist00")
            assert resp.status_code == 404


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestFileListEndpoint:

    async def test_files_empty_initially(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/files")
            assert resp.status_code == 200
            assert resp.json()["total"] == 0

    async def test_files_lists_uploaded_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post(
                    "/upload",
                    files={"file": ("report.csv", b"col1,col2\n1,2", "text/csv")},
                )
                resp = await c.get("/files")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["files"][0]["filename"] == "report.csv"


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestDeleteFileEndpoint:

    async def test_delete_removes_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                upload = await c.post(
                    "/upload",
                    files={"file": ("todelete.txt", b"bye", "text/plain")},
                )
                file_id = upload.json()["file_id"]

                del_resp = await c.delete(f"/files/{file_id}")
                assert del_resp.status_code == 200
                assert del_resp.json()["deleted"] is True

                # Download should now 404
                dl = await c.get(f"/download/{file_id}")
                assert dl.status_code == 404

    async def test_delete_nonexistent_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.delete("/files/nope0000000000000")
            assert resp.status_code == 404
