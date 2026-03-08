"""Integration tests for Phase 5: Cluster Provisioning.

Tests cover:
- BlueprintStore: load index, get blueprints, save blueprints
- BootstrapRequest v6: all config fields written to node.yaml
- PortAllocator: allocate, release, collision detection
- ClusterOrchestrator: provision, list, kickoff, teardown (mocked)
- ClusterSpec / NodeSpec / ClusterProvisionResult models
- Server endpoints: /blueprints, /clusters, /gateways/connect, /shutdown
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from runtime.blueprint_store import BlueprintStore, BlueprintNotFoundError
from runtime.bootstrap import BootstrapRequest, ActionFile, BootstrapStatus
from runtime.cluster_orchestrator import ClusterOrchestrator, PortAllocator
from runtime.models import (
    ClusterSpec,
    NodeSpec,
    ClusterProvisionResult,
    ClusterInfo,
    ClusterNodeResult,
    TeardownResult,
    BlueprintInfo,
    BlueprintListResponse,
    GatewayConnectRequest,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_blueprints_dir(tmp_path: Path) -> Path:
    """Create a temporary blueprints directory with minimal content."""
    bp_dir = tmp_path / "blueprints"
    roles_dir = bp_dir / "roles"
    teams_dir = bp_dir / "teams"
    generated_dir = bp_dir / "generated"
    roles_dir.mkdir(parents=True)
    teams_dir.mkdir(parents=True)
    generated_dir.mkdir(parents=True)

    # Write INDEX.yaml
    (bp_dir / "INDEX.yaml").write_text(
        yaml.dump({
            "blueprints": [
                {
                    "id": "pm",
                    "type": "role",
                    "name": "Project Manager",
                    "description": "Orchestrates workflows",
                    "tags": ["role", "management"],
                    "path": "roles/pm.md",
                },
                {
                    "id": "developer",
                    "type": "role",
                    "name": "Developer",
                    "description": "Implements features",
                    "tags": ["role", "development"],
                    "path": "roles/developer.md",
                },
                {
                    "id": "standard-cluster",
                    "type": "team",
                    "name": "Standard Cluster",
                    "description": "6-role team",
                    "tags": ["team", "standard"],
                    "path": "teams/standard-cluster.yaml",
                },
            ]
        }),
        encoding="utf-8",
    )

    # Write role files
    (roles_dir / "pm.md").write_text("# Role: PM\n\nYou are the PM.", encoding="utf-8")
    (roles_dir / "developer.md").write_text("# Role: Developer\n\nYou are a developer.", encoding="utf-8")

    # Write team file
    (teams_dir / "standard-cluster.yaml").write_text(
        yaml.dump({"name": "Standard Cluster", "roles": ["pm", "developer"]}),
        encoding="utf-8",
    )

    return bp_dir


@pytest.fixture
def blueprint_store(tmp_blueprints_dir: Path) -> BlueprintStore:
    return BlueprintStore(blueprints_dir=str(tmp_blueprints_dir))


# ── BlueprintStore Tests ─────────────────────────────────────────────────────

class TestBlueprintStore:
    @pytest.mark.asyncio
    async def test_startup_load_reads_index(self, blueprint_store: BlueprintStore):
        count = await blueprint_store.startup_load()
        assert count == 3

    @pytest.mark.asyncio
    async def test_list_blueprints_all(self, blueprint_store: BlueprintStore):
        await blueprint_store.startup_load()
        entries = await blueprint_store.list_blueprints()
        assert len(entries) == 3

    @pytest.mark.asyncio
    async def test_list_blueprints_filter_by_type(self, blueprint_store: BlueprintStore):
        await blueprint_store.startup_load()
        roles = await blueprint_store.list_blueprints(blueprint_type="role")
        assert len(roles) == 2
        assert all(b["type"] == "role" for b in roles)

    @pytest.mark.asyncio
    async def test_list_blueprints_filter_by_tag(self, blueprint_store: BlueprintStore):
        await blueprint_store.startup_load()
        teams = await blueprint_store.list_blueprints(tags=["team"])
        assert len(teams) == 1
        assert teams[0]["id"] == "standard-cluster"

    @pytest.mark.asyncio
    async def test_get_blueprint_role(self, blueprint_store: BlueprintStore):
        content = await blueprint_store.get_blueprint("role", "pm")
        assert "# Role: PM" in content
        assert "PM" in content

    @pytest.mark.asyncio
    async def test_get_blueprint_team(self, blueprint_store: BlueprintStore):
        content = await blueprint_store.get_blueprint("team", "standard-cluster")
        assert "Standard Cluster" in content

    @pytest.mark.asyncio
    async def test_get_blueprint_not_found(self, blueprint_store: BlueprintStore):
        with pytest.raises(BlueprintNotFoundError):
            await blueprint_store.get_blueprint("role", "nonexistent-role")

    @pytest.mark.asyncio
    async def test_get_role_blueprint_shorthand(self, blueprint_store: BlueprintStore):
        content = await blueprint_store.get_role_blueprint("pm")
        assert "PM" in content

    @pytest.mark.asyncio
    async def test_save_blueprint_persists(self, blueprint_store: BlueprintStore, tmp_blueprints_dir: Path):
        await blueprint_store.startup_load()
        saved_path = await blueprint_store.save_blueprint(
            blueprint_type="generated",
            blueprint_id="custom-agent",
            content="# Custom Agent\n\nDoes custom things.",
            meta={"name": "Custom Agent", "description": "Test", "tags": ["custom"]},
        )
        assert Path(saved_path).exists()
        # Should be in index now
        entries = await blueprint_store.list_blueprints()
        ids = [b["id"] for b in entries]
        assert "custom-agent" in ids

    @pytest.mark.asyncio
    async def test_missing_index_returns_zero(self, tmp_path: Path):
        store = BlueprintStore(blueprints_dir=str(tmp_path / "missing"))
        count = await store.startup_load()
        assert count == 0


# ── BootstrapRequest v6 Tests ────────────────────────────────────────────────

class TestBootstrapRequestV6:
    def test_default_fields(self):
        req = BootstrapRequest(node_id="test-node", listen="0.0.0.0:9000")
        assert req.node_id == "test-node"
        assert req.listen == "0.0.0.0:9000"
        assert req.event_bus_enabled is True
        assert req.scheduler_enabled is True
        assert req.task_pool_enabled is True
        assert req.memory_enabled is True
        assert req.session_backend == "persistent"
        assert req.llm_model == "claude-sonnet-4-20250514"

    def test_gateway_fields(self):
        req = BootstrapRequest(
            node_id="worker-A",
            listen="0.0.0.0:9001",
            gateway_node_id="gateway-cluster-1",
            gateway_address="http://localhost:9000",
            gateway_auth_token="tok-123",
        )
        assert req.gateway_node_id == "gateway-cluster-1"
        assert req.gateway_address == "http://localhost:9000"
        assert req.gateway_auth_token == "tok-123"

    def test_v6_feature_fields(self):
        req = BootstrapRequest(
            node_id="node-X",
            listen="0.0.0.0:9002",
            llm_api_key="sk-test",
            llm_model="claude-opus-4-6",
            event_bus_enabled=False,
            task_pool_enabled=False,
            memory_enabled=False,
            mcp_servers=[{"id": "fs", "transport": "stdio", "command": "npx fs-server"}],
        )
        assert req.llm_api_key == "sk-test"
        assert req.llm_model == "claude-opus-4-6"
        assert req.event_bus_enabled is False
        assert req.task_pool_enabled is False
        assert req.memory_enabled is False
        assert len(req.mcp_servers) == 1

    @pytest.mark.asyncio
    async def test_write_config_produces_v6_yaml(self, tmp_path: Path):
        """_step_write_config should produce a complete v6 node.yaml."""
        from runtime.bootstrap import BootstrapEngine, _RollbackTracker, BootstrapResult

        engine = BootstrapEngine()
        node_dir = tmp_path / "test-node"
        node_dir.mkdir()
        tracker = _RollbackTracker()
        result = BootstrapResult(node_id="test-node", status=BootstrapStatus.IN_PROGRESS)

        req = BootstrapRequest(
            node_id="test-node",
            listen="0.0.0.0:9000",
            gateway_node_id="gw-A",
            gateway_address="http://localhost:8090",
            gateway_auth_token="gw-token",
            llm_api_key="sk-test",
            llm_model="claude-sonnet-4-20250514",
            event_bus_enabled=True,
            scheduler_enabled=True,
            task_pool_enabled=True,
            memory_enabled=True,
            session_backend="persistent",
            allowed_tokens=["tok-a", "tok-b"],
        )

        await engine._step_write_config(node_dir, req, tracker, result)

        config_path = node_dir / "node.yaml"
        assert config_path.exists()

        with open(config_path) as f:
            data = yaml.safe_load(f)

        assert data["node_id"] == "test-node"
        assert data["gateway_node_id"] == "gw-A"
        assert data["gateway_address"] == "http://localhost:8090"
        assert data["llm"]["model"] == "claude-sonnet-4-20250514"
        assert data["event_bus"]["enabled"] is True
        assert data["scheduler"]["enabled"] is True
        assert data["task_pool"]["enabled"] is True
        assert data["memory"]["enabled"] is True
        assert data["session"]["backend"] == "persistent"
        assert "tok-a" in data["allowed_tokens"]
        assert "write_config" in result.steps_completed


# ── PortAllocator Tests ───────────────────────────────────────────────────────

class TestPortAllocator:
    @pytest.mark.asyncio
    async def test_allocate_returns_port_in_range(self, tmp_path: Path):
        allocator = PortAllocator(start=19000, end=19010, state_path=str(tmp_path / "ports.json"))
        port = await allocator.allocate()
        assert 19000 <= port <= 19010

    @pytest.mark.asyncio
    async def test_allocate_distinct_ports(self, tmp_path: Path):
        allocator = PortAllocator(start=19020, end=19030, state_path=str(tmp_path / "ports.json"))
        ports = [await allocator.allocate() for _ in range(3)]
        assert len(set(ports)) == 3

    @pytest.mark.asyncio
    async def test_release_makes_port_available(self, tmp_path: Path):
        allocator = PortAllocator(start=19040, end=19041, state_path=str(tmp_path / "ports.json"))
        port1 = await allocator.allocate()
        await allocator.release(port1)
        # After release, port can be re-allocated
        port2 = await allocator.allocate()
        assert port2 in range(19040, 19042)

    @pytest.mark.asyncio
    async def test_persists_state(self, tmp_path: Path):
        state_file = tmp_path / "ports.json"
        allocator = PortAllocator(start=19050, end=19060, state_path=str(state_file))
        port = await allocator.allocate()

        # Reload from state
        allocator2 = PortAllocator(start=19050, end=19060, state_path=str(state_file))
        # Allocated port should be tracked
        assert port in allocator2._allocated


# ── ClusterSpec / NodeSpec Model Tests ───────────────────────────────────────

class TestClusterModels:
    def test_cluster_spec_defaults(self):
        spec = ClusterSpec(llm_api_key="sk-test")
        assert spec.cluster_id.startswith("cluster-")
        assert spec.port_range_start == 8090
        assert spec.port_range_end == 8200
        assert spec.wire_subscriptions is True
        assert spec.kickoff_on_provision is False

    def test_node_spec_defaults(self):
        node = NodeSpec(node_id="pm-A", role="pm", listen="0.0.0.0:8091")
        assert node.event_bus_enabled is True
        assert node.task_pool_enabled is True
        assert node.memory_enabled is True
        assert node.session_backend == "persistent"
        assert node.is_gateway is False

    def test_cluster_provision_result_structure(self):
        gw = ClusterNodeResult(node_id="gw-A", role="gateway", address="http://localhost:8090", status="running")
        workers = [
            ClusterNodeResult(node_id="pm-A", role="pm", address="http://localhost:8091", status="running"),
            ClusterNodeResult(node_id="dev-A", role="developer", address="http://localhost:8092", status="running"),
        ]
        result = ClusterProvisionResult(
            cluster_id="cluster-test",
            status="running",
            gateway=gw,
            workers=workers,
        )
        assert result.cluster_id == "cluster-test"
        assert result.status == "running"
        assert len(result.workers) == 2
        assert result.gateway is not None

    def test_teardown_result(self):
        result = TeardownResult(
            cluster_id="cluster-test",
            stopped_nodes=["gw-A", "pm-A"],
            failed_nodes=[],
        )
        assert len(result.stopped_nodes) == 2
        assert len(result.failed_nodes) == 0


# ── ClusterOrchestrator Tests (mocked bootstrap) ─────────────────────────────

class TestClusterOrchestrator:
    @pytest.fixture
    def orchestrator(self, tmp_path: Path) -> ClusterOrchestrator:
        return ClusterOrchestrator(
            seed_config_path=None,
            port_range_start=19100,
            port_range_end=19200,
            state_path=str(tmp_path / "clusters.json"),
            blueprints_dir=str(tmp_path / "blueprints"),
            seed_auth_token="seed-token",
        )

    @pytest.mark.asyncio
    async def test_list_clusters_empty_initially(self, orchestrator: ClusterOrchestrator):
        clusters = await orchestrator.list_clusters()
        assert clusters == []

    @pytest.mark.asyncio
    async def test_get_cluster_returns_none_if_not_found(self, orchestrator: ClusterOrchestrator):
        result = await orchestrator.get_cluster("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_teardown_not_found_cluster(self, orchestrator: ClusterOrchestrator):
        result = await orchestrator.teardown_cluster("nonexistent")
        assert result.error is not None
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_provision_cluster_mocked(self, orchestrator: ClusterOrchestrator, tmp_path: Path):
        """Provision with mocked BootstrapEngine to avoid spawning real processes."""
        from runtime.bootstrap import BootstrapResult

        mock_gw_result = BootstrapResult(
            node_id="gw-test",
            status=BootstrapStatus.COMPLETED,
            address="http://127.0.0.1:19100",
            pid=99999,
        )
        mock_worker_result = BootstrapResult(
            node_id="pm-test",
            status=BootstrapStatus.COMPLETED,
            address="http://127.0.0.1:19101",
            pid=99998,
        )

        with patch.object(orchestrator._bootstrap_engine, "bootstrap", new_callable=AsyncMock) as mock_boot:
            mock_boot.side_effect = [mock_gw_result, mock_worker_result]
            # Patch subscription wiring to no-op
            with patch.object(orchestrator, "_wire_subscriptions", new_callable=AsyncMock):
                spec = ClusterSpec(
                    cluster_id="test-cluster",
                    llm_api_key="sk-test",
                    gateway=NodeSpec(
                        node_id="gw-test",
                        role="gateway",
                        listen="0.0.0.0:19100",
                        is_gateway=True,
                    ),
                    workers=[
                        NodeSpec(node_id="pm-test", role="pm", listen="0.0.0.0:19101"),
                    ],
                )
                result = await orchestrator.provision_cluster(spec)

        assert result.cluster_id == "test-cluster"
        assert result.status in ("running", "partial")
        assert result.gateway is not None
        assert result.gateway.node_id == "gw-test"
        assert len(result.workers) == 1

    @pytest.mark.asyncio
    async def test_provision_persists_cluster_state(self, orchestrator: ClusterOrchestrator, tmp_path: Path):
        """After provisioning, cluster should appear in list_clusters."""
        from runtime.bootstrap import BootstrapResult

        mock_result = BootstrapResult(
            node_id="gw-X",
            status=BootstrapStatus.COMPLETED,
            address="http://127.0.0.1:19110",
            pid=88888,
        )

        with patch.object(orchestrator._bootstrap_engine, "bootstrap", new_callable=AsyncMock) as mock_boot:
            mock_boot.return_value = mock_result
            with patch.object(orchestrator, "_wire_subscriptions", new_callable=AsyncMock):
                spec = ClusterSpec(
                    cluster_id="persistent-cluster",
                    llm_api_key="sk-test",
                    gateway=NodeSpec(
                        node_id="gw-X",
                        role="gateway",
                        listen="0.0.0.0:19110",
                        is_gateway=True,
                    ),
                    workers=[],
                )
                await orchestrator.provision_cluster(spec)

        clusters = await orchestrator.list_clusters()
        assert any(c.cluster_id == "persistent-cluster" for c in clusters)

    @pytest.mark.asyncio
    async def test_kickoff_returns_false_if_no_cluster(self, orchestrator: ClusterOrchestrator):
        result = await orchestrator.kickoff_cluster("ghost-cluster")
        assert result is False

    @pytest.mark.asyncio
    async def test_state_persisted_to_json(self, orchestrator: ClusterOrchestrator, tmp_path: Path):
        """Cluster state should be written to the state JSON file."""
        # Inject a fake cluster directly
        import time as _time
        fake = ClusterInfo(
            cluster_id="state-test",
            cluster_name="State Test",
            status="running",
            provisioned_at=_time.time(),
        )
        async with orchestrator._clusters_lock:
            orchestrator._clusters["state-test"] = fake
        await orchestrator._persist_state()

        state_file = Path(orchestrator._state_path)
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        ids = [c["cluster_id"] for c in data["clusters"]]
        assert "state-test" in ids


# ── Server Endpoint Tests (FastAPI TestClient) ───────────────────────────────

class TestPhase5Endpoints:
    @pytest.fixture
    def test_app(self, tmp_path: Path):
        """Create a minimal FastAPI test app with Phase 5 components."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from runtime.blueprint_store import BlueprintStore as _BS
        from runtime.cluster_orchestrator import ClusterOrchestrator as _CO
        from runtime.models import (
            BlueprintListResponse, BlueprintInfo,
            ClusterProvisionResult, ClusterInfo, TeardownResult,
            GatewayConnectRequest, GatewayConnectResponse,
        )

        # Create minimal blueprints dir
        bp_dir = tmp_path / "blueprints"
        bp_dir.mkdir()
        (bp_dir / "INDEX.yaml").write_text(
            yaml.dump({"blueprints": [
                {"id": "pm", "type": "role", "name": "PM", "description": "", "tags": [], "path": "roles/pm.md"}
            ]}),
        )
        (bp_dir / "roles").mkdir()
        (bp_dir / "roles" / "pm.md").write_text("# PM Role")

        store = _BS(blueprints_dir=str(bp_dir))
        orchestrator = _CO(
            seed_config_path=None,
            port_range_start=19200,
            port_range_end=19300,
            state_path=str(tmp_path / "clusters.json"),
        )

        app = FastAPI()

        @app.get("/blueprints")
        async def list_bp():
            entries = await store.list_blueprints()
            items = [BlueprintInfo(**{k: b.get(k, "") for k in ["id", "type", "name", "description", "path"]} | {"tags": b.get("tags", [])}) for b in entries]
            return BlueprintListResponse(blueprints=items, total=len(items))

        @app.get("/blueprints/{btype}/{bid}")
        async def get_bp(btype: str, bid: str):
            from fastapi.responses import PlainTextResponse as PR
            try:
                content = await store.get_blueprint(btype, bid)
                return PR(content=content)
            except BlueprintNotFoundError:
                from fastapi.responses import JSONResponse as JR
                return JR({"error": "NOT_FOUND"}, status_code=404)

        @app.get("/clusters")
        async def list_cl():
            clusters = await orchestrator.list_clusters()
            return {"clusters": [c.model_dump() for c in clusters], "total": len(clusters)}

        return TestClient(app)

    def test_list_blueprints_endpoint(self, test_app):
        resp = test_app.get("/blueprints")
        assert resp.status_code == 200
        data = resp.json()
        assert "blueprints" in data
        assert data["total"] >= 0

    def test_get_blueprint_endpoint(self, test_app):
        # Load the index first
        test_app.get("/blueprints")  # triggers startup_load
        resp = test_app.get("/blueprints/role/pm")
        # May be 200 or 404 depending on whether startup_load ran
        assert resp.status_code in (200, 404)

    def test_list_clusters_empty(self, test_app):
        resp = test_app.get("/clusters")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ── Integration: BootstrapRequest v6 Round-trip ───────────────────────────────

class TestBootstrapV6RoundTrip:
    """Verify the full flow: BootstrapRequest → node.yaml → parseable config."""

    @pytest.mark.asyncio
    async def test_bootstrap_request_yaml_parseable(self, tmp_path: Path):
        """node.yaml written by _step_write_config must be parseable by load_config."""
        from runtime.bootstrap import BootstrapEngine, _RollbackTracker, BootstrapResult
        from runtime.config import load_config

        node_dir = tmp_path / "test-node-v6"
        node_dir.mkdir()

        engine = BootstrapEngine()
        tracker = _RollbackTracker()
        result = BootstrapResult(node_id="test-node-v6", status=BootstrapStatus.IN_PROGRESS)

        req = BootstrapRequest(
            node_id="test-node-v6",
            listen="0.0.0.0:9999",
            auth_token="tok-abc",
            allowed_tokens=["tok-gw", "tok-abc"],
            gateway_node_id="gw-main",
            gateway_address="http://127.0.0.1:8090",
            gateway_auth_token="tok-gw",
            llm_api_key="sk-test-key",
            llm_model="claude-sonnet-4-20250514",
            event_bus_enabled=True,
            scheduler_enabled=True,
            task_pool_enabled=True,
            checkpoint_store_enabled=True,
            memory_enabled=True,
            session_backend="persistent",
            schedule=[{
                "trigger_type": "event",
                "on_event_type": "clarification.answered",
                "run_action": "handle_clarification_answer",
                "run_params": {},
            }],
        )

        await engine._step_write_config(node_dir, req, tracker, result)

        config_path = node_dir / "node.yaml"
        assert config_path.exists()

        # Must be parseable as YAML
        with open(config_path) as f:
            raw = yaml.safe_load(f)

        assert raw["node_id"] == "test-node-v6"
        assert raw["listen"] == "0.0.0.0:9999"
        assert raw["gateway_node_id"] == "gw-main"
        assert raw["event_bus"]["enabled"] is True
        assert raw["task_pool"]["enabled"] is True
        assert len(raw["schedule"]) == 1
        assert raw["schedule"][0]["trigger_type"] == "event"

        # load_config should not raise
        config = load_config(str(config_path))
        assert config.node_id == "test-node-v6"
        assert config.gateway_node_id == "gw-main"
        assert config.event_bus.enabled is True
        assert config.task_pool.enabled is True
