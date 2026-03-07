"""Tests for runtime.bootstrap — Node bootstrap workflow and auto-rollback."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from runtime.bootstrap import (
    ActionFile,
    BootstrapEngine,
    BootstrapRequest,
    BootstrapResult,
    BootstrapStatus,
    _RollbackTracker,
)


# ---------------------------------------------------------------------------
# RollbackTracker unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollback_tracker_reverses_order() -> None:
    """Undo operations execute in reverse order."""
    order: list[str] = []

    tracker = _RollbackTracker()

    async def undo_a() -> None:
        order.append("a")

    async def undo_b() -> None:
        order.append("b")

    tracker.register("step_a", undo_a())
    tracker.register("step_b", undo_b())

    rolled = await tracker.rollback()
    assert order == ["b", "a"]
    assert rolled == ["step_b", "step_a"]


@pytest.mark.asyncio
async def test_rollback_tracker_handles_errors() -> None:
    """Rollback continues even if one undo fails."""
    tracker = _RollbackTracker()

    async def undo_ok() -> None:
        pass

    async def undo_fail() -> None:
        raise RuntimeError("boom")

    tracker.register("ok_step", undo_ok())
    tracker.register("fail_step", undo_fail())

    rolled = await tracker.rollback()
    # Both should be attempted
    assert len(rolled) == 2


# ---------------------------------------------------------------------------
# BootstrapEngine — directory + config creation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bootstrap_creates_directory_and_config() -> None:
    """Bootstrap creates node directory, config, and skills."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = BootstrapEngine()
        request = BootstrapRequest(
            node_id="node-test",
            listen="0.0.0.0:9999",
            base_dir=tmpdir,
            skills_md="# Test Node\n\nNo skills.",
            actions=[
                ActionFile(
                    filename="hello.py",
                    content="def run(params, context):\n    return {'msg': 'hello'}\n",
                ),
            ],
        )

        # Only test directory/config creation (not starting process)
        # We'll test the individual steps
        node_dir = Path(tmpdir) / "node-test"
        result = BootstrapResult(node_id="node-test", status=BootstrapStatus.IN_PROGRESS)
        tracker = _RollbackTracker()

        await engine._step_create_dirs(node_dir, tracker, result)
        assert node_dir.exists()
        assert (node_dir / "actions").exists()

        await engine._step_write_config(node_dir, request, tracker, result)
        config_path = node_dir / "node.yaml"
        assert config_path.exists()
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert config["node_id"] == "node-test"
        assert config["listen"] == "0.0.0.0:9999"

        await engine._step_write_skills(node_dir, request, tracker, result)
        assert (node_dir / "skills.md").exists()

        await engine._step_write_actions(node_dir, request, tracker, result)
        assert (node_dir / "actions" / "hello.py").exists()


@pytest.mark.asyncio
async def test_bootstrap_writes_action_schemas() -> None:
    """Bootstrap writes schema files alongside actions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = BootstrapEngine()
        request = BootstrapRequest(
            node_id="node-schema",
            listen="0.0.0.0:9998",
            base_dir=tmpdir,
            actions=[
                ActionFile(
                    filename="greet.py",
                    content="def run(p, c):\n    return {}\n",
                    schema_content='{"type": "object"}',
                ),
            ],
        )

        node_dir = Path(tmpdir) / "node-schema"
        result = BootstrapResult(node_id="node-schema", status=BootstrapStatus.IN_PROGRESS)
        tracker = _RollbackTracker()

        await engine._step_create_dirs(node_dir, tracker, result)
        await engine._step_write_actions(node_dir, request, tracker, result)

        assert (node_dir / "actions" / "greet.py").exists()
        assert (node_dir / "actions" / "greet.schema.json").exists()


@pytest.mark.asyncio
async def test_bootstrap_rollback_removes_directory() -> None:
    """On failure, rollback removes the created node directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = BootstrapEngine()
        node_dir = Path(tmpdir) / "node-rollback"
        result = BootstrapResult(node_id="node-rollback", status=BootstrapStatus.IN_PROGRESS)
        tracker = _RollbackTracker()

        await engine._step_create_dirs(node_dir, tracker, result)
        assert node_dir.exists()

        # Simulate rollback
        rolled = await tracker.rollback()
        assert "create_dirs" in rolled
        assert not node_dir.exists()


@pytest.mark.asyncio
async def test_bootstrap_register_and_rollback_config() -> None:
    """Register writes to seed config; rollback reverts it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        seed_config = Path(tmpdir) / "seed.yaml"
        seed_config.write_text(yaml.dump({
            "node_id": "node-0",
            "listen": "0.0.0.0:8080",
            "nodes": {"node-0": "http://127.0.0.1:8080"},
        }))

        engine = BootstrapEngine(seed_config_path=str(seed_config))
        result = BootstrapResult(node_id="node-new", status=BootstrapStatus.IN_PROGRESS)
        tracker = _RollbackTracker()

        await engine._step_register_node(
            "node-new", "http://127.0.0.1:9090", tracker, result
        )

        # Check node was registered
        with open(seed_config) as f:
            config = yaml.safe_load(f)
        assert "node-new" in config["nodes"]

        # Rollback should revert
        await tracker.rollback()
        with open(seed_config) as f:
            config = yaml.safe_load(f)
        assert "node-new" not in config["nodes"]
        assert "node-0" in config["nodes"]  # Original preserved


@pytest.mark.asyncio
async def test_bootstrap_extra_nodes_in_config() -> None:
    """extra_nodes are merged into the new node's config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = BootstrapEngine()
        request = BootstrapRequest(
            node_id="node-2",
            listen="0.0.0.0:8082",
            base_dir=tmpdir,
            extra_nodes={"node-0": "http://10.0.0.1:8080"},
        )

        node_dir = Path(tmpdir) / "node-2"
        result = BootstrapResult(node_id="node-2", status=BootstrapStatus.IN_PROGRESS)
        tracker = _RollbackTracker()

        await engine._step_create_dirs(node_dir, tracker, result)
        await engine._step_write_config(node_dir, request, tracker, result)

        with open(node_dir / "node.yaml") as f:
            config = yaml.safe_load(f)
        assert "node-0" in config["nodes"]
        assert "node-2" in config["nodes"]


@pytest.mark.asyncio
async def test_bootstrap_auth_token_in_config() -> None:
    """auth_token is written to the new node's config when provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = BootstrapEngine()
        request = BootstrapRequest(
            node_id="node-secure",
            listen="0.0.0.0:8083",
            base_dir=tmpdir,
            auth_token="my-secret",
        )

        node_dir = Path(tmpdir) / "node-secure"
        result = BootstrapResult(node_id="node-secure", status=BootstrapStatus.IN_PROGRESS)
        tracker = _RollbackTracker()

        await engine._step_create_dirs(node_dir, tracker, result)
        await engine._step_write_config(node_dir, request, tracker, result)

        with open(node_dir / "node.yaml") as f:
            config = yaml.safe_load(f)
        assert config["auth_token"] == "my-secret"
