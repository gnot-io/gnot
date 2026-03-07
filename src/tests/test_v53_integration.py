"""Integration tests for v5.3 push/pull endpoints via ASGI transport."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.config import NodeConfig
from runtime.server import create_app
from runtime.action_loader import ActionRegistry


def make_config(trusted_nodes=None):
    return NodeConfig(
        node_id="node-0",
        listen="0.0.0.0:8080",
        nodes={"node-0": "http://127.0.0.1:8080"},
        auth_token="test-token",
        trusted_nodes=trusted_nodes or ["node-1"],
        heartbeat_timeout_seconds=30,
        ping_timeout_seconds=3.0,
    )


HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def app():
    config = make_config()
    registry: ActionRegistry = {}

    # Mock a simple sync action
    mock_module = MagicMock()
    mock_module.run = lambda params, ctx: {"result": "ok"}
    mock_module.ASYNC = False
    registry["echo"] = mock_module

    return create_app(config, registry)


@pytest.mark.asyncio
async def test_ping_no_auth(app):
    """GET /ping is always exempt from auth."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/ping")
    assert resp.status_code == 200
    assert resp.json()["pong"] is True


@pytest.mark.asyncio
async def test_register_node(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/nodes/register",
            json={"node_id": "node-1", "address": "http://10.0.0.1:8081"},
            headers=HEADERS,
        )
    assert resp.status_code == 200
    assert resp.json()["registered"] is True


@pytest.mark.asyncio
async def test_heartbeat_trusted_node(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Register first
        await client.post("/nodes/register", json={"node_id": "node-1"}, headers=HEADERS)
        resp = await client.post(
            "/nodes/node-1/heartbeat",
            json={"node_id": "node-1"},
            headers=HEADERS,
        )
    assert resp.status_code == 200
    assert resp.json()["acknowledged"] is True


@pytest.mark.asyncio
async def test_heartbeat_unknown_node_rejected(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/nodes/ghost-node/heartbeat",
            json={"node_id": "ghost-node"},
            headers=HEADERS,
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_nodes(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/nodes", headers=HEADERS)
    assert resp.status_code == 200
    node_ids = [n["node_id"] for n in resp.json()["nodes"]]
    assert "node-1" in node_ids


@pytest.mark.asyncio
async def test_pull_mode_action_enqueues(app):
    """POST /action targeting trusted but unreachable node → job queued."""
    import httpx

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Register node-1 with an address (so we attempt ping)
        await client.post(
            "/nodes/register",
            json={"node_id": "node-1", "address": "http://10.0.0.1:8081"},
            headers=HEADERS,
        )
        with patch("runtime.node_registry.httpx.AsyncClient", return_value=mock_client):
            resp = await client.post(
                "/action",
                json={
                    "target_node_id": "node-1",
                    "task_id": "task-integration-001",
                    "payload": {"action": "echo", "params": {}},
                },
                headers=HEADERS,
            )

    assert resp.status_code == 202
    data = resp.json()
    assert "job_id" in data
    assert data["status"] == "accepted"


@pytest.mark.asyncio
async def test_poll_and_claim_flow(app):
    """Full pull flow: enqueue → poll → claim → report result → GET /result."""
    import httpx

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/nodes/register",
            json={"node_id": "node-1", "address": "http://10.0.0.1:8081"},
            headers=HEADERS,
        )

        # 1. Enqueue via /action (ping will fail → pull mode)
        with patch("runtime.node_registry.httpx.AsyncClient", return_value=mock_client):
            action_resp = await client.post(
                "/action",
                json={
                    "target_node_id": "node-1",
                    "task_id": "task-flow-001",
                    "payload": {"action": "echo", "params": {}},
                },
                headers=HEADERS,
            )
        assert action_resp.status_code == 202
        job_id = action_resp.json()["job_id"]

        # 2. Worker polls
        poll_resp = await client.get("/jobs/poll", params={"node_id": "node-1"}, headers=HEADERS)
        assert poll_resp.status_code == 200
        jobs = poll_resp.json()["jobs"]
        assert any(j["job_id"] == job_id for j in jobs)

        # 3. Worker claims
        claim_resp = await client.post(
            f"/jobs/{job_id}/claim",
            json={"node_id": "node-1"},
            headers=HEADERS,
        )
        assert claim_resp.status_code == 200
        assert claim_resp.json()["claimed"] is True

        # 4. Worker reports result
        result_resp = await client.post(
            f"/jobs/{job_id}/result",
            json={"node_id": "node-1", "status": "completed", "output": {"answer": 42}},
            headers=HEADERS,
        )
        assert result_resp.status_code == 200

        # 5. LLM polls /result
        poll_result_resp = await client.get(f"/result/{job_id}", headers=HEADERS)
        assert poll_result_resp.status_code == 200
        data = poll_result_resp.json()
        assert data["status"] == "completed"
        assert data["output"]["answer"] == 42


@pytest.mark.asyncio
async def test_untrusted_node_action_rejected(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/action",
            json={
                "target_node_id": "evil-node",
                "task_id": "task-x",
                "payload": {"action": "execute_command", "params": {"command": "rm -rf /"}},
            },
            headers=HEADERS,
        )
    assert resp.status_code == 400
    assert "UNTRUSTED_NODE" in resp.json()["error"]


@pytest.mark.asyncio
async def test_poll_untrusted_node_rejected(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/jobs/poll", params={"node_id": "evil-node"}, headers=HEADERS)
    assert resp.status_code == 403
