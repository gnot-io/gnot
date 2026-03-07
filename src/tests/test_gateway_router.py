"""Tests for GatewayRouter — push/pull routing logic (v5.3)."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.gateway_router import GatewayRouter
from runtime.job_manager import JobManager
from runtime.job_queue import JobQueue
from runtime.models import (
    ActionRequest, ActionPayload, TraceInfo,
    AsyncActionResponse, SyncActionResponse, ErrorResponse, NodeStatus,
)
from runtime.node_registry import NodeRegistry


def make_request(target="node-1", action="read_file", params=None):
    return ActionRequest(
        target_node_id=target,
        task_id="task-test-001",
        trace=TraceInfo(),
        payload=ActionPayload(action=action, params=params or {"path": "/tmp/x"}),
    )


def make_router(trusted=None, node_id="node-0", executor=None):
    config = MagicMock()
    config.node_id = node_id
    config.max_hop = 10
    config.trusted_nodes = trusted or ["node-1", "node-2"]
    config.heartbeat_timeout_seconds = 30
    config.ping_timeout_seconds = 3.0

    from runtime.resolver import NodeNotFoundError
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(side_effect=NodeNotFoundError("unknown"))

    exec_ = executor or AsyncMock()
    job_manager = JobManager()
    node_registry = NodeRegistry(trusted_node_ids=config.trusted_nodes)
    job_queue = JobQueue()

    return GatewayRouter(
        config=config,
        resolver=resolver,
        executor=exec_,
        job_manager=job_manager,
        node_registry=node_registry,
        job_queue=job_queue,
    ), node_registry, job_queue, job_manager


@pytest.mark.asyncio
async def test_local_execution():
    """Action targeting self → executes locally."""
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value=SyncActionResponse(
        task_id="task-test-001", output={"success": True}
    ))
    router, _, _, _ = make_router(node_id="node-0", executor=executor)
    req = make_request(target="node-0")
    result = await router.route(req)
    assert isinstance(result, SyncActionResponse)
    executor.execute.assert_called_once()


@pytest.mark.asyncio
async def test_untrusted_node_rejected():
    """Target not in trusted list → immediate ErrorResponse."""
    router, _, _, _ = make_router(trusted=["node-1"])
    req = make_request(target="node-99")
    result = await router.route(req)
    assert isinstance(result, ErrorResponse)
    assert "UNTRUSTED_NODE" in result.error


@pytest.mark.asyncio
async def test_hop_count_exceeded():
    router, _, _, _ = make_router()
    req = make_request()
    req.trace.hop_count = 999
    result = await router.route(req)
    assert isinstance(result, ErrorResponse)
    assert "hop" in result.error.lower()


@pytest.mark.asyncio
async def test_loop_detection():
    router, _, _, _ = make_router(node_id="node-0")
    req = make_request()
    req.trace.route_path = ["node-0"]
    result = await router.route(req)
    assert isinstance(result, ErrorResponse)
    assert "loop" in result.error.lower()


@pytest.mark.asyncio
async def test_push_mode_reachable_sync():
    """Worker reachable, action returns sync result → passed through."""
    router, node_registry, _, _ = make_router()
    await node_registry.register("node-1", address="http://10.0.0.1:8080")

    sync_payload = {"task_id": "task-test-001", "status": "completed", "output": {"exit_code": 0}}

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = sync_payload
    mock_resp.raise_for_status = MagicMock()

    mock_ping = MagicMock()
    mock_ping.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    # ping returns 200, action returns sync payload
    mock_client.get = AsyncMock(return_value=mock_ping)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("runtime.node_registry.httpx.AsyncClient", return_value=mock_client), \
         patch("runtime.gateway_router.httpx.AsyncClient", return_value=mock_client):
        result = await router.route(make_request(target="node-1"))

    assert isinstance(result, SyncActionResponse)
    assert result.output["exit_code"] == 0


@pytest.mark.asyncio
async def test_push_mode_reachable_async():
    """Worker reachable, action returns job_id → registered as push job."""
    router, node_registry, job_queue, _ = make_router()
    await node_registry.register("node-1", address="http://10.0.0.1:8080")

    async_payload = {
        "task_id": "task-test-001",
        "job_id": "node1-job-abc",
        "status": "accepted",
        "estimated_completion_seconds": 30,
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = async_payload
    mock_resp.raise_for_status = MagicMock()

    mock_ping = MagicMock()
    mock_ping.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_ping)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("runtime.node_registry.httpx.AsyncClient", return_value=mock_client), \
         patch("runtime.gateway_router.httpx.AsyncClient", return_value=mock_client):
        result = await router.route(make_request(target="node-1"))

    assert isinstance(result, AsyncActionResponse)
    assert result.job_id == "node1-job-abc"
    # push job registered in job_queue
    route = await job_queue.get_route("node1-job-abc")
    assert route is not None


@pytest.mark.asyncio
async def test_pull_mode_no_address():
    """Node trusted but no address → enqueue immediately (pull mode)."""
    router, _, job_queue, job_manager = make_router()
    # node-1 has no address registered

    result = await router.route(make_request(target="node-1"))

    assert isinstance(result, AsyncActionResponse)
    jobs = await job_queue.poll("node-1")
    assert len(jobs) == 1
    assert jobs[0].action == "read_file"


@pytest.mark.asyncio
async def test_pull_mode_unreachable():
    """Node has address but ping fails → fallback to pull mode."""
    router, node_registry, job_queue, _ = make_router()
    await node_registry.register("node-1", address="http://10.0.0.1:8080")

    import httpx
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with patch("runtime.node_registry.httpx.AsyncClient", return_value=mock_client):
        result = await router.route(make_request(target="node-1"))

    assert isinstance(result, AsyncActionResponse)
    jobs = await job_queue.poll("node-1")
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_push_fail_fallback_to_pull():
    """Ping succeeds but proxy POST fails → fallback to pull mode transparently."""
    router, node_registry, job_queue, _ = make_router()
    await node_registry.register("node-1", address="http://10.0.0.1:8080")

    import httpx
    mock_ping = MagicMock()
    mock_ping.status_code = 200

    mock_client_ping = AsyncMock()
    mock_client_ping.__aenter__ = AsyncMock(return_value=mock_client_ping)
    mock_client_ping.__aexit__ = AsyncMock(return_value=None)
    mock_client_ping.get = AsyncMock(return_value=mock_ping)
    mock_client_ping.post = AsyncMock(side_effect=httpx.ConnectError("gateway timeout"))

    with patch("runtime.node_registry.httpx.AsyncClient", return_value=mock_client_ping), \
         patch("runtime.gateway_router.httpx.AsyncClient", return_value=mock_client_ping):
        result = await router.route(make_request(target="node-1"))

    # Should have silently fallen back to pull
    assert isinstance(result, AsyncActionResponse)
    jobs = await job_queue.poll("node-1")
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_route_result_local_job():
    """route_result for a job owned by local JobManager."""
    router, _, _, job_manager = make_router()
    job = await job_manager.create_job("node-0", "task-001")
    result = await router.route_result(job.job_id)
    assert result.job_id == job.job_id


@pytest.mark.asyncio
async def test_route_result_unknown_job():
    router, _, _, _ = make_router()
    result = await router.route_result("nonexistent-job-id")
    assert isinstance(result, ErrorResponse)
    assert "JOB_NOT_FOUND" in result.error
