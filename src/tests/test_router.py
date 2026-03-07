"""Tests for runtime.router."""

from __future__ import annotations

import pytest

from runtime.action_executor import ActionExecutor
from runtime.action_loader import load_actions
from runtime.config import NodeConfig
from runtime.job_manager import JobManager
from runtime.models import (
    ActionPayload,
    ActionRequest,
    AsyncActionResponse,
    ErrorResponse,
    SyncActionResponse,
    TraceInfo,
)
from runtime.resolver import NodeResolver
from runtime.router import RequestRouter


def _make_config(**kwargs) -> NodeConfig:
    defaults = {
        "node_id": "node-0",
        "listen": "0.0.0.0:8080",
        "nodes": {"node-0": "http://127.0.0.1:8080"},
        "max_hop": 3,
        "cache_ttl_seconds": 300,
    }
    defaults.update(kwargs)
    return NodeConfig(**defaults)


def _make_router(config: NodeConfig | None = None) -> RequestRouter:
    config = config or _make_config()
    registry = load_actions("seed/actions")
    job_manager = JobManager()
    resolver = NodeResolver(config)
    executor = ActionExecutor(registry, job_manager, config.node_id, schema_validator=None)
    return RequestRouter(config, resolver, executor)


def _make_request(
    target: str = "node-0",
    action: str = "read_file",
    params: dict | None = None,
    hop_count: int = 0,
    route_path: list[str] | None = None,
) -> ActionRequest:
    return ActionRequest(
        target_node_id=target,
        task_id="test-task-001",
        trace=TraceInfo(hop_count=hop_count, route_path=route_path or []),
        payload=ActionPayload(action=action, params=params or {}),
    )


@pytest.mark.asyncio
async def test_local_execution_sync() -> None:
    """Local sync action (write_file) returns SyncActionResponse."""
    import tempfile, os

    router = _make_router()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test.txt")
        req = _make_request(
            action="write_file",
            params={"path": path, "content": "hello mesh"},
        )
        result = await router.route(req)
        assert isinstance(result, SyncActionResponse)
        assert result.output["success"] is True


@pytest.mark.asyncio
async def test_action_not_found() -> None:
    """Unknown action returns ErrorResponse."""
    router = _make_router()
    req = _make_request(action="nonexistent_action")
    result = await router.route(req)
    assert isinstance(result, ErrorResponse)
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_hop_count_exceeded() -> None:
    """Requests exceeding max_hop are rejected."""
    router = _make_router()
    req = _make_request(hop_count=10)
    result = await router.route(req)
    assert isinstance(result, ErrorResponse)
    assert "hop" in result.error.lower()


@pytest.mark.asyncio
async def test_loop_detection() -> None:
    """Requests with self in route_path are rejected."""
    router = _make_router()
    req = _make_request(route_path=["node-0"])
    result = await router.route(req)
    assert isinstance(result, ErrorResponse)
    assert "loop" in result.error.lower()


@pytest.mark.asyncio
async def test_node_not_found() -> None:
    """Requests to unknown nodes return NODE_NOT_FOUND error."""
    router = _make_router()
    req = _make_request(target="node-99")
    result = await router.route(req)
    assert isinstance(result, ErrorResponse)
    assert "NOT_FOUND" in result.error
