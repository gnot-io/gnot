"""Upload manager — gateway-side file store for inter-node file transfer.

v5.8 — Added to enable binary file transfer between mesh nodes without
routing file content through JSON action payloads.

Design decisions:
  - Files are stored flat under `upload_dir` as `{file_id}_{safe_filename}`.
  - Metadata is kept in-memory (dict). On restart, metadata is lost but files
    remain on disk — orphaned files are cleaned up on next sweep_expired().
  - TTL is enforced lazily: expired files are detected and deleted on
    `get()`, `list_files()`, and `sweep_expired()`. No background task needed.
  - Max file size is enforced at the store() call site; the endpoint checks
    Content-Length first, then re-checks after reading to be safe.
  - File IDs are uuid4 hex — not content-addressed (same file uploaded twice
    gets two independent file_ids with independent TTLs).

Thread-safety: asyncio.Lock protects _meta mutations.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class FileTooLargeError(ValueError):
    """Raised when uploaded bytes exceed the configured max size."""


# ---------------------------------------------------------------------------
# Internal metadata record
# ---------------------------------------------------------------------------

@dataclass
class _UploadEntry:
    file_id: str
    filename: str           # sanitized original filename
    size_bytes: int
    created_at: float
    uploader: str | None    # node_id of uploader, or None


# ---------------------------------------------------------------------------
# Upload Manager
# ---------------------------------------------------------------------------

class UploadManager:
    """Stores and serves uploaded files with TTL-based expiry.

    Public interface:
        store(filename, data, uploader) -> _UploadEntry
        get(file_id)                    -> (Path, _UploadEntry) | None
        delete(file_id)                 -> bool
        list_files()                    -> list[_UploadEntry]
        sweep_expired()                 -> int   (files removed)
    """

    def __init__(
        self,
        upload_dir: str,
        max_size_bytes: int,
        ttl_seconds: int,
    ) -> None:
        self._dir = Path(upload_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_size = max_size_bytes
        self._ttl = ttl_seconds
        self._meta: dict[str, _UploadEntry] = {}
        self._lock = asyncio.Lock()
        logger.info(
            "UploadManager initialised — dir=%s max_size=%dMB ttl=%ds",
            self._dir, max_size_bytes // (1024 * 1024), ttl_seconds,
        )

    # -- write ---------------------------------------------------------------

    async def store(
        self,
        filename: str,
        data: bytes,
        uploader: str | None = None,
    ) -> _UploadEntry:
        """Persist bytes to disk and record metadata.

        Raises:
            FileTooLargeError: if len(data) > max_size_bytes.
        """
        if len(data) > self._max_size:
            raise FileTooLargeError(
                f"File size {len(data):,} bytes exceeds limit "
                f"{self._max_size:,} bytes ({self._max_size // (1024*1024)} MB)"
            )

        file_id = uuid.uuid4().hex[:16]
        safe_name = _sanitize_filename(filename or "upload")
        path = self._dir / f"{file_id}_{safe_name}"
        path.write_bytes(data)

        entry = _UploadEntry(
            file_id=file_id,
            filename=safe_name,
            size_bytes=len(data),
            created_at=time.time(),
            uploader=uploader,
        )
        async with self._lock:
            self._meta[file_id] = entry

        logger.info(
            "Stored upload %s: name=%s size=%d uploader=%s",
            file_id, safe_name, len(data), uploader,
        )
        return entry

    # -- read ----------------------------------------------------------------

    async def get(self, file_id: str) -> tuple[Path, _UploadEntry] | None:
        """Return (path, entry) for a file_id, or None if not found / expired.

        Lazily deletes the file if TTL has been exceeded.
        """
        async with self._lock:
            entry = self._meta.get(file_id)

        if entry is None:
            return None

        # Lazy TTL check
        if self._is_expired(entry):
            logger.info("File %s expired (TTL=%ds) — deleting lazily", file_id, self._ttl)
            await self.delete(file_id)
            return None

        path = self._dir / f"{file_id}_{entry.filename}"
        if not path.exists():
            logger.warning("File %s missing from disk — removing metadata", file_id)
            async with self._lock:
                self._meta.pop(file_id, None)
            return None

        return path, entry

    # -- delete --------------------------------------------------------------

    async def delete(self, file_id: str) -> bool:
        """Remove file and metadata. Returns True if file existed."""
        async with self._lock:
            entry = self._meta.pop(file_id, None)

        if entry is None:
            return False

        path = self._dir / f"{file_id}_{entry.filename}"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete file %s: %s", path, exc)

        logger.info("Deleted upload %s (%s)", file_id, entry.filename)
        return True

    # -- list ----------------------------------------------------------------

    async def list_files(self) -> list[_UploadEntry]:
        """Return all non-expired uploads, deleting expired ones lazily."""
        async with self._lock:
            snapshot = list(self._meta.values())

        result: list[_UploadEntry] = []
        for entry in snapshot:
            if self._is_expired(entry):
                await self.delete(entry.file_id)
            else:
                result.append(entry)

        return result

    # -- maintenance ---------------------------------------------------------

    async def sweep_expired(self) -> int:
        """Explicitly remove all expired files. Returns count of files removed.

        Also reconciles disk vs metadata: orphaned disk files are deleted.
        """
        removed = 0

        # Remove expired metadata entries
        async with self._lock:
            snapshot = list(self._meta.values())

        for entry in snapshot:
            if self._is_expired(entry):
                await self.delete(entry.file_id)
                removed += 1

        # Remove orphaned disk files not in metadata
        async with self._lock:
            known_ids = set(self._meta.keys())

        for disk_file in self._dir.glob("*_*"):
            fid = disk_file.name.split("_", 1)[0]
            if fid not in known_ids:
                try:
                    disk_file.unlink()
                    logger.info("Swept orphaned file: %s", disk_file.name)
                    removed += 1
                except OSError:
                    pass

        if removed:
            logger.info("sweep_expired: removed %d file(s)", removed)
        return removed

    # -- helpers -------------------------------------------------------------

    def _is_expired(self, entry: _UploadEntry) -> bool:
        return (time.time() - entry.created_at) > self._ttl

    @property
    def upload_dir(self) -> Path:
        return self._dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """Remove path separators and non-safe chars from a filename.

    Preserves extension, replaces unsafe chars with underscores.
    Result is always a flat filename (no directory components).
    """
    # Take only the final component (strip path traversal)
    name = Path(name).name
    # Replace anything not alphanumeric, dot, dash, underscore
    name = re.sub(r"[^\w.\-]", "_", name)
    # Collapse consecutive underscores
    name = re.sub(r"_+", "_", name)
    # Limit length
    if len(name) > 128:
        stem = Path(name).stem[:120]
        suffix = Path(name).suffix
        name = f"{stem}{suffix}"
    return name or "upload"
