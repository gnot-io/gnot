"""Tests for individual seed actions."""

from __future__ import annotations

import os
import tempfile

import pytest

from seed.actions import execute_command, read_file, write_file


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

class TestWriteFile:
    def test_write_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "output.txt")
            result = write_file.run({"path": path, "content": "hello"}, {})
            assert result["success"] is True
            assert os.path.exists(path)
            with open(path) as f:
                assert f.read() == "hello"

    def test_create_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "a", "b", "c", "deep.txt")
            result = write_file.run({"path": path, "content": "deep"}, {})
            assert result["success"] is True
            assert os.path.exists(path)

    def test_overwrite_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "exist.txt")
            write_file.run({"path": path, "content": "v1"}, {})
            write_file.run({"path": path, "content": "v2"}, {})
            with open(path) as f:
                assert f.read() == "v2"

    def test_missing_path(self) -> None:
        with pytest.raises(ValueError, match="path"):
            write_file.run({"content": "hello"}, {})

    def test_missing_content(self) -> None:
        with pytest.raises(ValueError, match="content"):
            write_file.run({"path": "/tmp/x"}, {})


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

class TestReadFile:
    def test_read_existing(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test content")
            f.flush()
            result = read_file.run({"path": f.name}, {})
            assert result["content"] == "test content"
            assert result["size_bytes"] > 0
            os.unlink(f.name)

    def test_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            read_file.run({"path": "/nonexistent/file.txt"}, {})

    def test_missing_path(self) -> None:
        with pytest.raises(ValueError, match="path"):
            read_file.run({}, {})


# ---------------------------------------------------------------------------
# execute_command
# ---------------------------------------------------------------------------

class TestExecuteCommand:
    @pytest.mark.asyncio
    async def test_simple_command(self) -> None:
        result = await execute_command.run({"command": "echo hello"}, {})
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]

    @pytest.mark.asyncio
    async def test_command_failure(self) -> None:
        result = await execute_command.run({"command": "exit 1"}, {})
        assert result["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        result = await execute_command.run(
            {"command": "sleep 10", "timeout_seconds": 1}, {}
        )
        assert result["exit_code"] == -1
        assert "Timeout" in result["stderr"]

    @pytest.mark.asyncio
    async def test_missing_command(self) -> None:
        with pytest.raises(ValueError, match="command"):
            await execute_command.run({}, {})

    def test_async_flag(self) -> None:
        assert execute_command.ASYNC is True
