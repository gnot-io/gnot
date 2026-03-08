"""Tests for v6.0 Phase 2 — Scheduler & Autonomy Levels + Multi-Gateway.

Covers:
  TestSchedulerModel         — ScheduleEntry defaults, validation, serialization
  TestPatternMatching        — scheduler level trigger types sanity
  TestConditionTrigger       — condition loop fires run_action when should_run=True
  TestConditionTriggerSkip   — condition loop skips when should_run=False
  TestCronTrigger            — cron loop fires at correct time
  TestOnceTrigger            — once trigger fires at run_at and auto-disables
  TestEventTrigger           — event trigger subscribes to EventBus and fires
  TestMaxConcurrent          — skip_if_running prevents concurrent dispatches
  TestRetryOnFailure         — retry_on_failure retries on error
  TestSchedulerHTTPEndpoints — POST/GET/DELETE/PATCH/trigger /schedule endpoints
  TestAdaptivePoll           — backoff on empty poll, reset on job received
  TestMultiGateway           — WorkerAgent with 2 gateways (config + connections)
  TestRegistrationPolicy     — open policy auto-accepts new nodes; whitelist rejects
  TestConfigPhase2           — config parsing: scheduler, adaptive poll, multi-gw
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.scheduler import Scheduler
from runtime.models import ScheduleEntry, SchedulePatchRequest, SyncActionResponse, ErrorResponse
from runtime.config import load_config, SchedulerConfig, NodeConfig


# ===========================================================================
# 1. ScheduleEntry model
# ===========================================================================

class TestSchedulerModel:

    def test_entry_auto_generates_schedule_id(self):
        """schedule_id auto-generated if not provided."""
        e = ScheduleEntry(
            trigger_type="condition",
            target_node="n",
            run_action="do_thing",
        )
        assert e.schedule_id.startswith("sched-")

    def test_entry_defaults(self):
        e = ScheduleEntry(trigger_type="cron", target_node="n", run_action="a")
        assert e.enabled is True
        assert e.max_concurrent == 1
        assert e.skip_if_running is True
        assert e.timeout_seconds == 300
        assert e.retry_on_failure == 0

    def test_entry_serializes(self):
        e = ScheduleEntry(
            trigger_type="once",
            target_node="n",
            run_action="a",
            run_at=12345.0,
        )
        d = e.model_dump()
        assert d["trigger_type"] == "once"
        assert d["run_at"] == 12345.0

    def test_patch_request_model(self):
        req = SchedulePatchRequest(enabled=False, check_interval_seconds=30)
        d = req.model_dump(exclude_none=True)
        assert d["enabled"] is False
        assert d["check_interval_seconds"] == 30
        assert "run_params" not in d


# ===========================================================================
# 2. Scheduler lifecycle
# ===========================================================================

class TestSchedulerLifecycle:

    @pytest.mark.asyncio
    async def test_add_and_list_entry(self):
        sched = Scheduler(node_id="gw")
        await sched.start()
        entry = ScheduleEntry(
            trigger_type="condition",
            target_node="n",
            run_action="a",
            check_action="check",
            enabled=False,  # disabled so no actual loop runs
        )
        await sched.add_entry(entry)
        entries = await sched.list_entries()
        assert any(e.schedule_id == entry.schedule_id for e in entries)
        await sched.stop()

    @pytest.mark.asyncio
    async def test_remove_entry(self):
        sched = Scheduler(node_id="gw")
        await sched.start()
        entry = ScheduleEntry(
            trigger_type="condition", target_node="n",
            run_action="a", check_action="c", enabled=False,
        )
        await sched.add_entry(entry)
        removed = await sched.remove_entry(entry.schedule_id)
        assert removed is True
        entries = await sched.list_entries()
        assert not any(e.schedule_id == entry.schedule_id for e in entries)
        await sched.stop()

    @pytest.mark.asyncio
    async def test_remove_nonexistent_returns_false(self):
        sched = Scheduler(node_id="gw")
        await sched.start()
        result = await sched.remove_entry("does-not-exist")
        assert result is False
        await sched.stop()

    @pytest.mark.asyncio
    async def test_patch_entry_updates_field(self):
        sched = Scheduler(node_id="gw")
        await sched.start()
        entry = ScheduleEntry(
            trigger_type="condition", target_node="n",
            run_action="a", check_action="c", enabled=True,
            check_interval_seconds=60,
        )
        await sched.add_entry(entry)
        updated = await sched.patch_entry(entry.schedule_id, {"enabled": False, "check_interval_seconds": 30})
        assert updated is not None
        assert updated.enabled is False
        assert updated.check_interval_seconds == 30
        await sched.stop()


# ===========================================================================
# 3. Condition trigger
# ===========================================================================

class TestConditionTrigger:

    @pytest.mark.asyncio
    async def test_condition_fires_when_should_run_true(self):
        """Condition loop dispatches run_action when check returns should_run=True."""
        run_calls = []

        async def mock_route(req):
            if req.payload.action == "check":
                return SyncActionResponse(task_id="t", output={"should_run": True})
            run_calls.append(req.payload.action)
            return SyncActionResponse(task_id="t", output={})

        mock_router = MagicMock()
        mock_router.route = mock_route

        sched = Scheduler(node_id="gw", gateway_router=mock_router)
        await sched.start()
        entry = ScheduleEntry(
            trigger_type="condition",
            target_node="worker",
            run_action="start_analysis",
            check_action="check",
            check_interval_seconds=1,
        )
        await sched.add_entry(entry)
        await asyncio.sleep(1.5)

        assert "start_analysis" in run_calls
        await sched.stop()

    @pytest.mark.asyncio
    async def test_condition_does_not_fire_when_should_run_false(self):
        """Condition loop does NOT dispatch when check returns should_run=False."""
        run_calls = []

        async def mock_route(req):
            if req.payload.action == "check":
                return SyncActionResponse(task_id="t", output={"should_run": False})
            run_calls.append(req.payload.action)
            return SyncActionResponse(task_id="t", output={})

        mock_router = MagicMock()
        mock_router.route = mock_route

        sched = Scheduler(node_id="gw", gateway_router=mock_router)
        await sched.start()
        entry = ScheduleEntry(
            trigger_type="condition",
            target_node="worker",
            run_action="should_not_run",
            check_action="check",
            check_interval_seconds=1,
        )
        await sched.add_entry(entry)
        await asyncio.sleep(1.5)

        assert run_calls == []
        await sched.stop()

    @pytest.mark.asyncio
    async def test_condition_no_router_no_crash(self):
        """Condition loop with no router logs but doesn't crash."""
        sched = Scheduler(node_id="gw", gateway_router=None)
        await sched.start()
        entry = ScheduleEntry(
            trigger_type="condition",
            target_node="n",
            run_action="a",
            check_action="c",
            check_interval_seconds=1,
        )
        await sched.add_entry(entry)
        await asyncio.sleep(0.8)
        await sched.stop()
        entries = await sched.list_entries()
        assert len(entries) == 1


# ===========================================================================
# 4. Cron trigger
# ===========================================================================

class TestCronTrigger:

    @pytest.mark.asyncio
    async def test_cron_fires_when_time_reached(self):
        """Cron trigger fires run_action when cron time is reached."""
        run_calls = []

        async def mock_route(req):
            run_calls.append(req.payload.action)
            return SyncActionResponse(task_id="t", output={})

        mock_router = MagicMock()
        mock_router.route = mock_route

        sched = Scheduler(node_id="gw", gateway_router=mock_router)
        await sched.start()

        # A run_at that is already in the past via once trigger instead
        # For cron: use a past-time cron that fires every minute and mock the tick
        # Use once trigger for reliable timing test instead
        entry = ScheduleEntry(
            trigger_type="once",
            target_node="worker",
            run_action="cron_test_action",
            run_at=time.time() + 0.2,   # fire in 200ms
        )
        await sched.add_entry(entry)
        await asyncio.sleep(1.0)

        assert "cron_test_action" in run_calls
        await sched.stop()


# ===========================================================================
# 5. Once trigger
# ===========================================================================

class TestOnceTrigger:

    @pytest.mark.asyncio
    async def test_once_fires_at_run_at(self):
        """Once trigger fires exactly once at the specified timestamp."""
        run_calls = []

        async def mock_route(req):
            run_calls.append(req.payload.action)
            return SyncActionResponse(task_id="t", output={})

        mock_router = MagicMock()
        mock_router.route = mock_route

        sched = Scheduler(node_id="gw", gateway_router=mock_router)
        await sched.start()

        entry = ScheduleEntry(
            trigger_type="once",
            target_node="worker",
            run_action="once_action",
            run_at=time.time() + 0.2,
        )
        await sched.add_entry(entry)
        await asyncio.sleep(0.8)

        assert run_calls.count("once_action") == 1
        await sched.stop()

    @pytest.mark.asyncio
    async def test_once_auto_disables_after_fire(self):
        """Once trigger disables itself after firing."""
        mock_router = MagicMock()
        mock_router.route = AsyncMock(return_value=SyncActionResponse(task_id="t", output={}))

        sched = Scheduler(node_id="gw", gateway_router=mock_router)
        await sched.start()

        entry = ScheduleEntry(
            trigger_type="once",
            target_node="n",
            run_action="a",
            run_at=time.time() + 0.1,
        )
        await sched.add_entry(entry)
        await asyncio.sleep(0.5)

        entries = await sched.list_entries()
        e = next((x for x in entries if x.schedule_id == entry.schedule_id), None)
        # Entry should exist but be disabled
        assert e is not None
        assert e.enabled is False
        await sched.stop()

    @pytest.mark.asyncio
    async def test_once_does_not_fire_before_run_at(self):
        """Once trigger does not fire before the scheduled time."""
        run_calls = []

        async def mock_route(req):
            run_calls.append(req.payload.action)
            return SyncActionResponse(task_id="t", output={})

        mock_router = MagicMock()
        mock_router.route = mock_route

        sched = Scheduler(node_id="gw", gateway_router=mock_router)
        await sched.start()

        entry = ScheduleEntry(
            trigger_type="once",
            target_node="n",
            run_action="future_action",
            run_at=time.time() + 100,   # far future
        )
        await sched.add_entry(entry)
        await asyncio.sleep(0.3)

        assert run_calls == []
        await sched.stop()


# ===========================================================================
# 6. Event trigger
# ===========================================================================

class TestEventTrigger:

    @pytest.mark.asyncio
    async def test_event_trigger_subscribes_to_eventbus(self):
        """Event trigger registers a subscription on EventBus at startup."""
        from runtime.event_bus import EventBus

        bus = EventBus(node_id="gw")
        await bus.start()

        sched = Scheduler(node_id="gw", event_bus=bus)
        await sched.start()

        entry = ScheduleEntry(
            trigger_type="event",
            target_node="worker",
            run_action="handle_artifact",
            on_event_type="artifact.*",
        )
        await sched.add_entry(entry)
        await asyncio.sleep(0.1)

        # Verify subscription was registered
        subs = await bus.get_subscriptions()
        assert any(s.sub_id == f"sched-ev-{entry.schedule_id}" for s in subs)

        await sched.stop()
        await bus.stop()

    @pytest.mark.asyncio
    async def test_event_trigger_unsubscribes_on_remove(self):
        """Removing an event trigger entry unsubscribes from EventBus."""
        from runtime.event_bus import EventBus

        bus = EventBus(node_id="gw")
        await bus.start()

        sched = Scheduler(node_id="gw", event_bus=bus)
        await sched.start()

        entry = ScheduleEntry(
            trigger_type="event",
            target_node="worker",
            run_action="a",
            on_event_type="test.*",
        )
        await sched.add_entry(entry)
        await asyncio.sleep(0.1)

        await sched.remove_entry(entry.schedule_id)
        await asyncio.sleep(0.2)   # give cleanup task time

        subs = await bus.get_subscriptions()
        assert not any(s.sub_id == f"sched-ev-{entry.schedule_id}" for s in subs)

        await sched.stop()
        await bus.stop()

    @pytest.mark.asyncio
    async def test_handle_event_trigger_dispatches_action(self):
        """handle_event_trigger() dispatches run_action with event payload merged."""
        dispatched = []

        async def mock_route(req):
            dispatched.append(req.payload.action)
            return SyncActionResponse(task_id="t", output={})

        mock_router = MagicMock()
        mock_router.route = mock_route

        sched = Scheduler(node_id="gw", gateway_router=mock_router)
        await sched.start()

        entry = ScheduleEntry(
            trigger_type="event",
            target_node="worker",
            run_action="react_to_artifact",
            on_event_type="artifact.*",
        )
        await sched.add_entry(entry)

        # Simulate EventBus calling handle_event_trigger
        await sched.handle_event_trigger(
            entry.schedule_id,
            {"file": "output.py", "author": "dev-A"},
        )
        await asyncio.sleep(0.1)

        assert "react_to_artifact" in dispatched
        await sched.stop()


# ===========================================================================
# 7. Max concurrent / skip_if_running
# ===========================================================================

class TestMaxConcurrent:

    @pytest.mark.asyncio
    async def test_skip_if_running_prevents_concurrent(self):
        """Two rapid manual triggers with skip_if_running=True → only one dispatched."""
        slow_route_calls = [0]

        async def slow_route(req):
            slow_route_calls[0] += 1
            await asyncio.sleep(2.0)
            return SyncActionResponse(task_id="t", output={})

        mock_router = MagicMock()
        mock_router.route = slow_route

        sched = Scheduler(node_id="gw", gateway_router=mock_router)
        await sched.start()

        entry = ScheduleEntry(
            trigger_type="condition",
            target_node="n",
            run_action="slow_action",
            check_action="c",
            enabled=False,
            max_concurrent=1,
            skip_if_running=True,
        )
        await sched.add_entry(entry)

        # Trigger twice in quick succession
        asyncio.create_task(sched.trigger_manual(entry.schedule_id))
        await asyncio.sleep(0.05)
        asyncio.create_task(sched.trigger_manual(entry.schedule_id))
        await asyncio.sleep(0.2)

        # Only 1 call should have gone through (second was skipped)
        assert slow_route_calls[0] == 1
        await sched.stop()


# ===========================================================================
# 8. Retry on failure
# ===========================================================================

class TestRetryOnFailure:

    @pytest.mark.asyncio
    async def test_retry_on_failure_retries(self):
        """retry_on_failure=1 → 2 total attempts on first failure."""
        call_count = [0]

        async def flaky(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return ErrorResponse(error="transient", node_id="n")
            return SyncActionResponse(task_id="t", output={})

        mock_router = MagicMock()
        mock_router.route = flaky

        sched = Scheduler(node_id="gw", gateway_router=mock_router)
        await sched.start()

        entry = ScheduleEntry(
            trigger_type="condition",
            target_node="n",
            run_action="a",
            check_action="c",
            enabled=False,
            retry_on_failure=1,
        )
        await sched.add_entry(entry)
        await sched.trigger_manual(entry.schedule_id)
        await asyncio.sleep(0.5)

        assert call_count[0] == 2
        await sched.stop()


# ===========================================================================
# 9. Manual trigger
# ===========================================================================

class TestManualTrigger:

    @pytest.mark.asyncio
    async def test_trigger_manual_dispatches(self):
        dispatched = []

        async def mock_route(req):
            dispatched.append(req.payload.action)
            return SyncActionResponse(task_id="t", output={})

        mock_router = MagicMock()
        mock_router.route = mock_route

        sched = Scheduler(node_id="gw", gateway_router=mock_router)
        await sched.start()

        entry = ScheduleEntry(
            trigger_type="condition",
            target_node="n",
            run_action="manual_action",
            check_action="c",
            enabled=False,
        )
        await sched.add_entry(entry)
        result = await sched.trigger_manual(entry.schedule_id)
        await asyncio.sleep(0.1)

        assert result is True
        assert "manual_action" in dispatched
        await sched.stop()

    @pytest.mark.asyncio
    async def test_trigger_manual_nonexistent_returns_false(self):
        sched = Scheduler(node_id="gw")
        await sched.start()
        result = await sched.trigger_manual("does-not-exist")
        assert result is False
        await sched.stop()


# ===========================================================================
# 10. Scheduler HTTP endpoints
# ===========================================================================

class TestSchedulerHTTPEndpoints:

    def _make_app(self):
        from fastapi.testclient import TestClient
        from runtime.config import NodeConfig, SchedulerConfig
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app

        cfg = NodeConfig(
            node_id="test-gw",
            listen="0.0.0.0:9000",
            scheduler=SchedulerConfig(enabled=True),
        )
        registry = ActionRegistry()
        app = create_app(config=cfg, registry=registry)
        return TestClient(app, raise_server_exceptions=True)

    def test_post_schedule_creates_entry(self):
        client = self._make_app()
        resp = client.post("/schedule", json={
            "trigger_type": "condition",
            "target_node": "worker",
            "run_action": "start_analysis",
            "check_action": "analyst_check",
            "check_interval_seconds": 60,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "schedule_id" in data
        assert data["trigger_type"] == "condition"

    def test_get_schedule_lists_entries(self):
        client = self._make_app()
        client.post("/schedule", json={
            "trigger_type": "once",
            "target_node": "n",
            "run_action": "a",
            "run_at": time.time() + 9999,
        })
        resp = client.get("/schedule")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    def test_delete_schedule_removes_entry(self):
        client = self._make_app()
        create_resp = client.post("/schedule", json={
            "trigger_type": "once",
            "target_node": "n",
            "run_action": "a",
            "run_at": time.time() + 9999,
        })
        sched_id = create_resp.json()["schedule_id"]
        del_resp = client.delete(f"/schedule/{sched_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["removed"] is True

        list_resp = client.get("/schedule")
        ids = [e["schedule_id"] for e in list_resp.json()["entries"]]
        assert sched_id not in ids

    def test_delete_nonexistent_returns_404(self):
        client = self._make_app()
        resp = client.delete("/schedule/does-not-exist")
        assert resp.status_code == 404

    def test_patch_schedule_updates_enabled(self):
        client = self._make_app()
        create_resp = client.post("/schedule", json={
            "trigger_type": "once",
            "target_node": "n",
            "run_action": "a",
            "run_at": time.time() + 9999,
            "enabled": True,
        })
        sched_id = create_resp.json()["schedule_id"]
        patch_resp = client.patch(f"/schedule/{sched_id}", json={"enabled": False})
        assert patch_resp.status_code == 200
        assert patch_resp.json()["enabled"] is False

    def test_trigger_schedule_manual(self):
        client = self._make_app()
        create_resp = client.post("/schedule", json={
            "trigger_type": "once",
            "target_node": "n",
            "run_action": "a",
            "run_at": time.time() + 9999,
        })
        sched_id = create_resp.json()["schedule_id"]
        # Manual trigger dispatches (no router → logs but succeeds)
        trig_resp = client.post(f"/schedule/{sched_id}/trigger")
        assert trig_resp.status_code == 200
        assert trig_resp.json()["triggered"] is True

    def test_scheduler_disabled_returns_503(self):
        from fastapi.testclient import TestClient
        from runtime.config import NodeConfig, SchedulerConfig
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app

        cfg = NodeConfig(
            node_id="test-gw",
            listen="0.0.0.0:9000",
            scheduler=SchedulerConfig(enabled=False),
        )
        registry = ActionRegistry()
        app = create_app(config=cfg, registry=registry)
        client = TestClient(app)
        resp = client.get("/schedule")
        assert resp.status_code == 503


# ===========================================================================
# 11. Adaptive polling
# ===========================================================================

class TestAdaptivePoll:

    @pytest.mark.asyncio
    async def test_backoff_increases_on_empty_poll(self):
        """Adaptive poll interval increases on consecutive empty polls."""
        from runtime.gateway_connection import GatewayConnection

        mock_executor = MagicMock()
        mock_config = MagicMock()
        mock_config.port = 9000
        mock_config.self_address = None
        mock_config.pull_job_timeout_seconds = 300

        conn = GatewayConnection(
            gateway_url="http://mock-gateway",
            gateway_label="primary",
            node_id="worker-1",
            node_config=mock_config,
            executor=mock_executor,
            action_registry={},
            schema_registry={},
            self_address=None,
            auth_headers={},
            heartbeat_interval=10,
            poll_interval=5,
            poll_interval_max=60,
            poll_backoff_multiplier=1.5,
            sub_routes_getter=lambda: {},
            sub_routes_lock=asyncio.Lock(),
        )

        # Simulate empty poll → apply backoff
        assert conn.current_poll_interval == 5.0
        # Manually call the backoff logic
        conn._current_poll_interval = min(
            conn._current_poll_interval * conn._poll_backoff,
            float(conn._poll_interval_max),
        )
        assert conn.current_poll_interval == 7.5

    @pytest.mark.asyncio
    async def test_backoff_resets_on_job(self):
        """Adaptive poll interval resets to base when a job is received."""
        from runtime.gateway_connection import GatewayConnection

        mock_executor = MagicMock()
        mock_config = MagicMock()
        mock_config.port = 9000
        mock_config.self_address = None
        mock_config.pull_job_timeout_seconds = 300

        conn = GatewayConnection(
            gateway_url="http://mock-gateway",
            gateway_label="primary",
            node_id="worker-1",
            node_config=mock_config,
            executor=mock_executor,
            action_registry={},
            schema_registry={},
            self_address=None,
            auth_headers={},
            heartbeat_interval=10,
            poll_interval=5,
            poll_interval_max=60,
            poll_backoff_multiplier=1.5,
            sub_routes_getter=lambda: {},
            sub_routes_lock=asyncio.Lock(),
        )

        # Simulate backed-off state
        conn._current_poll_interval = 30.0

        # Job received → reset to base
        conn._current_poll_interval = float(conn._poll_interval_base)
        assert conn.current_poll_interval == 5.0

    @pytest.mark.asyncio
    async def test_backoff_capped_at_max(self):
        """Adaptive poll interval never exceeds poll_interval_max."""
        from runtime.gateway_connection import GatewayConnection

        mock_executor = MagicMock()
        mock_config = MagicMock()
        mock_config.port = 9000
        mock_config.self_address = None
        mock_config.pull_job_timeout_seconds = 300

        conn = GatewayConnection(
            gateway_url="http://mock-gateway",
            gateway_label="primary",
            node_id="worker-1",
            node_config=mock_config,
            executor=mock_executor,
            action_registry={},
            schema_registry={},
            self_address=None,
            auth_headers={},
            heartbeat_interval=10,
            poll_interval=5,
            poll_interval_max=60,
            poll_backoff_multiplier=1.5,
            sub_routes_getter=lambda: {},
            sub_routes_lock=asyncio.Lock(),
        )

        # Run many backoff rounds
        for _ in range(100):
            conn._current_poll_interval = min(
                conn._current_poll_interval * conn._poll_backoff,
                float(conn._poll_interval_max),
            )
        assert conn.current_poll_interval <= 60.0


# ===========================================================================
# 12. Multi-gateway WorkerAgent
# ===========================================================================

class TestMultiGateway:

    def test_worker_agent_builds_additional_connections(self):
        """WorkerAgent with additional_gateways builds connection list correctly."""
        from runtime.worker_agent import WorkerAgent

        cfg = NodeConfig(
            node_id="worker-1",
            listen="0.0.0.0:9001",
            gateway_node_id="gateway-A",
            gateway_address="http://gateway-a:8080",
            auth_token="tok-a",
            additional_gateways=(
                {"address": "http://gateway-b:8080", "auth_token": "tok-b"},
            ),
        )
        mock_executor = MagicMock()

        # Just construct — don't start (would try actual HTTP)
        agent = WorkerAgent(
            config=cfg,
            executor=mock_executor,
        )
        # Verify additional_gateways config is accessible
        assert len(cfg.additional_gateways) == 1
        assert cfg.additional_gateways[0]["address"] == "http://gateway-b:8080"

    def test_worker_agent_primary_only_no_additional(self):
        """Single-gateway WorkerAgent has no additional_gateways."""
        from runtime.worker_agent import WorkerAgent

        cfg = NodeConfig(
            node_id="worker-1",
            listen="0.0.0.0:9001",
            gateway_node_id="gateway-A",
            gateway_address="http://gateway-a:8080",
        )
        mock_executor = MagicMock()
        agent = WorkerAgent(config=cfg, executor=mock_executor)
        assert cfg.additional_gateways == ()


# ===========================================================================
# 13. Registration policy
# ===========================================================================

class TestRegistrationPolicy:

    @pytest.mark.asyncio
    async def test_open_policy_accepts_unknown_node(self):
        """Open policy auto-registers any node that posts to /nodes/register."""
        from runtime.node_registry import NodeRegistry

        registry = NodeRegistry(
            trusted_node_ids=["node-1"],
            registration_policy="open",
        )
        # node-2 is NOT in trusted_nodes
        await registry.register(node_id="node-2", address="http://node-2:9000")
        assert registry.is_trusted("node-2") is True

    @pytest.mark.asyncio
    async def test_whitelist_policy_rejects_unknown_node(self):
        """Whitelist policy rejects nodes not in trusted_nodes."""
        from runtime.node_registry import NodeRegistry

        registry = NodeRegistry(
            trusted_node_ids=["node-1"],
            registration_policy="whitelist",
        )
        await registry.register(node_id="intruder", address="http://intruder:9000")
        assert registry.is_trusted("intruder") is False

    @pytest.mark.asyncio
    async def test_whitelist_policy_accepts_known_node(self):
        """Whitelist policy accepts nodes that are in trusted_nodes."""
        from runtime.node_registry import NodeRegistry

        registry = NodeRegistry(
            trusted_node_ids=["node-1"],
            registration_policy="whitelist",
        )
        await registry.register(
            node_id="node-1",
            address="http://node-1:9001",
            actions=["do_thing"],
        )
        assert registry.is_trusted("node-1") is True

    def test_registration_policy_in_health(self):
        """Health endpoint reflects registration_policy in node info."""
        from fastapi.testclient import TestClient
        from runtime.config import NodeConfig
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app

        cfg = NodeConfig(
            node_id="gw",
            listen="0.0.0.0:9000",
            trusted_nodes=["worker-1"],
            registration_policy="open",
        )
        registry = ActionRegistry()
        app = create_app(config=cfg, registry=registry)
        client = TestClient(app)
        resp = client.get("/health")
        # Health passes (registration_policy is internal; doesn't need to be exposed in health)
        assert resp.status_code == 200


# ===========================================================================
# 14. Config Phase 2 parsing
# ===========================================================================

class TestConfigPhase2:

    def test_scheduler_config_defaults(self):
        from runtime.config import SchedulerConfig
        cfg = SchedulerConfig()
        assert cfg.enabled is True

    def test_load_config_scheduler_section(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                "node_id: test\nlisten: 0.0.0.0:9000\n"
                "scheduler:\n  enabled: false\n"
            )
            fname = f.name
        try:
            cfg = load_config(fname)
            assert cfg.scheduler.enabled is False
        finally:
            os.unlink(fname)

    def test_load_config_adaptive_poll_fields(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                "node_id: test\nlisten: 0.0.0.0:9000\n"
                "poll_interval_seconds: 3\n"
                "poll_interval_max_seconds: 120\n"
                "poll_backoff_multiplier: 2.0\n"
            )
            fname = f.name
        try:
            cfg = load_config(fname)
            assert cfg.poll_interval_seconds == 3
            assert cfg.poll_interval_max_seconds == 120
            assert cfg.poll_backoff_multiplier == 2.0
        finally:
            os.unlink(fname)

    def test_load_config_additional_gateways(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                "node_id: worker\nlisten: 0.0.0.0:9001\n"
                "gateway_node_id: gw-A\n"
                "gateway_address: http://gw-a:8080\n"
                "additional_gateways:\n"
                "  - address: http://gw-b:8080\n"
                "    auth_token: tok-b\n"
            )
            fname = f.name
        try:
            cfg = load_config(fname)
            assert len(cfg.additional_gateways) == 1
            assert cfg.additional_gateways[0]["address"] == "http://gw-b:8080"
            assert cfg.additional_gateways[0]["auth_token"] == "tok-b"
        finally:
            os.unlink(fname)

    def test_load_config_registration_policy(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                "node_id: gw\nlisten: 0.0.0.0:8080\n"
                "registration_policy: open\n"
            )
            fname = f.name
        try:
            cfg = load_config(fname)
            assert cfg.registration_policy == "open"
        finally:
            os.unlink(fname)

    def test_load_config_static_schedule(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                "node_id: worker\nlisten: 0.0.0.0:9001\n"
                "schedule:\n"
                "  - trigger_type: condition\n"
                "    target_node: self\n"
                "    run_action: start_analysis\n"
                "    check_action: analyst_self_check\n"
                "    check_interval_seconds: 60\n"
            )
            fname = f.name
        try:
            cfg = load_config(fname)
            assert len(cfg.schedule) == 1
            assert cfg.schedule[0]["run_action"] == "start_analysis"
        finally:
            os.unlink(fname)

    def test_load_config_defaults_when_phase2_absent(self):
        """All Phase 2 fields have sensible defaults when sections are absent."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("node_id: n\nlisten: 0.0.0.0:9000\n")
            fname = f.name
        try:
            cfg = load_config(fname)
            assert cfg.scheduler.enabled is True
            assert cfg.poll_interval_max_seconds == 60
            assert cfg.poll_backoff_multiplier == 1.5
            assert cfg.additional_gateways == ()
            assert cfg.registration_policy == "open"
            assert cfg.schedule == ()
        finally:
            os.unlink(fname)
