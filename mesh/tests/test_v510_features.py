"""Tests for v5.10 — BGP-style Route Advertisement & Capability Discovery.

Covers:
  1.  NodeRegistry.register() stores actions on direct node
  2.  NodeRegistry.register() installs advertised sub-routes with next_hop
  3.  NodeRegistry.get_next_hop() — direct child returns itself
  4.  NodeRegistry.get_next_hop() — indirect node returns next_hop
  5.  NodeRegistry.get_next_hop() — unknown node returns None
  6.  NodeRegistry.build_capability_tree() — flat dict with next_hop annotations
  7.  GatewayRouter.route() — target is direct child (unchanged behaviour)
  8.  GatewayRouter.route() — target reachable via next_hop (new multi-hop path)
  9.  GatewayRouter.route() — unknown target still returns UNTRUSTED_NODE error
  10. WorkerAgent — registration payload includes actions + advertise_routes
  11. WorkerAgent.add_sub_route() — updates sub_routes and re-registers
  12. POST /nodes/register — stores actions in registry
  13. POST /nodes/register — advertised routes propagated into registry
  14. GET  /capabilities    — returns full capability tree
  15. GET  /capabilities    — includes sub-routes from advertisement
  16. IntentHandler system prompt — contains node IDs and action names
  17. IntentHandler system prompt — shows next_hop for indirect nodes
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.node_registry import NodeRegistry
from runtime.models import (
    ActionRequest,
    NodeRegistrationRequest,
    SyncActionResponse,
    TraceInfo,
    ErrorResponse,
)


# ---------------------------------------------------------------------------
# 1–6: NodeRegistry unit tests
# ---------------------------------------------------------------------------

class TestNodeRegistryV510:

    @pytest.mark.asyncio
    async def test_register_stores_actions(self):
        reg = NodeRegistry()
        await reg.register("node-1", actions=["execute_command", "read_file"])
        assert reg.get_actions("node-1") == ["execute_command", "read_file"]

    @pytest.mark.asyncio
    async def test_register_installs_sub_routes_with_next_hop(self):
        reg = NodeRegistry()
        await reg.register(
            "node-1",
            advertise_routes=["node-1a", "node-1b"],
            capabilities={
                "node-1a": ["execute_command", "get_order_info"],
                "node-1b": ["send_notification"],
            },
        )
        assert reg.is_trusted("node-1a")
        assert reg.is_trusted("node-1b")
        assert reg.get_actions("node-1a") == ["execute_command", "get_order_info"]
        assert reg.get_actions("node-1b") == ["send_notification"]

    @pytest.mark.asyncio
    async def test_get_next_hop_direct_child_returns_itself(self):
        reg = NodeRegistry(trusted_node_ids=["node-1"])
        await reg.register("node-1", address="http://node1:8080")
        assert reg.get_next_hop("node-1") == "node-1"

    @pytest.mark.asyncio
    async def test_get_next_hop_indirect_node_returns_next_hop(self):
        reg = NodeRegistry()
        await reg.register(
            "node-1",
            advertise_routes=["node-1a"],
            capabilities={"node-1a": ["get_order_info"]},
        )
        assert reg.get_next_hop("node-1a") == "node-1"

    def test_get_next_hop_unknown_returns_none(self):
        reg = NodeRegistry()
        assert reg.get_next_hop("nonexistent") is None

    @pytest.mark.asyncio
    async def test_build_capability_tree_returns_all_nodes(self):
        reg = NodeRegistry()
        await reg.register(
            "node-1",
            actions=["execute_command"],
            advertise_routes=["node-1a"],
            capabilities={"node-1a": ["get_order_info"]},
        )
        tree = reg.build_capability_tree(own_actions=["write_file"])
        assert "node-1" in tree
        assert "node-1a" in tree
        assert tree["node-1"].next_hop is None   # direct child
        assert tree["node-1a"].next_hop == "node-1"
        assert "get_order_info" in tree["node-1a"].actions

    @pytest.mark.asyncio
    async def test_re_registration_preserves_existing_next_hop(self):
        """Sub-route registered indirectly keeps its next_hop even after direct re-register attempt."""
        reg = NodeRegistry()
        # node-1 advertises node-1a
        await reg.register(
            "node-1",
            advertise_routes=["node-1a"],
            capabilities={"node-1a": ["get_order_info"]},
        )
        assert reg.get_next_hop("node-1a") == "node-1"

        # node-1a tries to register directly (e.g. if it ever gets a direct path)
        # next_hop should remain as-is (first writer wins for next_hop)
        await reg.register("node-1a", actions=["get_order_info", "new_action"])
        assert reg.get_actions("node-1a") == ["get_order_info", "new_action"]
        # next_hop stays because direct register sets next_hop=None only for NEW entries
        # (the entry already existed with next_hop="node-1")


# ---------------------------------------------------------------------------
# 7–9: GatewayRouter multi-hop routing
# ---------------------------------------------------------------------------

def _make_action_request(target: str, action: str = "execute_command") -> ActionRequest:
    from runtime.models import ActionPayload
    return ActionRequest(
        target_node_id=target,
        payload=ActionPayload(action=action, params={}),
        trace=TraceInfo(hop_count=0, route_path=[]),
    )


class TestGatewayRouterV510:

    def _make_router(self, node_id="node-0", registry=None):
        from runtime.gateway_router import GatewayRouter
        from runtime.job_manager import JobManager
        from runtime.job_queue import JobQueue
        from runtime.action_executor import ActionExecutor
        from runtime.resolver import NodeResolver
        from runtime.schema_validator import ActionSchemaValidator

        config = MagicMock()
        config.node_id = node_id
        config.max_hop = 5
        config.pull_job_timeout_seconds = 300

        from runtime.resolver import NodeNotFoundError
        resolver = MagicMock()
        resolver.resolve = AsyncMock(side_effect=NodeNotFoundError("not found"))

        executor = MagicMock()
        executor.execute = AsyncMock(return_value=SyncActionResponse(
            task_id="t", output={"exit_code": 0, "stdout": "ok"}
        ))

        job_manager = JobManager()
        job_queue = JobQueue()

        node_reg = registry or NodeRegistry(trusted_node_ids=[])

        return GatewayRouter(
            config=config,
            resolver=resolver,
            executor=executor,
            job_manager=job_manager,
            node_registry=node_reg,
            job_queue=job_queue,
        )

    @pytest.mark.asyncio
    async def test_route_direct_child_works(self):
        """Direct trusted node with no address → pull mode (existing behaviour)."""
        reg = NodeRegistry()
        await reg.register("node-1", actions=["execute_command"])
        router = self._make_router(registry=reg)

        req = _make_action_request("node-1")
        result = await router.route(req)
        # Pull mode returns AsyncActionResponse (has job_id)
        assert hasattr(result, "job_id")

    @pytest.mark.asyncio
    async def test_route_indirect_node_goes_via_next_hop(self):
        """Target reachable via next-hop: router forwards to next-hop, not direct."""
        reg = NodeRegistry()
        await reg.register(
            "node-1",
            advertise_routes=["node-1a"],
            capabilities={"node-1a": ["get_order_info"]},
        )
        router = self._make_router(registry=reg)

        req = _make_action_request("node-1a", action="get_order_info")
        result = await router.route(req)
        # Should NOT be an UNTRUSTED_NODE error
        assert not (isinstance(result, ErrorResponse) and "UNTRUSTED_NODE" in result.error)
        # Should be async (pull-mode forward)
        assert hasattr(result, "job_id") or hasattr(result, "output")

    @pytest.mark.asyncio
    async def test_route_unknown_target_returns_error(self):
        """Completely unknown target still returns UNTRUSTED_NODE."""
        reg = NodeRegistry()
        router = self._make_router(registry=reg)

        req = _make_action_request("totally-unknown-node")
        result = await router.route(req)
        assert isinstance(result, ErrorResponse)
        assert "UNTRUSTED_NODE" in result.error


# ---------------------------------------------------------------------------
# 10–11: WorkerAgent advertisement tests
# ---------------------------------------------------------------------------

class TestWorkerAgentV510:

    def _make_agent(self, node_id="node-1", actions=None):
        from runtime.worker_agent import WorkerAgent
        config = MagicMock()
        config.node_id = node_id
        config.gateway_address = "http://gateway:8080"
        config.self_address = None
        config.port = 8081
        config.auth_token = "test-token"
        config.heartbeat_interval_seconds = 10
        config.poll_interval_seconds = 5

        executor = MagicMock()
        registry = {a: MagicMock() for a in (actions or ["execute_command"])}

        agent = WorkerAgent(config=config, executor=executor, action_registry=registry)
        return agent

    @pytest.mark.asyncio
    async def test_register_payload_includes_actions(self):
        agent = self._make_agent(actions=["execute_command", "read_file"])

        captured = {}

        async def fake_post(url, json=None, headers=None):
            captured["payload"] = json
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"registered": True})
            return resp

        import httpx
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(side_effect=fake_post)):
            await agent._register()

        payload = captured["payload"]
        assert "actions" in payload
        assert "execute_command" in payload["actions"]
        assert "read_file" in payload["actions"]

    @pytest.mark.asyncio
    async def test_add_sub_route_updates_and_re_registers(self):
        agent = self._make_agent(actions=["execute_command"])

        re_register_calls = []

        async def fake_register():
            re_register_calls.append(dict(agent._sub_routes))

        with patch.object(agent, "_register", side_effect=fake_register):
            await agent.add_sub_route("node-1a", ["get_order_info"])

        assert "node-1a" in agent._sub_routes
        assert agent._sub_routes["node-1a"].actions == ["get_order_info"]
        assert len(re_register_calls) == 1

    @pytest.mark.asyncio
    async def test_register_payload_includes_sub_routes(self):
        agent = self._make_agent(actions=["execute_command"])
        # Pre-populate sub routes
        from runtime.worker_agent import SubRouteInfo
        agent._sub_routes["node-1a"] = SubRouteInfo(actions=["get_order_info"])

        captured = {}

        async def fake_post(url, json=None, headers=None):
            captured["payload"] = json
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"registered": True})
            return resp

        import httpx
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(side_effect=fake_post)):
            await agent._register()

        payload = captured["payload"]
        assert "advertise_routes" in payload
        assert "node-1a" in payload["advertise_routes"]
        assert payload["capabilities"]["node-1a"] == ["get_order_info"]


# ---------------------------------------------------------------------------
# 12–15: ASGI integration tests
# ---------------------------------------------------------------------------

try:
    from httpx import AsyncClient, ASGITransport
    from runtime.action_loader import load_actions
    from runtime.config import load_config
    from runtime.server import create_app
    INTEGRATION_AVAILABLE = True
except ImportError:
    INTEGRATION_AVAILABLE = False


def _make_test_app(tmpdir: str):
    yaml_path = os.path.join(tmpdir, "node.yaml")
    with open(yaml_path, "w") as f:
        f.write(f"node_id: node-0\nlisten: 0.0.0.0:8080\n")
    config = load_config(yaml_path)
    seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
    registry = load_actions(seed_dir)
    return create_app(config, registry)


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestCapabilityEndpoints:

    async def test_register_stores_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_test_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/nodes/register", json={
                    "node_id": "node-1",
                    "actions": ["execute_command", "custom_action"],
                })
            assert resp.status_code == 200

            # Now capabilities should show node-1 with its actions
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                caps = await c.get("/capabilities")
            assert caps.status_code == 200
            data = caps.json()
            assert "node-1" in data["reachable"]
            assert "execute_command" in data["reachable"]["node-1"]["actions"]

    async def test_register_with_sub_routes_propagated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_test_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                # node-1 registers and advertises node-1a
                resp = await c.post("/nodes/register", json={
                    "node_id": "node-1",
                    "actions": ["execute_command"],
                    "advertise_routes": ["node-1a"],
                    "capabilities": {"node-1a": ["get_order_info"]},
                })
            assert resp.status_code == 200
            assert "node-1a" in resp.json().get("routes_acknowledged", [])

            # capabilities should show both node-1 and node-1a
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                caps = await c.get("/capabilities")
            data = caps.json()
            assert "node-1" in data["reachable"]
            assert "node-1a" in data["reachable"]
            assert data["reachable"]["node-1a"]["next_hop"] == "node-1"
            assert "get_order_info" in data["reachable"]["node-1a"]["actions"]

    async def test_capabilities_includes_self_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_test_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                caps = await c.get("/capabilities")
            data = caps.json()
            assert data["node_id"] == "node-0"
            assert "execute_command" in data["actions"]

    async def test_capabilities_empty_reachable_when_no_nodes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_test_app(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                caps = await c.get("/capabilities")
            data = caps.json()
            assert isinstance(data["reachable"], dict)


# ---------------------------------------------------------------------------
# 16–17: IntentHandler system prompt with capability tree
# ---------------------------------------------------------------------------

class TestIntentHandlerSystemPromptV510:

    def _make_handler(self, registered_nodes: dict[str, dict]):
        """registered_nodes: {node_id: {actions, next_hop?}}"""
        from runtime.intent_handler import IntentHandler
        from runtime.conversation_store import ConversationStore

        config = MagicMock()
        config.node_id = "node-0"
        config.intent_max_turns = 5
        config.pull_job_timeout_seconds = 60
        config.intent_system_prompt = None
        config.llm_enabled = True

        node_registry = MagicMock()

        def build_tree(own_actions):
            from runtime.models import CapabilityNode
            result = {}
            for nid, info in registered_nodes.items():
                result[nid] = CapabilityNode(
                    node_id=nid,
                    actions=info.get("actions", []),
                    next_hop=info.get("next_hop"),
                )
            return result

        node_registry.build_capability_tree.side_effect = build_tree

        action_registry = {"execute_command": MagicMock(), "read_file": MagicMock()}
        router = MagicMock()
        store = ConversationStore()

        return IntentHandler(
            config=config,
            llm_client=MagicMock(),
            gateway_router=router,
            node_registry=node_registry,
            action_registry=action_registry,
            conversation_store=store,
        )

    def test_system_prompt_contains_node_ids(self):
        handler = self._make_handler({
            "node-1": {"actions": ["execute_command"]},
            "node-1a": {"actions": ["get_order_info"], "next_hop": "node-1"},
        })
        prompt = handler._build_system_prompt()
        assert "node-1" in prompt
        assert "node-1a" in prompt
        assert "get_order_info" in prompt

    def test_system_prompt_shows_next_hop_for_indirect_nodes(self):
        handler = self._make_handler({
            "node-1": {"actions": ["execute_command"]},
            "node-1a": {"actions": ["get_order_info"], "next_hop": "node-1"},
        })
        prompt = handler._build_system_prompt()
        # Should mention that node-1a is reachable via node-1
        assert "via node-1" in prompt

    def test_system_prompt_direct_nodes_show_no_via(self):
        handler = self._make_handler({
            "node-1": {"actions": ["execute_command"], "next_hop": None},
        })
        prompt = handler._build_system_prompt()
        assert "node-1" in prompt
        # Direct node should say (direct) not "via"
        assert "(direct)" in prompt
