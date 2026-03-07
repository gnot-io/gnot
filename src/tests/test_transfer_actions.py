"""Tests for read_file_b64 and write_file_b64 seed actions."""
import base64
import os
import tempfile
import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from seed.actions.read_file_b64 import run as read_b64
from seed.actions.write_file_b64 import run as write_b64

CTX = {"task_id": "test", "job_id": None, "node_id": "node-0",
       "config": {}, "logger": __import__("logging").getLogger("test"),
       "temp_dir": "/tmp"}


# ── read_file_b64 ────────────────────────────────────────────────────────────

def test_read_text_file_returns_base64():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
        f.write(b"Hello, Mesh!")
        path = f.name
    try:
        result = read_b64({"path": path}, CTX)
        assert result["size_bytes"] == 12
        decoded = base64.b64decode(result["content_b64"])
        assert decoded == b"Hello, Mesh!"
    finally:
        os.unlink(path)


def test_read_binary_file_returns_valid_base64():
    raw = bytes(range(256))  # all byte values — would break UTF-8 read
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(raw)
        path = f.name
    try:
        result = read_b64({"path": path}, CTX)
        assert result["size_bytes"] == 256
        assert base64.b64decode(result["content_b64"]) == raw
    finally:
        os.unlink(path)


def test_read_missing_path_raises_value_error():
    with pytest.raises(ValueError, match="Missing required param: path"):
        read_b64({}, CTX)


def test_read_nonexistent_file_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        read_b64({"path": "/nonexistent/file.sql"}, CTX)


def test_read_returns_resolved_path():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    try:
        result = read_b64({"path": path}, CTX)
        assert os.path.isabs(result["path"])
    finally:
        os.unlink(path)


# ── write_file_b64 ───────────────────────────────────────────────────────────

def test_write_text_file():
    content = b"Hello from write_file_b64!"
    b64 = base64.b64encode(content).decode("ascii")
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "out.txt")
        result = write_b64({"path": dest, "content_b64": b64}, CTX)
        assert result["success"] is True
        assert result["size_bytes"] == len(content)
        assert open(dest, "rb").read() == content


def test_write_binary_file_roundtrip():
    raw = bytes(range(256))
    b64 = base64.b64encode(raw).decode("ascii")
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "data.bin")
        result = write_b64({"path": dest, "content_b64": b64}, CTX)
        assert result["size_bytes"] == 256
        assert open(dest, "rb").read() == raw


def test_write_creates_parent_directories():
    b64 = base64.b64encode(b"data").decode("ascii")
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "a", "b", "c", "file.txt")
        result = write_b64({"path": dest, "content_b64": b64}, CTX)
        assert result["success"] is True
        assert os.path.exists(dest)


def test_write_missing_path_raises_value_error():
    with pytest.raises(ValueError, match="Missing required param: path"):
        write_b64({"content_b64": "dGVzdA=="}, CTX)


def test_write_missing_content_raises_value_error():
    with pytest.raises(ValueError, match="Missing required param: content_b64"):
        write_b64({"path": "/tmp/x.txt"}, CTX)


def test_write_invalid_base64_raises_value_error():
    with pytest.raises(ValueError, match="Invalid Base64"):
        write_b64({"path": "/tmp/x.txt", "content_b64": "NOT!VALID!BASE64!!!"}, CTX)


# ── roundtrip integration ────────────────────────────────────────────────────

def test_full_roundtrip_read_then_write():
    """Simulate: read_file_b64 on source → write_file_b64 on dest."""
    payload = b"\x00\x01\x02Binary\xFF\xFEContent"

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "source.bin")
        dst = os.path.join(d, "dest.bin")

        open(src, "wb").write(payload)

        # Step 1: read on source node
        read_result = read_b64({"path": src}, CTX)

        # Step 2: write on dest node (using output from step 1)
        write_result = write_b64(
            {"path": dst, "content_b64": read_result["content_b64"]},
            CTX,
        )

        assert write_result["success"] is True
        assert open(dst, "rb").read() == payload
        assert write_result["size_bytes"] == read_result["size_bytes"]
