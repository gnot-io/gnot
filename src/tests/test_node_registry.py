"""Tests for NodeRegistry (v5.3)."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.node_registry import NodeRegistry
from runtime.models import NodeStatus


@pytest.fixture
def registry():
    return NodeRegistry(
        trusted_node_ids=["node-1", "node-2"],
        heartbeat_timeout_seconds=30,
        ping_timeout_seconds=3.0,
    )


def test_static_nodes_are_trusted(registry):
    assert registry.is_trusted("node-1")
    assert registry.is_trusted("node-2")
    assert not registry.is_trusted("node-99")


@pytest.mark.asyncio
async def test_dynamic_registration(registry):
    await registry.register("node-3", address="http://10.0.0.3:8080")
    assert registry.is_trusted("node-3")
    assert await registry.get_address("node-3") == "http://10.0.0.3:8080"


@pytest.mark.asyncio
async def test_re_registration_updates_address(registry):
    await registry.register("node-1", address="http://10.0.0.1:9000")
    assert await registry.get_address("node-1") == "http://10.0.0.1:9000"


@pytest.mark.asyncio
async def test_heartbeat_marks_online(registry):
    await registry.register("node-1", address="http://10.0.0.1:8080")
    result = await registry.heartbeat("node-1")
    assert result is True
    assert await registry.get_status("node-1") == NodeStatus.ONLINE


@pytest.mark.asyncio
async def test_heartbeat_unknown_node_rejected(registry):
    result = await registry.heartbeat("ghost-node")
    assert result is False


@pytest.mark.asyncio
async def test_stale_heartbeat_marks_unreachable():
    reg = NodeRegistry(trusted_node_ids=["node-1"], heartbeat_timeout_seconds=1)
    await reg.register("node-1", address="http://10.0.0.1:8080")
    await reg.heartbeat("node-1")
    async with reg._lock:
        reg._entries["node-1"].last_heartbeat = time.time() - 60
    marked = await reg.mark_stale_nodes_unreachable()
    assert "node-1" in marked
    assert await reg.get_status("node-1") == NodeStatus.UNREACHABLE


@pytest.mark.asyncio
async def test_ping_reachable_node(registry):
    await registry.register("node-1", address="http://10.0.0.1:8080")
    import httpx

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("runtime.node_registry.httpx.AsyncClient", return_value=mock_client):
        reachable = await registry.ping("node-1")

    assert reachable is True
    assert await registry.get_status("node-1") == NodeStatus.ONLINE


@pytest.mark.asyncio
async def test_ping_unreachable_node(registry):
    await registry.register("node-1", address="http://10.0.0.1:8080")
    import httpx

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with patch("runtime.node_registry.httpx.AsyncClient", return_value=mock_client):
        reachable = await registry.ping("node-1")

    assert reachable is False
    assert await registry.get_status("node-1") == NodeStatus.UNREACHABLE


@pytest.mark.asyncio
async def test_ping_no_address_returns_false(registry):
    reachable = await registry.ping("node-2")  # node-2 has no address set
    assert reachable is False


@pytest.mark.asyncio
async def test_list_nodes(registry):
    nodes = await registry.list_nodes()
    node_ids = [n.node_id for n in nodes]
    assert "node-1" in node_ids
    assert "node-2" in node_ids
