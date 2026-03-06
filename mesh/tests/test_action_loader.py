"""Tests for runtime.action_loader."""

from __future__ import annotations

import tempfile
from pathlib import Path

from runtime.action_loader import load_actions


def test_load_valid_actions() -> None:
    """Actions with a run() function are loaded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        action_file = Path(tmpdir) / "greet.py"
        action_file.write_text(
            "def run(params, context):\n    return {'msg': 'hello'}\n"
        )
        registry = load_actions(tmpdir)
        assert "greet" in registry
        result = registry["greet"].run({}, {})
        assert result == {"msg": "hello"}


def test_skip_module_without_run() -> None:
    """Modules missing run() are skipped with a warning."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bad = Path(tmpdir) / "no_run.py"
        bad.write_text("x = 42\n")
        registry = load_actions(tmpdir)
        assert "no_run" not in registry


def test_skip_private_files() -> None:
    """Files starting with _ are ignored."""
    with tempfile.TemporaryDirectory() as tmpdir:
        priv = Path(tmpdir) / "_internal.py"
        priv.write_text("def run(p, c): return {}\n")
        registry = load_actions(tmpdir)
        assert "_internal" not in registry


def test_nonexistent_directory() -> None:
    """Nonexistent directory returns empty registry."""
    registry = load_actions("/nonexistent/path")
    assert registry == {}


def test_async_flag_detection() -> None:
    """Modules with ASYNC = True are detected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        action = Path(tmpdir) / "slow.py"
        action.write_text(
            "ASYNC = True\ndef run(params, context):\n    return {}\n"
        )
        registry = load_actions(tmpdir)
        assert "slow" in registry
        assert getattr(registry["slow"], "ASYNC", False) is True
