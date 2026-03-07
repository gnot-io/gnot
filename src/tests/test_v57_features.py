"""Tests for v5.7 features.

Covers 4 items:
  1. Lazy staleness check in NodeRegistry
     - get_address: stale node → UNREACHABLE before address returned
     - get_status:  stale node → UNREACHABLE
     - ping:        stale node → skip network probe, return False
     - Non-stale node → not affected
     - Node with no heartbeat → not marked stale

  2. Pull job timeout (lazy check in route_result)
     - Job within timeout → returns current status unchanged
     - Job past timeout   → status becomes "failed" with timeout error message

  3. Lifespan migration
     - ASGI startup/shutdown completes without DeprecationWarning

  4. Queue depth in /health
     - /health returns queue_depths dict
     - Reflects correct per-node counts
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import warnings

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.models import NodeStatus
from runtime.node_registry import NodeRegistry


# ---------------------------------------------------------------------------
# 1. Lazy staleness check — NodeRegistry
# ---------------------------------------------------------------------------

class TestLazyStaleness:

    def _make_registry(self, timeout: int = 30) -> NodeRegistry:
        reg = NodeRegistry(
            trusted_node_ids=["node-1"],
            heartbeat_timeout_seconds=timeout,
        )
        return reg

    @pytest.mark.asyncio
    async def test_get_address_stale_node_marked_unreachable(self):
        reg = self._make_registry(timeout=5)
        # Simulate a heartbeat received 10 seconds ago (beyond 5s timeout)
        await reg.heartbeat("node-1")
        reg._entries["node-1"].last_heartbeat = time.time() - 10  # manually age it

        addr = await reg.get_address("node-1")
        # Address is still returned (caller might log it), but status changed
        status = await reg.get_status("node-1")
        assert status == NodeStatus.UNREACHABLE

    @pytest.mark.asyncio
    async def test_get_status_stale_marks_unreachable(self):
        reg = self._make_registry(timeout=5)
        await reg.heartbeat("node-1")
        reg._entries["node-1"].last_heartbeat = time.time() - 10

        status = await reg.get_status("node-1")
        assert status == NodeStatus.UNREACHABLE

    @pytest.mark.asyncio
    async def test_fresh_node_not_marked_stale(self):
        reg = self._make_registry(timeout=30)
        await reg.heartbeat("node-1")
        # Heartbeat is fresh (just now)

        status = await reg.get_status("node-1")
        assert status == NodeStatus.ONLINE

    @pytest.mark.asyncio
    async def test_ping_skips_probe_for_stale_node(self):
        """Stale node should not be probed over the network — return False immediately."""
        reg = self._make_registry(timeout=5)
        await reg.heartbeat("node-1")
        reg._entries["node-1"].last_heartbeat = time.time() - 10
        reg._entries["node-1"].address = "http://10.0.0.1:8080"

        # ping() should NOT make a network call — node is already UNREACHABLE after lazy check
        import unittest.mock as mock
        with mock.patch("httpx.AsyncClient") as mock_client:
            result = await reg.ping("node-1")
        # Network was never called
        mock_client.assert_not_called()
        assert result is False

    @pytest.mark.asyncio
    async def test_node_without_heartbeat_not_stale(self):
        """A node that has never sent a heartbeat (UNKNOWN) should NOT be marked stale."""
        reg = self._make_registry(timeout=5)
        # node-1 was added from config but never sent a heartbeat

        status = await reg.get_status("node-1")
        # Should remain UNKNOWN, not UNREACHABLE
        assert status == NodeStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_already_unreachable_node_not_double_logged(self):
        """UNREACHABLE nodes are not re-processed by lazy check."""
        reg = self._make_registry(timeout=5)
        await reg.heartbeat("node-1")
        reg._entries["node-1"].last_heartbeat = time.time() - 10
        reg._entries["node-1"].status = NodeStatus.UNREACHABLE  # already unreachable

        # Calling get_status should not crash or re-log
        status = await reg.get_status("node-1")
        assert status == NodeStatus.UNREACHABLE


# ---------------------------------------------------------------------------
# 2. Pull job timeout — lazy check in route_result
# ---------------------------------------------------------------------------

try:
    from httpx import AsyncClient, ASGITransport
    from runtime.action_loader import load_actions
    from runtime.config import load_config
    from runtime.server import create_app

    INTEGRATION_AVAILABLE = True
except ImportError:
    INTEGRATION_AVAILABLE = False


def _make_config(tmpdir: str, extra_yaml: str = "") -> object:
    yaml_path = os.path.join(tmpdir, "node.yaml")
    with open(yaml_path, "w") as f:
        f.write(
            "node_id: node-gw\n"
            "listen: 0.0.0.0:8080\n"
            "trusted_nodes:\n"
            "  - node-worker\n"
            f"{extra_yaml}"
        )
    return load_config(yaml_path)


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestPullJobTimeout:

    async def test_pull_job_within_timeout_returns_queued(self):
        """A recently created pull job should not be timed out."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir, "pull_job_timeout_seconds: 300\n")
            seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
            registry = load_actions(seed_dir)
            app = create_app(config, registry)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Send action to untrusted pull worker — gets queued
                resp = await client.post("/action", json={
                    "target_node_id": "node-worker",
                    "payload": {"action": "read_file", "params": {"path": "/tmp/x"}},
                })
                # node-worker has no address → pull mode → accepted
                assert resp.status_code in (200, 202)
                data = resp.json()
                job_id = data.get("job_id")
                if job_id is None:
                    pytest.skip("Worker resolved locally, not queued")

                # Poll immediately — should still be queued/accepted, NOT failed
                result = await client.get(f"/result/{job_id}")
                assert result.status_code == 200
                status = result.json().get("status")
                assert status in ("queued", "accepted", "running")

    async def test_pull_job_past_timeout_returns_failed(self):
        """A pull job older than timeout_seconds should be marked failed lazily."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Set very short timeout
            config = _make_config(tmpdir, "pull_job_timeout_seconds: 1\n")
            seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
            registry = load_actions(seed_dir)
            app = create_app(config, registry)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/action", json={
                    "target_node_id": "node-worker",
                    "payload": {"action": "read_file", "params": {"path": "/tmp/x"}},
                })
                data = resp.json()
                job_id = data.get("job_id")
                if job_id is None:
                    pytest.skip("Worker resolved locally, not queued")

                # Wait past the 1-second timeout
                await asyncio.sleep(2)

                # Now poll — should be FAILED
                result = await client.get(f"/result/{job_id}")
                assert result.status_code == 200
                rdata = result.json()
                assert rdata["status"] == "failed"
                assert "timed out" in rdata.get("error", "").lower()


# ---------------------------------------------------------------------------
# 3. Lifespan migration — no DeprecationWarning from on_event
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestLifespanMigration:

    async def test_no_deprecation_warning_on_startup(self):
        """FastAPI app should start without on_event DeprecationWarning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = os.path.join(tmpdir, "node.yaml")
            with open(yaml_path, "w") as f:
                f.write("node_id: node-test\nlisten: 0.0.0.0:8080\n")
            config = load_config(yaml_path)
            seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
            registry = load_actions(seed_dir)

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                app = create_app(config, registry)
                # Trigger ASGI lifespan
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    await client.get("/health")

            deprecation_msgs = [
                str(w.message) for w in caught
                if issubclass(w.category, DeprecationWarning)
                and "on_event" in str(w.message)
            ]
            assert deprecation_msgs == [], (
                f"on_event DeprecationWarning still present: {deprecation_msgs}"
            )


# ---------------------------------------------------------------------------
# 4. Queue depth in /health
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestQueueDepthInHealth:

    async def test_health_includes_queue_depths_field(self):
        """/health response must include queue_depths dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)
            seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
            registry = load_actions(seed_dir)
            app = create_app(config, registry)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

            assert resp.status_code == 200
            data = resp.json()
            assert "queue_depths" in data
            assert isinstance(data["queue_depths"], dict)

    async def test_health_queue_depth_reflects_queued_jobs(self):
        """After queuing a job for node-worker, queue_depths should show count=1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir, "pull_job_timeout_seconds: 300\n")
            seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
            registry = load_actions(seed_dir)
            app = create_app(config, registry)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Queue a pull job
                resp = await client.post("/action", json={
                    "target_node_id": "node-worker",
                    "payload": {"action": "read_file", "params": {"path": "/tmp/x"}},
                })
                job_id = resp.json().get("job_id")
                if job_id is None:
                    pytest.skip("Worker resolved locally, not queued")

                health = await client.get("/health")
                data = health.json()
                depths = data.get("queue_depths", {})
                assert depths.get("node-worker", 0) >= 1
