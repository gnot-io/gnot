"""Tests for v6.0 Phase 1 — EventBus & Pub/Sub Foundation.

Covers:
  1. Event model — creation, defaults, serialization
  2. Subscription model — creation, defaults, serialization
  3. Pattern matching — exact, prefix wildcard, suffix wildcard, all
  4. Payload filter — match / no-match
  5. EventBus.emit — basic fan-out, matched count
  6. EventBus.subscribe / unsubscribe
  7. EventBus.get_events — filtering (type, source, since, limit)
  8. Debounce — skip delivery within debounce window
  9. Max deliveries — auto-unsubscribe on limit
  10. Delivery retry — retry on failure, exponential backoff
  11. Delivery via no-router (standalone mode — no crash)
  12. JSONL persistence — append on emit, load on startup
  13. Config parsing — EventBusConfig from node.yaml
  14. ChannelRegistry — thin wrapper delegates to NodeRegistry
  15. HTTP endpoints — POST /emit, POST /subscribe, DELETE, GET /subscriptions, GET /events
  16. Health endpoint — v6 event_bus section
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.event_bus import (
    EventBus,
    _matches_pattern,
    _matches_payload_filter,
)
from runtime.models import (
    Event,
    Subscription,
    EmitRequest,
    EmitResponse,
    SubscribeRequest,
    SubscribeResponse,
)
from runtime.channel_registry import ChannelRegistry
from runtime.config import EventBusConfig, load_config


# ===========================================================================
# 1. Event model
# ===========================================================================

class TestEventModel:

    def test_event_auto_generates_id(self):
        """event_id is auto-generated uuid4 string."""
        e = Event(event_type="test.done", source_node="node-1", payload={})
        assert e.event_id
        assert len(e.event_id) == 36  # UUID format

    def test_event_auto_generates_timestamp(self):
        """timestamp is set to current time."""
        before = time.time()
        e = Event(event_type="test.done", source_node="node-1")
        assert e.timestamp >= before

    def test_event_serializes_to_dict(self):
        """Event.model_dump() returns all fields."""
        e = Event(
            event_type="artifact.written",
            source_node="dev-A",
            payload={"file": "out.py"},
            correlation_id="corr-1",
            reply_to="pm-node",
        )
        d = e.model_dump()
        assert d["event_type"] == "artifact.written"
        assert d["payload"]["file"] == "out.py"
        assert d["correlation_id"] == "corr-1"

    def test_event_roundtrip_json(self):
        """Event survives JSON serialization/deserialization."""
        e = Event(event_type="task.completed", source_node="worker-1")
        json_str = e.model_dump_json()
        e2 = Event.model_validate_json(json_str)
        assert e2.event_id == e.event_id
        assert e2.event_type == e.event_type


# ===========================================================================
# 2. Subscription model
# ===========================================================================

class TestSubscriptionModel:

    def test_subscription_auto_generates_sub_id(self):
        """sub_id is auto-generated."""
        s = Subscription(subscriber_node="node-1", callback_action="handle_event")
        assert s.sub_id.startswith("sub-")

    def test_subscription_defaults(self):
        """Default pattern is '*', no filter, no debounce, unlimited deliveries."""
        s = Subscription(subscriber_node="node-1", callback_action="handle")
        assert s.event_type_pattern == "*"
        assert s.source_node is None
        assert s.payload_filter is None
        assert s.debounce_seconds == 0.0
        assert s.max_deliveries is None


# ===========================================================================
# 3. Pattern matching
# ===========================================================================

class TestPatternMatching:

    def test_wildcard_all_matches_everything(self):
        assert _matches_pattern("*", "test.failed") is True
        assert _matches_pattern("*", "artifact.written") is True
        assert _matches_pattern("*", "anything") is True

    def test_exact_match(self):
        assert _matches_pattern("test.failed", "test.failed") is True
        assert _matches_pattern("test.failed", "test.passed") is False
        assert _matches_pattern("test.failed", "test.failed.extra") is False

    def test_prefix_wildcard(self):
        assert _matches_pattern("test.*", "test.failed") is True
        assert _matches_pattern("test.*", "test.passed") is True
        assert _matches_pattern("test.*", "test.anything") is True
        assert _matches_pattern("test.*", "other.failed") is False
        assert _matches_pattern("test.*", "test") is False  # no dot

    def test_suffix_wildcard(self):
        assert _matches_pattern("*.failed", "test.failed") is True
        assert _matches_pattern("*.failed", "review.failed") is True
        assert _matches_pattern("*.failed", "test.passed") is False
        assert _matches_pattern("*.failed", "failed") is False  # no dot prefix

    def test_prefix_and_suffix_no_overlap(self):
        """Prefix wildcard doesn't match suffix patterns."""
        assert _matches_pattern("test.*", "other.test.failed") is False


# ===========================================================================
# 4. Payload filter
# ===========================================================================

class TestPayloadFilter:

    def test_filter_matches_subset(self):
        assert _matches_payload_filter({"status": "ok"}, {"status": "ok", "extra": 1}) is True

    def test_filter_rejects_wrong_value(self):
        assert _matches_payload_filter({"status": "ok"}, {"status": "fail"}) is False

    def test_filter_rejects_missing_key(self):
        assert _matches_payload_filter({"key": "val"}, {}) is False

    def test_empty_filter_always_matches(self):
        assert _matches_payload_filter({}, {"anything": "here"}) is True


# ===========================================================================
# 5. EventBus.emit — basic fan-out
# ===========================================================================

class TestEventBusEmit:

    @pytest.mark.asyncio
    async def test_emit_returns_matched_count(self):
        """emit() returns number of subscriptions that matched."""
        bus = EventBus(node_id="gw")
        await bus.start()
        sub1 = Subscription(
            subscriber_node="node-1", callback_action="handle",
            event_type_pattern="test.*",
        )
        sub2 = Subscription(
            subscriber_node="node-2", callback_action="handle",
            event_type_pattern="artifact.*",
        )
        await bus.subscribe(sub1)
        await bus.subscribe(sub2)

        event = Event(event_type="test.failed", source_node="dev-A")
        matched = await bus.emit(event)
        assert matched == 1  # only sub1 matches

        await bus.stop()

    @pytest.mark.asyncio
    async def test_emit_appends_to_event_log(self):
        """Emitted events appear in the event log."""
        bus = EventBus(node_id="gw")
        await bus.start()
        e1 = Event(event_type="task.started", source_node="pm")
        e2 = Event(event_type="task.completed", source_node="pm")
        await bus.emit(e1)
        await bus.emit(e2)

        events = await bus.get_events()
        ids = [e.event_id for e in events]
        assert e1.event_id in ids
        assert e2.event_id in ids
        await bus.stop()

    @pytest.mark.asyncio
    async def test_emit_no_match_returns_zero(self):
        """emit() returns 0 when no subscriptions match."""
        bus = EventBus(node_id="gw")
        await bus.start()
        sub = Subscription(
            subscriber_node="node-1", callback_action="handle",
            event_type_pattern="artifact.*",
        )
        await bus.subscribe(sub)
        event = Event(event_type="test.failed", source_node="dev")
        matched = await bus.emit(event)
        assert matched == 0
        await bus.stop()


# ===========================================================================
# 6. Subscribe / Unsubscribe
# ===========================================================================

class TestSubscribeUnsubscribe:

    @pytest.mark.asyncio
    async def test_subscribe_returns_sub_id(self):
        bus = EventBus(node_id="gw")
        await bus.start()
        sub = Subscription(subscriber_node="n", callback_action="a")
        sub_id = await bus.subscribe(sub)
        assert sub_id == sub.sub_id
        await bus.stop()

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_subscription(self):
        bus = EventBus(node_id="gw")
        await bus.start()
        sub = Subscription(subscriber_node="n", callback_action="a")
        await bus.subscribe(sub)
        removed = await bus.unsubscribe(sub.sub_id)
        assert removed is True

        # No longer matches after unsubscribe
        e = Event(event_type="*", source_node="x")
        matched = await bus.emit(e)
        assert matched == 0
        await bus.stop()

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_returns_false(self):
        bus = EventBus(node_id="gw")
        await bus.start()
        result = await bus.unsubscribe("sub-does-not-exist")
        assert result is False
        await bus.stop()

    @pytest.mark.asyncio
    async def test_get_subscriptions_lists_all(self):
        bus = EventBus(node_id="gw")
        await bus.start()
        s1 = Subscription(subscriber_node="n1", callback_action="a")
        s2 = Subscription(subscriber_node="n2", callback_action="b")
        await bus.subscribe(s1)
        await bus.subscribe(s2)
        subs = await bus.get_subscriptions()
        ids = [s.sub_id for s in subs]
        assert s1.sub_id in ids
        assert s2.sub_id in ids
        await bus.stop()


# ===========================================================================
# 7. EventBus.get_events — filtering
# ===========================================================================

class TestGetEvents:

    @pytest.mark.asyncio
    async def test_filter_by_event_type(self):
        bus = EventBus(node_id="gw")
        await bus.start()
        await bus.emit(Event(event_type="test.failed", source_node="dev"))
        await bus.emit(Event(event_type="artifact.written", source_node="dev"))

        result = await bus.get_events(event_type="test.*")
        assert all(e.event_type == "test.failed" for e in result)
        assert len(result) == 1
        await bus.stop()

    @pytest.mark.asyncio
    async def test_filter_by_source_node(self):
        bus = EventBus(node_id="gw")
        await bus.start()
        await bus.emit(Event(event_type="x", source_node="node-A"))
        await bus.emit(Event(event_type="x", source_node="node-B"))

        result = await bus.get_events(source_node="node-A")
        assert len(result) == 1
        assert result[0].source_node == "node-A"
        await bus.stop()

    @pytest.mark.asyncio
    async def test_filter_by_since(self):
        bus = EventBus(node_id="gw")
        await bus.start()
        old = Event(event_type="x", source_node="s")
        old = old.model_copy(update={"timestamp": time.time() - 100})
        new = Event(event_type="x", source_node="s")

        await bus.emit(old)
        await bus.emit(new)

        since = time.time() - 10
        result = await bus.get_events(since=since)
        assert len(result) == 1
        assert result[0].event_id == new.event_id
        await bus.stop()

    @pytest.mark.asyncio
    async def test_limit_truncates_results(self):
        bus = EventBus(node_id="gw")
        await bus.start()
        for i in range(10):
            await bus.emit(Event(event_type="x", source_node="s"))
        result = await bus.get_events(limit=3)
        assert len(result) == 3
        await bus.stop()


# ===========================================================================
# 8. Debounce
# ===========================================================================

class TestDebounce:

    @pytest.mark.asyncio
    async def test_debounce_skips_rapid_events(self):
        """Second event within debounce window is not queued for delivery."""
        bus = EventBus(node_id="gw")
        await bus.start()
        sub = Subscription(
            subscriber_node="n", callback_action="a",
            event_type_pattern="*",
            debounce_seconds=60.0,  # very long — second emit within window
        )
        await bus.subscribe(sub)

        e1 = Event(event_type="x", source_node="s")
        e2 = Event(event_type="x", source_node="s")
        m1 = await bus.emit(e1)
        m2 = await bus.emit(e2)
        assert m1 == 1
        assert m2 == 0  # debounced
        await bus.stop()


# ===========================================================================
# 9. Max deliveries
# ===========================================================================

class TestMaxDeliveries:

    @pytest.mark.asyncio
    async def test_max_deliveries_one_limits_to_one(self):
        """max_deliveries=1 → auto-unsubscribed after first match."""
        delivered: list[str] = []

        bus = EventBus(node_id="gw")
        await bus.start()
        sub = Subscription(
            subscriber_node="n", callback_action="a",
            event_type_pattern="*",
            max_deliveries=1,
        )
        await bus.subscribe(sub)

        e1 = Event(event_type="x", source_node="s")
        e2 = Event(event_type="x", source_node="s")
        m1 = await bus.emit(e1)
        # Give delivery worker time to process
        await asyncio.sleep(0.1)
        m2 = await bus.emit(e2)

        assert m1 == 1
        assert m2 == 0  # subscription auto-removed after delivery_count >= max_deliveries
        await bus.stop()


# ===========================================================================
# 10. Delivery — no-router standalone mode
# ===========================================================================

class TestDeliveryNoRouter:

    @pytest.mark.asyncio
    async def test_emit_with_no_router_does_not_crash(self):
        """EventBus with gateway_router=None logs but doesn't raise."""
        bus = EventBus(node_id="gw", gateway_router=None)
        await bus.start()
        sub = Subscription(subscriber_node="n", callback_action="a")
        await bus.subscribe(sub)
        event = Event(event_type="test.done", source_node="s")
        matched = await bus.emit(event)
        assert matched == 1

        # Give delivery worker time
        await asyncio.sleep(0.1)
        # Verify subscription still exists (delivery succeeded in no-router mode)
        subs = await bus.get_subscriptions()
        assert len(subs) == 1
        await bus.stop()


# ===========================================================================
# 11. Delivery retry — mock router
# ===========================================================================

class TestDeliveryRetry:

    @pytest.mark.asyncio
    async def test_successful_delivery_increments_count(self):
        """Successful delivery increments delivery_count on subscription."""
        mock_router = MagicMock()
        from runtime.models import SyncActionResponse
        mock_router.route = AsyncMock(return_value=SyncActionResponse(
            task_id="t1", output={}
        ))

        bus = EventBus(
            node_id="gw",
            gateway_router=mock_router,
            delivery_retry_count=1,
        )
        await bus.start()
        sub = Subscription(subscriber_node="n", callback_action="a")
        await bus.subscribe(sub)
        await bus.emit(Event(event_type="x", source_node="s"))

        await asyncio.sleep(0.2)  # let delivery worker run

        subs = await bus.get_subscriptions()
        assert subs[0].delivery_count == 1
        await bus.stop()

    @pytest.mark.asyncio
    async def test_delivery_error_response_triggers_retry(self):
        """ErrorResponse from router triggers retry up to retry_count times."""
        from runtime.models import ErrorResponse as ErrResp
        call_count = [0]

        async def flaky_route(req):
            call_count[0] += 1
            if call_count[0] < 3:
                return ErrResp(error="transient", node_id="n")
            from runtime.models import SyncActionResponse
            return SyncActionResponse(task_id="t", output={})

        mock_router = MagicMock()
        mock_router.route = flaky_route

        bus = EventBus(
            node_id="gw",
            gateway_router=mock_router,
            delivery_retry_count=3,
            delivery_retry_backoff=1.0,
        )
        await bus.start()
        sub = Subscription(subscriber_node="n", callback_action="a")
        await bus.subscribe(sub)
        await bus.emit(Event(event_type="x", source_node="s"))

        # Wait long enough for 2 retries with 1.0s backoff
        await asyncio.sleep(3.0)

        assert call_count[0] == 3
        await bus.stop()


# ===========================================================================
# 12. JSONL persistence
# ===========================================================================

class TestJSONLPersistence:

    @pytest.mark.asyncio
    async def test_events_persisted_to_file(self):
        """Emitted events are written to JSONL file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "events.jsonl")
            bus = EventBus(node_id="gw", persistence_path=path)
            await bus.start()

            e1 = Event(event_type="test.done", source_node="dev")
            e2 = Event(event_type="artifact.written", source_node="dev")
            await bus.emit(e1)
            await bus.emit(e2)

            # Wait for async write
            await asyncio.sleep(0.1)

            lines = Path(path).read_text().strip().splitlines()
            assert len(lines) == 2
            data = [json.loads(l) for l in lines]
            ids = [d["event_id"] for d in data]
            assert e1.event_id in ids
            assert e2.event_id in ids
            await bus.stop()

    @pytest.mark.asyncio
    async def test_load_persisted_events(self):
        """load_persisted_events() loads events from JSONL into in-memory log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "events.jsonl")
            # Pre-write some events
            e1 = Event(event_type="task.started", source_node="pm")
            e2 = Event(event_type="task.completed", source_node="pm")
            with open(path, "w") as f:
                f.write(e1.model_dump_json() + "\n")
                f.write(e2.model_dump_json() + "\n")

            bus = EventBus(node_id="gw", persistence_path=path)
            await bus.start()
            loaded = await bus.load_persisted_events()

            assert loaded == 2
            events = await bus.get_events()
            ids = [e.event_id for e in events]
            assert e1.event_id in ids
            assert e2.event_id in ids
            await bus.stop()

    @pytest.mark.asyncio
    async def test_no_persistence_path_no_file(self):
        """Without persistence_path, no file is created."""
        bus = EventBus(node_id="gw", persistence_path=None)
        await bus.start()
        await bus.emit(Event(event_type="x", source_node="s"))
        await asyncio.sleep(0.05)
        # No exception, no file side effects
        loaded = await bus.load_persisted_events()
        assert loaded == 0
        await bus.stop()


# ===========================================================================
# 13. Config parsing
# ===========================================================================

class TestEventBusConfig:

    def test_default_event_bus_config(self):
        """EventBusConfig has sensible defaults."""
        cfg = EventBusConfig()
        assert cfg.enabled is True
        assert cfg.max_log_size == 10_000
        assert cfg.delivery_timeout_seconds == 10.0
        assert cfg.delivery_retry_count == 3
        assert cfg.delivery_retry_backoff == 2.0
        assert cfg.persistence_path is None

    def test_load_config_has_event_bus_field(self):
        """load_config() produces NodeConfig with event_bus field."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                "node_id: test-node\n"
                "listen: 0.0.0.0:9999\n"
                "event_bus:\n"
                "  enabled: true\n"
                "  max_log_size: 5000\n"
                "  delivery_timeout_seconds: 15\n"
                "  delivery_retry_count: 5\n"
                "  delivery_retry_backoff: 1.5\n"
                "  persistence_path: /tmp/test-events.jsonl\n"
            )
            fname = f.name
        try:
            cfg = load_config(fname)
            assert cfg.event_bus.enabled is True
            assert cfg.event_bus.max_log_size == 5000
            assert cfg.event_bus.delivery_timeout_seconds == 15.0
            assert cfg.event_bus.delivery_retry_count == 5
            assert cfg.event_bus.delivery_retry_backoff == 1.5
            assert cfg.event_bus.persistence_path == "/tmp/test-events.jsonl"
        finally:
            os.unlink(fname)

    def test_load_config_event_bus_defaults_when_absent(self):
        """event_bus section absent → defaults applied."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("node_id: test-node\nlisten: 0.0.0.0:9999\n")
            fname = f.name
        try:
            cfg = load_config(fname)
            assert cfg.event_bus.enabled is True
            assert cfg.event_bus.max_log_size == 10_000
        finally:
            os.unlink(fname)


# ===========================================================================
# 14. ChannelRegistry
# ===========================================================================

class TestChannelRegistry:

    def _make_registry(self, node_ids: list[str]):
        """Build a minimal NodeRegistry mock with given node IDs."""
        mock_reg = MagicMock()
        # Use a real dict so len() and iteration work correctly
        entries = {nid: MagicMock() for nid in node_ids}
        type(mock_reg)._entries = property(lambda self: entries)
        mock_reg.is_trusted = lambda nid: nid in entries
        return mock_reg

    def test_get_channel_id(self):
        reg = self._make_registry(["n1", "n2"])
        cr = ChannelRegistry(reg, "gateway-0")
        assert cr.get_channel_id() == "gateway-0"

    def test_get_members(self):
        reg = self._make_registry(["n1", "n2", "n3"])
        cr = ChannelRegistry(reg, "gw")
        members = cr.get_members()
        assert set(members) == {"n1", "n2", "n3"}

    def test_is_member_true(self):
        reg = self._make_registry(["n1"])
        cr = ChannelRegistry(reg, "gw")
        assert cr.is_member("n1") is True

    def test_is_member_false(self):
        reg = self._make_registry(["n1"])
        cr = ChannelRegistry(reg, "gw")
        assert cr.is_member("unknown") is False

    def test_member_count(self):
        reg = self._make_registry(["a", "b", "c"])
        cr = ChannelRegistry(reg, "gw")
        assert cr.member_count() == 3


# ===========================================================================
# 15. HTTP endpoints
# ===========================================================================

class TestEventBusHTTPEndpoints:
    """Integration-style tests using FastAPI TestClient."""

    def _make_app(self):
        """Build a minimal test app with EventBus enabled."""
        from fastapi.testclient import TestClient
        from runtime.config import NodeConfig, EventBusConfig
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app

        cfg = NodeConfig(
            node_id="test-gw",
            listen="0.0.0.0:9000",
            event_bus=EventBusConfig(enabled=True),
        )
        registry = ActionRegistry()
        app = create_app(config=cfg, registry=registry)
        return TestClient(app, raise_server_exceptions=True)

    def test_post_emit_returns_event_id(self):
        client = self._make_app()
        resp = client.post("/emit", json={
            "event_type": "test.done",
            "source_node": "dev-A",
            "payload": {"result": "pass"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "event_id" in data
        assert data["matched_subscriptions"] == 0

    def test_post_subscribe_returns_sub_id(self):
        client = self._make_app()
        resp = client.post("/subscribe", json={
            "subscriber_node": "pm",
            "callback_action": "handle_event",
            "event_type_pattern": "test.*",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "sub_id" in data
        assert data["event_type_pattern"] == "test.*"

    def test_emit_after_subscribe_matches(self):
        """Subscribe then emit matching event → matched_subscriptions == 1."""
        client = self._make_app()
        client.post("/subscribe", json={
            "subscriber_node": "pm",
            "callback_action": "handle",
            "event_type_pattern": "task.*",
        })
        resp = client.post("/emit", json={
            "event_type": "task.started",
            "source_node": "dev",
        })
        assert resp.json()["matched_subscriptions"] == 1

    def test_get_subscriptions_lists_registered(self):
        client = self._make_app()
        client.post("/subscribe", json={
            "subscriber_node": "n1",
            "callback_action": "a",
            "description": "my sub",
        })
        resp = client.get("/subscriptions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["subscriptions"][0]["subscriber_node"] == "n1"

    def test_delete_subscription(self):
        client = self._make_app()
        sub_resp = client.post("/subscribe", json={
            "subscriber_node": "n1",
            "callback_action": "a",
        })
        sub_id = sub_resp.json()["sub_id"]
        del_resp = client.delete(f"/subscriptions/{sub_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["removed"] is True

        # Confirm removed
        list_resp = client.get("/subscriptions")
        assert list_resp.json()["total"] == 0

    def test_delete_nonexistent_subscription_returns_404(self):
        client = self._make_app()
        resp = client.delete("/subscriptions/does-not-exist")
        assert resp.status_code == 404

    def test_get_events_returns_emitted(self):
        client = self._make_app()
        client.post("/emit", json={"event_type": "x.y", "source_node": "n"})
        resp = client.get("/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    def test_get_events_filter_by_source_node(self):
        client = self._make_app()
        client.post("/emit", json={"event_type": "x", "source_node": "alpha"})
        client.post("/emit", json={"event_type": "x", "source_node": "beta"})
        resp = client.get("/events?source_node=alpha")
        events = resp.json()["events"]
        assert all(e["source_node"] == "alpha" for e in events)

    def test_emit_disabled_returns_503(self):
        """EventBus disabled → /emit returns 503."""
        from fastapi.testclient import TestClient
        from runtime.config import NodeConfig, EventBusConfig
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app

        cfg = NodeConfig(
            node_id="test-gw",
            listen="0.0.0.0:9000",
            event_bus=EventBusConfig(enabled=False),
        )
        registry = ActionRegistry()
        app = create_app(config=cfg, registry=registry)
        client = TestClient(app)
        resp = client.post("/emit", json={"event_type": "x", "source_node": "n"})
        assert resp.status_code == 503


# ===========================================================================
# 16. Health endpoint — v6 event_bus section
# ===========================================================================

class TestHealthEndpointV6:

    def test_health_includes_event_bus_section(self):
        from fastapi.testclient import TestClient
        from runtime.config import NodeConfig, EventBusConfig
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app

        cfg = NodeConfig(
            node_id="gw",
            listen="0.0.0.0:9000",
            event_bus=EventBusConfig(enabled=True),
        )
        registry = ActionRegistry()
        app = create_app(config=cfg, registry=registry)
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "event_bus" in data
        assert data["event_bus"]["enabled"] is True
        assert "subscriptions" in data["event_bus"]
        assert "events_in_log" in data["event_bus"]

    def test_health_event_bus_disabled(self):
        from fastapi.testclient import TestClient
        from runtime.config import NodeConfig, EventBusConfig
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app

        cfg = NodeConfig(
            node_id="gw",
            listen="0.0.0.0:9000",
            event_bus=EventBusConfig(enabled=False),
        )
        registry = ActionRegistry()
        app = create_app(config=cfg, registry=registry)
        client = TestClient(app)
        resp = client.get("/health")
        data = resp.json()
        assert data["event_bus"]["enabled"] is False
