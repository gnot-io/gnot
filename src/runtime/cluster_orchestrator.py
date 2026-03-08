"""ClusterOrchestrator — provisions and tears down GNOT node clusters.

Phase 5: v6.0 Cluster Provisioning.

Provision flow:
    1. Allocate ports for all nodes (gateway + workers)
    2. Bootstrap gateway node via BootstrapEngine
    3. Bootstrap workers in parallel (each pointing to gateway)
    4. Wire standard subscriptions (clarification.answered → handle_clarification_answer)
    5. Optionally emit cluster.started to kick off the workflow
    6. Persist cluster state for teardown

Teardown flow:
    1. POST /shutdown to each node (graceful 5s window)
    2. SIGTERM remaining processes
    3. Remove from active clusters registry
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from runtime.bootstrap import BootstrapEngine, BootstrapRequest, BootstrapStatus
from runtime.models import (
    ClusterInfo,
    ClusterNodeResult,
    ClusterProvisionResult,
    ClusterSpec,
    NodeSpec,
    TeardownResult,
)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

PROVISION_TIMEOUT_SECONDS: float = 120.0    # per-node bootstrap timeout
SHUTDOWN_GRACE_SECONDS: float = 5.0
SHUTDOWN_HTTP_TIMEOUT: float = 5.0
WIRE_RETRY_COUNT: int = 3
WIRE_RETRY_DELAY: float = 2.0

# Standard subscriptions wired to every cluster member
_STANDARD_SUBSCRIPTIONS: list[dict] = [
    {
        "event_type_pattern": "clarification.answered",
        "callback_action": "handle_clarification_answer",
        "callback_params_template": {},
        "description": "Resume suspended task when answer arrives",
    },
    {
        "event_type_pattern": "clarification.timeout",
        "callback_action": "handle_clarification_timeout",
        "callback_params_template": {},
        "description": "Handle suspended task timeout with assumption",
    },
]


@dataclass
class _AllocatedPort:
    port: int
    in_use: bool = False


class PortAllocator:
    """Thread-safe port allocator from a configured range.

    Tracks which ports are currently allocated and releases them on teardown.
    State is stored in a JSON sidecar file for persistence across restarts.
    """

    def __init__(self, start: int, end: int, state_path: str | None = None) -> None:
        self._start = start
        self._end = end
        self._state_path = Path(state_path) if state_path else None
        self._allocated: set[int] = set()
        self._lock = asyncio.Lock()

        if self._state_path and self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text())
                self._allocated = set(data.get("allocated_ports", []))
                logger.info("PortAllocator restored %d allocated ports", len(self._allocated))
            except Exception as exc:
                logger.warning("Could not restore port state: %s", exc)

    async def allocate(self) -> int:
        """Allocate the next free port in the range."""
        async with self._lock:
            for port in range(self._start, self._end + 1):
                if port not in self._allocated and not await self._is_port_in_use(port):
                    self._allocated.add(port)
                    await self._persist()
                    return port
            raise RuntimeError(
                f"No free ports available in range {self._start}–{self._end}. "
                f"Currently allocated: {sorted(self._allocated)}"
            )

    async def release(self, port: int) -> None:
        """Release a previously allocated port back to the pool."""
        async with self._lock:
            self._allocated.discard(port)
            await self._persist()

    async def release_many(self, ports: list[int]) -> None:
        async with self._lock:
            for p in ports:
                self._allocated.discard(p)
            await self._persist()

    @staticmethod
    async def _is_port_in_use(port: int) -> bool:
        """Check if a port is currently in use by trying to bind to it."""
        import socket
        def _check() -> bool:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                s.close()
                return False
            except OSError:
                return True
        return await asyncio.to_thread(_check)

    async def _persist(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"allocated_ports": sorted(self._allocated)}),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Could not persist port state: %s", exc)


class ClusterOrchestrator:
    """Orchestrates cluster lifecycle: provision, wire subscriptions, kickoff, teardown.

    One seed node runs a ClusterOrchestrator. It uses the local BootstrapEngine
    to spawn gateway + worker processes, then wires EventBus subscriptions via HTTP.

    Cluster state is persisted to a JSON file so teardown can happen after restart.
    """

    def __init__(
        self,
        seed_config_path: str | None,
        port_range_start: int = 8090,
        port_range_end: int = 8200,
        state_path: str = "./clusters.json",
        blueprints_dir: str = "./blueprints",
        seed_auth_token: str | None = None,
    ) -> None:
        self._state_path = Path(state_path)
        self._blueprints_dir = blueprints_dir
        self._seed_auth_token = seed_auth_token
        self._bootstrap_engine = BootstrapEngine(seed_config_path=seed_config_path)
        self._port_allocator = PortAllocator(
            start=port_range_start,
            end=port_range_end,
            state_path=str(self._state_path.with_suffix(".ports.json")),
        )
        self._clusters: dict[str, ClusterInfo] = {}
        self._clusters_lock = asyncio.Lock()
        self._load_state()

    # ── Public API ──────────────────────────────────────────────────────────

    async def provision_cluster(
        self,
        spec: ClusterSpec,
    ) -> ClusterProvisionResult:
        """Provision a complete cluster from a ClusterSpec.

        Steps:
            1. Auto-allocate ports if not specified in the spec
            2. Bootstrap gateway node
            3. Bootstrap workers in parallel
            4. Wire standard EventBus subscriptions on each node
            5. Persist cluster state
            6. Optionally kickoff

        Returns a ClusterProvisionResult with per-node status.
        """
        logger.info(
            "Provisioning cluster %s — gateway + %d worker(s)",
            spec.cluster_id, len(spec.workers),
        )

        # -- Generate gateway token if not set ---------------------------------
        gateway_token = spec.gateway_token or secrets.token_hex(16)
        cluster_id = spec.cluster_id
        ports_used: list[int] = []

        result = ClusterProvisionResult(
            cluster_id=cluster_id,
            status="provisioning",
        )

        try:
            # -- Step 1: Resolve / build gateway spec --------------------------
            gw_spec = await self._resolve_gateway_spec(spec, gateway_token)
            gw_port = int(gw_spec.listen.rsplit(":", 1)[1])
            ports_used.append(gw_port)
            gw_addr = f"http://127.0.0.1:{gw_port}"

            # -- Step 2: Bootstrap gateway -------------------------------------
            logger.info("Bootstrapping gateway node: %s", gw_spec.node_id)
            gw_req = await self._build_bootstrap_request(gw_spec, spec, gateway_token, gw_addr=None)
            gw_boot = await self._bootstrap_engine.bootstrap(gw_req)

            gw_result = ClusterNodeResult(
                node_id=gw_spec.node_id,
                role="gateway",
                address=gw_boot.address,
                pid=gw_boot.pid,
                status="running" if gw_boot.status == BootstrapStatus.COMPLETED else "failed",
                error=gw_boot.error,
            )
            result.gateway = gw_result

            if gw_boot.status != BootstrapStatus.COMPLETED:
                result.status = "failed"
                result.error = f"Gateway bootstrap failed: {gw_boot.error}"
                return result

            # Wait briefly for gateway to be ready
            await asyncio.sleep(1.0)

            # -- Step 3: Bootstrap workers in parallel -------------------------
            worker_tasks: list[asyncio.Task] = []
            worker_specs: list[NodeSpec] = []
            worker_ports: list[int] = []

            for worker_spec in spec.workers:
                w_port = int(worker_spec.listen.rsplit(":", 1)[1]) if ":" in worker_spec.listen else await self._port_allocator.allocate()
                if ":" not in worker_spec.listen:
                    worker_spec = worker_spec.model_copy(update={"listen": f"0.0.0.0:{w_port}"})
                else:
                    w_port = int(worker_spec.listen.rsplit(":", 1)[1])
                ports_used.append(w_port)
                worker_ports.append(w_port)
                worker_specs.append(worker_spec)

                w_req = await self._build_bootstrap_request(
                    worker_spec, spec, gateway_token,
                    gw_addr=gw_addr,
                    gw_node_id=gw_spec.node_id,
                    gw_auth_token=gateway_token,
                )
                task = asyncio.create_task(
                    self._bootstrap_engine.bootstrap(w_req),
                    name=f"bootstrap-{worker_spec.node_id}",
                )
                worker_tasks.append(task)

            worker_boots = await asyncio.gather(*worker_tasks, return_exceptions=True)

            worker_results: list[ClusterNodeResult] = []
            any_failed = False
            for worker_spec, boot in zip(worker_specs, worker_boots):
                if isinstance(boot, Exception):
                    w_res = ClusterNodeResult(
                        node_id=worker_spec.node_id,
                        role=worker_spec.role,
                        status="failed",
                        error=str(boot),
                    )
                    any_failed = True
                else:
                    w_res = ClusterNodeResult(
                        node_id=worker_spec.node_id,
                        role=worker_spec.role,
                        address=boot.address,
                        pid=boot.pid,
                        status="running" if boot.status == BootstrapStatus.COMPLETED else "failed",
                        error=boot.error,
                    )
                    if boot.status != BootstrapStatus.COMPLETED:
                        any_failed = True
                worker_results.append(w_res)

            result.workers = worker_results
            result.port_range_used = ports_used
            result.status = "partial" if any_failed else "running"

            # -- Step 4: Wire standard subscriptions --------------------------
            if spec.wire_subscriptions and not any_failed:
                all_addresses = []
                if gw_result.address:
                    all_addresses.append(gw_result.address)
                for wr in worker_results:
                    if wr.address and wr.status == "running":
                        all_addresses.append(wr.address)
                await self._wire_subscriptions(all_addresses, gateway_token)

            # -- Step 5: Persist cluster state --------------------------------
            cluster_info = ClusterInfo(
                cluster_id=cluster_id,
                cluster_name=spec.cluster_name or cluster_id,
                status=result.status,
                gateway=gw_result,
                workers=worker_results,
                provisioned_at=time.time(),
                tags=spec.tags,
            )
            async with self._clusters_lock:
                self._clusters[cluster_id] = cluster_info
            await self._persist_state()

            # -- Step 6: Kickoff (optional) ------------------------------------
            if spec.kickoff_on_provision and not any_failed and gw_result.address:
                await self._emit_kickoff(gw_result.address, cluster_id, gateway_token, spec.kickoff_prompt)
                cluster_info = cluster_info.model_copy(update={"kickoff_sent": True})
                async with self._clusters_lock:
                    self._clusters[cluster_id] = cluster_info
                await self._persist_state()

            logger.info(
                "Cluster %s provisioned — status=%s, nodes=%d",
                cluster_id, result.status, 1 + len(worker_results),
            )
            return result

        except Exception as exc:
            logger.exception("Cluster provisioning failed for %s", cluster_id)
            result.status = "failed"
            result.error = str(exc)
            # Release allocated ports on failure
            await self._port_allocator.release_many(ports_used)
            return result

    async def teardown_cluster(
        self,
        cluster_id: str,
        archive_logs: bool = True,
    ) -> TeardownResult:
        """Gracefully stop all nodes in a cluster.

        Sends POST /shutdown to each node, falls back to SIGTERM on failure.
        Updates cluster state to 'torn_down'.
        """
        async with self._clusters_lock:
            cluster = self._clusters.get(cluster_id)
        if cluster is None:
            return TeardownResult(
                cluster_id=cluster_id,
                error=f"Cluster not found: {cluster_id}",
            )

        logger.info("Tearing down cluster %s", cluster_id)

        # Collect all addresses + PIDs
        nodes_to_stop: list[tuple[str, str | None, int | None]] = []  # (node_id, address, pid)
        if cluster.gateway:
            nodes_to_stop.append((cluster.gateway.node_id, cluster.gateway.address, cluster.gateway.pid))
        for w in cluster.workers:
            nodes_to_stop.append((w.node_id, w.address, w.pid))

        stopped: list[str] = []
        failed: list[str] = []

        # Shutdown workers first, gateway last
        worker_nodes = nodes_to_stop[1:]  # skip gateway
        gateway_node = nodes_to_stop[:1]

        for batch in [worker_nodes, gateway_node]:
            for node_id, address, pid in batch:
                success = await self._shutdown_node(node_id, address, pid)
                if success:
                    stopped.append(node_id)
                else:
                    failed.append(node_id)

        # Update cluster state
        updated = cluster.model_copy(update={
            "status": "torn_down",
            "torn_down_at": time.time(),
        })
        async with self._clusters_lock:
            self._clusters[cluster_id] = updated
        await self._persist_state()

        # Release ports
        ports = cluster.model_dump().get("port_range_used", [])
        # Extract ports from addresses
        for _, address, _ in nodes_to_stop:
            if address:
                try:
                    port = int(address.rsplit(":", 1)[1].rstrip("/"))
                    await self._port_allocator.release(port)
                except (ValueError, IndexError):
                    pass

        logger.info(
            "Cluster %s torn down — stopped=%s, failed=%s",
            cluster_id, stopped, failed,
        )
        return TeardownResult(
            cluster_id=cluster_id,
            stopped_nodes=stopped,
            failed_nodes=failed,
            archived=archive_logs,
        )

    async def kickoff_cluster(
        self,
        cluster_id: str,
        auth_token: str | None = None,
        prompt: str = "",
    ) -> bool:
        """Emit cluster.started event to the cluster's gateway.

        Returns True if the event was emitted successfully.
        """
        async with self._clusters_lock:
            cluster = self._clusters.get(cluster_id)
        if cluster is None or cluster.gateway is None or not cluster.gateway.address:
            return False

        token = auth_token or self._seed_auth_token
        await self._emit_kickoff(cluster.gateway.address, cluster_id, token, prompt)

        updated = cluster.model_copy(update={"kickoff_sent": True})
        async with self._clusters_lock:
            self._clusters[cluster_id] = updated
        await self._persist_state()
        return True

    async def list_clusters(self) -> list[ClusterInfo]:
        """Return all known clusters (all statuses)."""
        async with self._clusters_lock:
            return list(self._clusters.values())

    async def get_cluster(self, cluster_id: str) -> ClusterInfo | None:
        """Return info for a specific cluster, or None if not found."""
        async with self._clusters_lock:
            return self._clusters.get(cluster_id)

    # ── Internal helpers ────────────────────────────────────────────────────

    async def _resolve_gateway_spec(self, spec: ClusterSpec, gateway_token: str) -> NodeSpec:
        """Build or validate the gateway NodeSpec, allocating port if needed."""
        if spec.gateway:
            gw = spec.gateway
            if ":" not in gw.listen or not gw.listen.rsplit(":", 1)[1].isdigit():
                port = await self._port_allocator.allocate()
                gw = gw.model_copy(update={"listen": f"0.0.0.0:{port}"})
            return gw

        # Auto-generate minimal gateway spec
        port = await self._port_allocator.allocate()
        return NodeSpec(
            node_id=f"gateway-{spec.cluster_id}",
            role="gateway",
            listen=f"0.0.0.0:{port}",
            is_gateway=True,
            auth_token=gateway_token,
            allowed_tokens=[gateway_token],
            skills_md=f"# Gateway — {spec.cluster_id}\n\nCluster gateway for {spec.cluster_name or spec.cluster_id}.\n",
        )

    async def _build_bootstrap_request(
        self,
        node_spec: NodeSpec,
        cluster_spec: ClusterSpec,
        gateway_token: str,
        gw_addr: str | None = None,
        gw_node_id: str | None = None,
        gw_auth_token: str | None = None,
    ) -> BootstrapRequest:
        """Convert a NodeSpec into a BootstrapRequest."""
        # Inherit LLM config from cluster spec if node doesn't override
        llm_key = node_spec.llm_api_key or cluster_spec.llm_api_key
        llm_model = node_spec.llm_model or cluster_spec.llm_model
        llm_base_url = node_spec.llm_base_url or cluster_spec.llm_base_url

        # Build allowed_tokens: node's own token + gateway token
        allowed = list(node_spec.allowed_tokens) if node_spec.allowed_tokens else []
        if gateway_token and gateway_token not in allowed:
            allowed.append(gateway_token)

        # Convert action dicts to ActionFile objects
        from runtime.bootstrap import ActionFile
        action_files = []
        for a in (node_spec.actions or []):
            if isinstance(a, dict):
                action_files.append(ActionFile(**a))
            else:
                action_files.append(a)

        # Node-specific storage dirs (scoped to cluster+node)
        storage_prefix = f"./data/{cluster_spec.cluster_id}/{node_spec.node_id}"

        return BootstrapRequest(
            node_id=node_spec.node_id,
            listen=node_spec.listen,
            base_dir=cluster_spec.base_dir,
            runtime_entry=cluster_spec.runtime_entry,
            skills_md=node_spec.skills_md,
            actions=action_files,
            pip_packages=node_spec.pip_packages,
            extra_nodes=node_spec.extra_nodes,

            # Auth
            auth_token=node_spec.auth_token or gateway_token,
            allowed_tokens=allowed,

            # Gateway (worker nodes only)
            gateway_node_id=gw_node_id if gw_addr else None,
            gateway_address=gw_addr,
            gateway_auth_token=gw_auth_token or gateway_token,

            # LLM
            llm_api_key=llm_key,
            llm_model=llm_model or "claude-sonnet-4-20250514",
            llm_base_url=llm_base_url or "https://api.anthropic.com/v1",

            # EventBus
            event_bus_enabled=node_spec.event_bus_enabled,

            # Scheduler
            scheduler_enabled=node_spec.scheduler_enabled,
            schedule=node_spec.schedule or [],

            # Polling
            poll_interval_seconds=node_spec.poll_interval_seconds,
            poll_interval_max_seconds=node_spec.poll_interval_max_seconds,
            poll_backoff_multiplier=node_spec.poll_backoff_multiplier,

            # Task lifecycle
            task_pool_enabled=node_spec.task_pool_enabled,
            checkpoint_store_enabled=node_spec.checkpoint_store_enabled,
            checkpoint_store_path=f"{storage_prefix}/checkpoints/",

            # Session & memory (scoped per node)
            session_backend=node_spec.session_backend,
            session_storage_dir=f"{storage_prefix}/sessions",
            memory_enabled=node_spec.memory_enabled,
            memory_storage_dir=f"{storage_prefix}/memory",
        )

    async def _wire_subscriptions(
        self,
        node_addresses: list[str],
        auth_token: str | None = None,
    ) -> None:
        """POST standard subscriptions to each node in the cluster."""
        headers: dict[str, str] = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            for address in node_addresses:
                for sub in _STANDARD_SUBSCRIPTIONS:
                    sub_payload = {
                        "subscriber_node": _extract_node_id_from_address(address),
                        **sub,
                    }
                    for attempt in range(WIRE_RETRY_COUNT):
                        try:
                            resp = await client.post(f"{address}/subscribe", json=sub_payload)
                            if resp.status_code in (200, 201, 503):  # 503 = event bus disabled (ok for gw-only)
                                break
                            logger.warning(
                                "Wire subscription attempt %d to %s: HTTP %d",
                                attempt + 1, address, resp.status_code,
                            )
                        except httpx.HTTPError as exc:
                            logger.warning("Wire subscription attempt %d to %s failed: %s", attempt + 1, address, exc)
                        if attempt < WIRE_RETRY_COUNT - 1:
                            await asyncio.sleep(WIRE_RETRY_DELAY)

    async def _emit_kickoff(
        self,
        gateway_address: str,
        cluster_id: str,
        auth_token: str | None,
        prompt: str = "",
    ) -> None:
        """Emit cluster.started event to the gateway."""
        headers: dict[str, str] = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        payload = {
            "event_type": "cluster.started",
            "payload": {
                "cluster_id": cluster_id,
                "kickoff_prompt": prompt,
                "timestamp": time.time(),
            },
        }
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
                resp = await client.post(f"{gateway_address}/emit", json=payload)
                if resp.status_code == 200:
                    logger.info("cluster.started emitted for %s", cluster_id)
                else:
                    logger.warning("cluster.started emit returned HTTP %d", resp.status_code)
        except httpx.HTTPError as exc:
            logger.warning("Could not emit cluster.started: %s", exc)

    async def _shutdown_node(
        self,
        node_id: str,
        address: str | None,
        pid: int | None,
    ) -> bool:
        """Send POST /shutdown to a node; fall back to SIGTERM."""
        if address:
            headers: dict[str, str] = {}
            if self._seed_auth_token:
                headers["Authorization"] = f"Bearer {self._seed_auth_token}"
            try:
                async with httpx.AsyncClient(timeout=SHUTDOWN_HTTP_TIMEOUT, headers=headers) as client:
                    resp = await client.post(f"{address}/shutdown", json={"reason": "cluster_teardown"})
                    if resp.status_code in (200, 202):
                        logger.info("Node %s shut down via HTTP", node_id)
                        await asyncio.sleep(SHUTDOWN_GRACE_SECONDS)
                        return True
            except httpx.HTTPError as exc:
                logger.warning("HTTP shutdown failed for %s: %s — trying SIGTERM", node_id, exc)

        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                await asyncio.sleep(SHUTDOWN_GRACE_SECONDS)
                try:
                    os.kill(pid, 0)
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                logger.info("Node %s (pid=%d) terminated", node_id, pid)
                return True
            except ProcessLookupError:
                logger.info("Node %s (pid=%d) already gone", node_id, pid)
                return True
            except Exception as exc:
                logger.error("Failed to kill node %s (pid=%d): %s", node_id, pid, exc)
                return False

        logger.warning("No address or PID for node %s — cannot shut down", node_id)
        return False

    # ── State persistence ───────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Load cluster state from disk on startup."""
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            for cluster_dict in data.get("clusters", []):
                try:
                    info = ClusterInfo(**cluster_dict)
                    self._clusters[info.cluster_id] = info
                except Exception as exc:
                    logger.warning("Could not restore cluster: %s — %s", cluster_dict.get("cluster_id"), exc)
            logger.info("ClusterOrchestrator loaded %d cluster(s) from state", len(self._clusters))
        except Exception as exc:
            logger.error("Could not load cluster state from %s: %s", self._state_path, exc)

    async def _persist_state(self) -> None:
        """Write current cluster state to disk."""
        async with self._clusters_lock:
            clusters_list = [c.model_dump() for c in self._clusters.values()]
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"clusters": clusters_list}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("Could not persist cluster state: %s", exc)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _extract_node_id_from_address(address: str) -> str:
    """Extract a node_id hint from an HTTP address (for subscription wiring)."""
    # We don't know the node_id from address alone — use address as fallback
    # The subscriber_node will be corrected by the receiving node anyway
    return address.replace("http://", "").replace("https://", "").replace(":", "-").replace("/", "")
