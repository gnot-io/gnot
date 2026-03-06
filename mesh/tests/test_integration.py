"""Integration tests — multi-node mesh communication.

These tests spin up real FastAPI servers using ASGI transport to
verify cross-node routing, proxy forwarding, and auth propagation
without requiring actual network ports.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from runtime.action_loader import load_actions
from runtime.config import NodeConfig
from runtime.schema_validator import load_schemas
from runtime.server import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_test_node(
    tmpdir: str,
    node_id: str,
    port: int,
    nodes: dict[str, str],
    auth_token: str | None = None,
) -> tuple:
    """Create a node app with test actions in a temp directory.

    Returns:
        (app, actions_dir)
    """
    actions_dir = Path(tmpdir) / node_id / "actions"
    actions_dir.mkdir(parents=True, exist_ok=True)

    # Create a simple sync action
    echo_action = actions_dir / "echo.py"
    echo_action.write_text(
        "def run(params, context):\n"
        "    return {'echo': params.get('msg', ''), 'node': context['node_id']}\n"
    )

    # Create echo schema
    echo_schema = actions_dir / "echo.schema.json"
    echo_schema.write_text(json.dumps({
        "type": "object",
        "properties": {"msg": {"type": "string"}},
        "required": ["msg"],
    }))

    # Create skills
    skills_path = Path(tmpdir) / node_id / "skills.md"
    skills_path.write_text(f"# {node_id}\n\n## echo\nEcho back a message.\n")

    config = NodeConfig(
        node_id=node_id,
        listen=f"0.0.0.0:{port}",
        nodes=nodes,
        max_hop=5,
        cache_ttl_seconds=300,
        auth_token=auth_token,
    )

    registry = load_actions(str(actions_dir))
    schema_registry = load_schemas(str(actions_dir))
    app = create_app(config, registry, str(skills_path), schema_registry=schema_registry)
    return app, config


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_action_execution() -> None:
    """Node executes action locally when target_node_id == self."""
    with tempfile.TemporaryDirectory() as tmpdir:
        app, _ = _create_test_node(tmpdir, "node-0", 8080, {"node-0": "http://127.0.0.1:8080"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/action", json={
                "target_node_id": "node-0",
                "task_id": "int-test-001",
                "trace": {"hop_count": 0, "route_path": []},
                "payload": {"action": "echo", "params": {"msg": "hello"}},
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "completed"
            assert data["output"]["echo"] == "hello"
            assert data["output"]["node"] == "node-0"


@pytest.mark.asyncio
async def test_schema_validation_rejects_bad_params() -> None:
    """Schema validation returns 422 for invalid params."""
    with tempfile.TemporaryDirectory() as tmpdir:
        app, _ = _create_test_node(tmpdir, "node-0", 8080, {"node-0": "http://127.0.0.1:8080"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Missing required "msg" field
            resp = await client.post("/action", json={
                "target_node_id": "node-0",
                "task_id": "int-test-002",
                "trace": {"hop_count": 0, "route_path": []},
                "payload": {"action": "echo", "params": {}},
            })
            assert resp.status_code == 422
            assert "SCHEMA_VALIDATION_ERROR" in resp.json()["error"]


@pytest.mark.asyncio
async def test_node_not_found_returns_error() -> None:
    """Routing to an unknown node returns NODE_NOT_FOUND."""
    with tempfile.TemporaryDirectory() as tmpdir:
        app, _ = _create_test_node(tmpdir, "node-0", 8080, {"node-0": "http://127.0.0.1:8080"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/action", json={
                "target_node_id": "node-99",
                "task_id": "int-test-003",
                "trace": {"hop_count": 0, "route_path": []},
                "payload": {"action": "echo", "params": {"msg": "hi"}},
            })
            assert resp.status_code == 400
            assert "NOT_FOUND" in resp.json()["error"]


@pytest.mark.asyncio
async def test_hop_count_protection() -> None:
    """Requests exceeding max_hop are rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        app, _ = _create_test_node(tmpdir, "node-0", 8080, {"node-0": "http://127.0.0.1:8080"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/action", json={
                "target_node_id": "node-0",
                "task_id": "int-test-004",
                "trace": {"hop_count": 100, "route_path": []},
                "payload": {"action": "echo", "params": {"msg": "hi"}},
            })
            assert resp.status_code == 400
            assert "hop" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_loop_detection() -> None:
    """Request with self in route_path is rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        app, _ = _create_test_node(tmpdir, "node-0", 8080, {"node-0": "http://127.0.0.1:8080"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/action", json={
                "target_node_id": "node-0",
                "task_id": "int-test-005",
                "trace": {"hop_count": 1, "route_path": ["node-0"]},
                "payload": {"action": "echo", "params": {"msg": "hi"}},
            })
            assert resp.status_code == 400
            assert "loop" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_auth_blocks_unauthenticated() -> None:
    """Protected node rejects requests without a valid token."""
    with tempfile.TemporaryDirectory() as tmpdir:
        app, _ = _create_test_node(
            tmpdir, "node-0", 8080,
            {"node-0": "http://127.0.0.1:8080"},
            auth_token="test-secret",
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # No token
            resp = await client.post("/action", json={
                "target_node_id": "node-0",
                "task_id": "int-test-006",
                "trace": {"hop_count": 0, "route_path": []},
                "payload": {"action": "echo", "params": {"msg": "hi"}},
            })
            assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_allows_authenticated() -> None:
    """Protected node allows requests with valid token."""
    with tempfile.TemporaryDirectory() as tmpdir:
        app, _ = _create_test_node(
            tmpdir, "node-0", 8080,
            {"node-0": "http://127.0.0.1:8080"},
            auth_token="test-secret",
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/action",
                json={
                    "target_node_id": "node-0",
                    "task_id": "int-test-007",
                    "trace": {"hop_count": 0, "route_path": []},
                    "payload": {"action": "echo", "params": {"msg": "secure"}},
                },
                headers={"Authorization": "Bearer test-secret"},
            )
            assert resp.status_code == 200
            assert resp.json()["output"]["echo"] == "secure"


@pytest.mark.asyncio
async def test_health_and_skills_exempt_from_auth() -> None:
    """Health and skills endpoints work without auth even when token is set."""
    with tempfile.TemporaryDirectory() as tmpdir:
        app, _ = _create_test_node(
            tmpdir, "node-0", 8080,
            {"node-0": "http://127.0.0.1:8080"},
            auth_token="test-secret",
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["node_id"] == "node-0"

            resp = await client.get("/skills")
            assert resp.status_code == 200
            assert "node-0" in resp.text


@pytest.mark.asyncio
async def test_resolve_endpoint() -> None:
    """Resolve returns correct addresses for known nodes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nodes = {
            "node-0": "http://127.0.0.1:8080",
            "node-1": "http://10.0.0.1:8081",
        }
        app, _ = _create_test_node(tmpdir, "node-0", 8080, nodes)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/resolve/node-1")
            assert resp.status_code == 200
            assert resp.json()["address"] == "http://10.0.0.1:8081"

            resp = await client.get("/resolve/node-99")
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_action_not_found_returns_error() -> None:
    """Calling a nonexistent action returns an error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        app, _ = _create_test_node(tmpdir, "node-0", 8080, {"node-0": "http://127.0.0.1:8080"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/action", json={
                "target_node_id": "node-0",
                "task_id": "int-test-008",
                "trace": {"hop_count": 0, "route_path": []},
                "payload": {"action": "nonexistent", "params": {}},
            })
            assert resp.status_code == 400
            assert "not found" in resp.json()["error"].lower()
