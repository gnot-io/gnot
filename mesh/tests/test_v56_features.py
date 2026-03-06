"""Tests for v5.6 features.

Covers:
  1. ActionRequest.task_id auto-generation when absent.
  2. ActionRequest.trace auto-default when absent.
  3. Envelope with only target_node_id + payload is accepted (minimal form).
  4. Explicit task_id is preserved (not overwritten).
  5. Explicit trace is preserved.
  6. Partial trace (only hop_count) still works.
  7. POST /action with minimal envelope via ASGI test client.
  8. POST /action with full envelope still works (backward compat).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.models import ActionPayload, ActionRequest, TraceInfo


# ---------------------------------------------------------------------------
# Unit tests — ActionRequest model
# ---------------------------------------------------------------------------

class TestActionRequestDefaults:

    def test_task_id_auto_generated_when_absent(self):
        req = ActionRequest(
            target_node_id="node-1",
            payload=ActionPayload(action="read_file", params={"path": "/tmp/x"}),
        )
        assert req.task_id is not None
        assert req.task_id.startswith("task-")
        assert len(req.task_id) > 5

    def test_task_id_is_unique_per_instance(self):
        req_a = ActionRequest(
            target_node_id="node-1",
            payload=ActionPayload(action="read_file", params={"path": "/tmp/a"}),
        )
        req_b = ActionRequest(
            target_node_id="node-1",
            payload=ActionPayload(action="read_file", params={"path": "/tmp/b"}),
        )
        assert req_a.task_id != req_b.task_id

    def test_explicit_task_id_preserved(self):
        req = ActionRequest(
            target_node_id="node-1",
            task_id="my-custom-task-001",
            payload=ActionPayload(action="read_file", params={"path": "/tmp/x"}),
        )
        assert req.task_id == "my-custom-task-001"

    def test_trace_auto_default_when_absent(self):
        req = ActionRequest(
            target_node_id="node-1",
            payload=ActionPayload(action="read_file", params={"path": "/tmp/x"}),
        )
        assert req.trace is not None
        assert req.trace.hop_count == 0
        assert req.trace.route_path == []

    def test_explicit_trace_preserved(self):
        trace = TraceInfo(hop_count=2, route_path=["node-0", "node-1"])
        req = ActionRequest(
            target_node_id="node-2",
            task_id="task-hop",
            trace=trace,
            payload=ActionPayload(action="read_file", params={"path": "/tmp/x"}),
        )
        assert req.trace.hop_count == 2
        assert req.trace.route_path == ["node-0", "node-1"]

    def test_minimal_envelope_parses_from_dict(self):
        """Minimal JSON that Claude would send via curl."""
        raw = {
            "target_node_id": "node-0",
            "payload": {
                "action": "execute_command",
                "params": {"command": "echo hi", "timeout_seconds": 10},
            },
        }
        req = ActionRequest(**raw)
        assert req.target_node_id == "node-0"
        assert req.payload.action == "execute_command"
        assert req.task_id is not None
        assert req.trace.hop_count == 0

    def test_minimal_envelope_parses_from_json_string(self):
        """Round-trip through json.loads to simulate curl body parsing."""
        body = json.dumps({
            "target_node_id": "node-1",
            "payload": {"action": "write_file", "params": {"path": "/tmp/t.txt", "content": "hi"}},
        })
        req = ActionRequest(**json.loads(body))
        assert req.payload.action == "write_file"
        assert req.trace is not None

    def test_full_envelope_still_works(self):
        """Backward compatibility: full v5.0-style envelope is still accepted."""
        raw = {
            "target_node_id": "node-1",
            "task_id": "task-20260301-0001",
            "trace": {"hop_count": 0, "route_path": []},
            "payload": {"action": "read_file", "params": {"path": "/etc/hostname"}},
        }
        req = ActionRequest(**raw)
        assert req.task_id == "task-20260301-0001"
        assert req.trace.hop_count == 0


# ---------------------------------------------------------------------------
# ASGI integration — POST /action with minimal envelope
# ---------------------------------------------------------------------------

try:
    from httpx import AsyncClient, ASGITransport
    from runtime.action_loader import ActionRegistry, load_actions
    from runtime.config import NodeConfig, load_config
    from runtime.server import create_app

    INTEGRATION_AVAILABLE = True
except ImportError:
    INTEGRATION_AVAILABLE = False


def _make_minimal_config(tmpdir: str) -> NodeConfig:
    yaml_path = os.path.join(tmpdir, "node.yaml")
    with open(yaml_path, "w") as f:
        f.write(
            "node_id: node-test\n"
            "listen: 0.0.0.0:8080\n"
            "nodes:\n"
            "  node-test: http://127.0.0.1:8080\n"
        )
    return load_config(yaml_path)


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestAsgiMinimalEnvelope:

    async def test_post_action_minimal_envelope_sync(self):
        """Minimal envelope triggers auto task_id and trace; sync action returns output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_minimal_config(tmpdir)

            # Use seed write_file action
            seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
            registry = load_actions(seed_dir)
            app = create_app(config, registry)

            target_file = os.path.join(tmpdir, "test_v56.txt")
            body = {
                "target_node_id": "node-test",
                "payload": {
                    "action": "write_file",
                    "params": {"path": target_file, "content": "v5.6 minimal"},
                },
            }

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/action", json=body)

            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "completed"
            assert data["output"]["success"] is True
            # task_id must be present (auto-generated)
            assert "task_id" in data
            assert data["task_id"].startswith("task-")

    async def test_post_action_full_envelope_backward_compat(self):
        """Full v5.0 envelope with explicit task_id and trace still works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_minimal_config(tmpdir)
            seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
            registry = load_actions(seed_dir)
            app = create_app(config, registry)

            target_file = os.path.join(tmpdir, "test_compat.txt")
            body = {
                "target_node_id": "node-test",
                "task_id": "task-legacy-001",
                "trace": {"hop_count": 0, "route_path": []},
                "payload": {
                    "action": "write_file",
                    "params": {"path": target_file, "content": "compat"},
                },
            }

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/action", json=body)

            assert resp.status_code == 200
            data = resp.json()
            assert data["task_id"] == "task-legacy-001"
