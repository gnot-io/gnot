"""Tests for runtime.resolver."""

from __future__ import annotations

import pytest

from runtime.config import NodeConfig
from runtime.resolver import LOCAL_SENTINEL, NodeNotFoundError, NodeResolver


def _make_config(**kwargs) -> NodeConfig:
    defaults = {
        "node_id": "node-0",
        "listen": "0.0.0.0:8080",
        "nodes": {"node-0": "http://127.0.0.1:8080", "node-1": "http://10.0.0.1:8080"},
        "default_resolver": "node-0",
        "max_hop": 10,
        "cache_ttl_seconds": 300,
    }
    defaults.update(kwargs)
    return NodeConfig(**defaults)


@pytest.mark.asyncio
async def test_resolve_local() -> None:
    """Resolving self returns LOCAL sentinel."""
    config = _make_config()
    resolver = NodeResolver(config)
    result = await resolver.resolve("node-0")
    assert result == LOCAL_SENTINEL


@pytest.mark.asyncio
async def test_resolve_from_config() -> None:
    """Resolving a known node returns its address."""
    config = _make_config()
    resolver = NodeResolver(config)
    result = await resolver.resolve("node-1")
    assert result == "http://10.0.0.1:8080"


@pytest.mark.asyncio
async def test_resolve_not_found() -> None:
    """Resolving an unknown node raises NodeNotFoundError."""
    config = _make_config(nodes={"node-0": "http://127.0.0.1:8080"})
    resolver = NodeResolver(config)
    with pytest.raises(NodeNotFoundError):
        await resolver.resolve("node-99")


@pytest.mark.asyncio
async def test_resolve_no_default_resolver() -> None:
    """With no default_resolver, unknown nodes raise error."""
    config = _make_config(default_resolver=None)
    resolver = NodeResolver(config)
    with pytest.raises(NodeNotFoundError):
        await resolver.resolve("node-99")
