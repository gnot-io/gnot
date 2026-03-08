"""Worker agent — background service running inside each worker node.

On startup the worker agent:
  1. Registers itself with the gateway (POST /nodes/register).
  2. Starts a heartbeat loop — POSTs to gateway every heartbeat_interval_seconds.
  3. Starts a poll loop — GETs /jobs/poll, claims + executes unclaimed jobs,
     reports results back to the gateway.

v6.0 Phase 2 changes:
  - Multi-gateway support: WorkerAgent now manages multiple GatewayConnection
    instances (one per gateway), delegating all per-gateway logic to them.
  - Adaptive polling: moved to GatewayConnection (backoff on idle, reset on job).
  - Primary gateway from config.gateway_address, additional gateways from
    config.additional_gateways (list of {address, auth_token}).
  - WorkerAgent retains backward-compat: still works as single-gateway agent.

The agent runs as asyncio background tasks, started by node_runtime.py when
the node config contains a gateway_node_id.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from dataclasses import dataclass, field

from runtime.action_executor import ActionExecutor
from runtime.config import NodeConfig
from runtime.models import QueuedJob

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HEARTBEAT_INTERVAL_SECONDS: int = 10
DEFAULT_POLL_INTERVAL_SECONDS: int = 5
REGISTER_RETRY_DELAY_SECONDS: float = 5.0
MAX_REGISTER_RETRIES: int = 12   # ~60 seconds total before giving up
HTTP_TIMEOUT_SECONDS: float = 10.0
# v5.12 — periodic re-registration multiplier
RE_REGISTER_INTERVAL_MULTIPLIER: int = 6  # re-register every N heartbeat intervals


# ---------------------------------------------------------------------------
# Sub-route info — carries actions + action_specs through advertisement chain
# ---------------------------------------------------------------------------

@dataclass
class SubRouteInfo:
    """v5.13: actions + action_specs for one advertised sub-node."""
    actions: list[str] = field(default_factory=list)
    action_specs: dict[str, dict] = field(default_factory=dict)  # {action: spec_dict}


# ---------------------------------------------------------------------------
# Worker Agent
# ---------------------------------------------------------------------------

class WorkerAgent:
    """Manages gateway communication for a worker node.

    v6.0: Orchestrates multiple GatewayConnection instances — one per gateway.
    Primary gateway: config.gateway_address
    Additional gateways: config.additional_gateways (list of {address, auth_token})

    Backward compatible: single-gateway deployments work unchanged.
    """

    def __init__(
        self,
        config: NodeConfig,
        executor: ActionExecutor,
        action_registry: dict | None = None,
        schema_registry: dict | None = None,
    ) -> None:
        if not config.gateway_address:
            raise ValueError("WorkerAgent requires gateway_address in config")

        self._config = config
        self._executor = executor
        self._action_registry = action_registry or {}
        self._schema_registry = schema_registry or {}
        self._node_id = config.node_id
        self._self_address = config.self_address
        self._self_url: str = config.self_address or f"http://localhost:{config.port}"

        # v5.10: sub-nodes that have registered with this node
        self._sub_routes: dict[str, SubRouteInfo] = {}
        self._sub_routes_lock = asyncio.Lock()

        # Build primary auth headers
        _outbound_token = getattr(config, 'gateway_auth_token', None) or config.auth_token
        self._primary_auth_headers: dict[str, str] = {}
        if _outbound_token:
            self._primary_auth_headers["Authorization"] = f"Bearer {_outbound_token}"

        self._running = False
        self._connections: list = []          # list[GatewayConnection]

        # Legacy attrs for backward compat with code that reads these
        self._gateway_url = config.gateway_address.rstrip("/")
        self._auth_headers = self._primary_auth_headers
        self._heartbeat_interval = config.heartbeat_interval_seconds
        self._poll_interval = config.poll_interval_seconds
        self._tasks: list[asyncio.Task[None]] = []

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Register with all gateways and start background loops."""
        self._running = True

        from runtime.gateway_connection import GatewayConnection

        # Build list of (gateway_url, auth_headers, label) tuples
        gateways: list[tuple[str, dict[str, str], str]] = [
            (
                self._config.gateway_address.rstrip("/"),  # type: ignore[union-attr]
                self._primary_auth_headers,
                "primary",
            )
        ]

        # v6.0: additional_gateways from config
        for i, gw_cfg in enumerate(getattr(self._config, "additional_gateways", []) or []):
            gw_url = gw_cfg.get("address", "").rstrip("/")
            gw_token = gw_cfg.get("auth_token") or self._config.auth_token
            gw_headers: dict[str, str] = {}
            if gw_token:
                gw_headers["Authorization"] = f"Bearer {gw_token}"
            gateways.append((gw_url, gw_headers, f"additional-{i}"))

        # Adaptive poll config (v6.0; fall back to v5.x defaults)
        poll_interval_max = getattr(self._config, "poll_interval_max_seconds", 60)
        poll_backoff = getattr(self._config, "poll_backoff_multiplier", 1.5)

        self._connections = []
        for gw_url, gw_headers, label in gateways:
            conn = GatewayConnection(
                gateway_url=gw_url,
                gateway_label=label,
                node_id=self._node_id,
                node_config=self._config,
                executor=self._executor,
                action_registry=self._action_registry,
                schema_registry=self._schema_registry,
                self_address=self._self_address,
                auth_headers=gw_headers,
                heartbeat_interval=self._config.heartbeat_interval_seconds,
                poll_interval=self._config.poll_interval_seconds,
                poll_interval_max=poll_interval_max,
                poll_backoff_multiplier=poll_backoff,
                sub_routes_getter=lambda: self._sub_routes,
                sub_routes_lock=self._sub_routes_lock,
                local_node_registry=getattr(self, "_local_node_registry", None),
            )
            await conn.start()
            self._connections.append(conn)

        logger.info(
            "WorkerAgent started for %s → %d gateway(s)",
            self._node_id, len(self._connections),
        )

    async def stop(self) -> None:
        """Cancel all gateway connections and background tasks."""
        self._running = False
        for conn in self._connections:
            await conn.stop()
        self._connections.clear()
        # Legacy task cleanup for any directly-spawned tasks
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("WorkerAgent stopped for %s", self._node_id)

    # -- sub-route management (kept on WorkerAgent, used by all connections) --

    async def connect_to_gateway(
        self,
        gateway_address: str,
        auth_token: str | None = None,
    ) -> str:
        """Runtime gateway join — connect to a new gateway without restart.

        v6.0 Phase 5: Called by POST /gateways/connect endpoint.
        Creates and starts a new GatewayConnection for the given address.

        Args:
            gateway_address: Full HTTP URL of the gateway (e.g. "http://localhost:8090")
            auth_token:      Bearer token to use when calling the gateway.
                             Falls back to primary auth_token if not set.

        Returns:
            Gateway node_id after successful registration.

        Raises:
            RuntimeError if registration fails.
        """
        from runtime.gateway_connection import GatewayConnection

        gw_url = gateway_address.rstrip("/")
        token = auth_token or self._config.auth_token
        gw_headers: dict[str, str] = {}
        if token:
            gw_headers["Authorization"] = f"Bearer {token}"

        poll_interval_max = getattr(self._config, "poll_interval_max_seconds", 60)
        poll_backoff = getattr(self._config, "poll_backoff_multiplier", 1.5)
        label = f"runtime-{len(self._connections)}"

        conn = GatewayConnection(
            gateway_url=gw_url,
            gateway_label=label,
            node_id=self._node_id,
            node_config=self._config,
            executor=self._executor,
            action_registry=self._action_registry,
            schema_registry=self._schema_registry,
            self_address=self._self_address,
            auth_headers=gw_headers,
            heartbeat_interval=self._config.heartbeat_interval_seconds,
            poll_interval=self._config.poll_interval_seconds,
            poll_interval_max=poll_interval_max,
            poll_backoff_multiplier=poll_backoff,
            sub_routes_getter=lambda: self._sub_routes,
            sub_routes_lock=self._sub_routes_lock,
            local_node_registry=getattr(self, "_local_node_registry", None),
        )

        await conn.start()
        self._connections.append(conn)

        logger.info(
            "WorkerAgent[%s] connected to new gateway %s (label=%s) — total gateways: %d",
            self._node_id, gw_url, label, len(self._connections),
        )

        # Try to resolve the gateway node_id from /health
        gw_node_id = gw_url
        try:
            async with httpx.AsyncClient(timeout=5.0, headers=gw_headers) as client:
                resp = await client.get(f"{gw_url}/health")
                if resp.status_code == 200:
                    data = resp.json()
                    gw_node_id = data.get("node_id", gw_url)
        except httpx.HTTPError:
            pass

        return gw_node_id

    async def add_sub_route(
        self,
        sub_node_id: str,
        sub_actions: list[str],
        action_specs: dict[str, dict] | None = None,
    ) -> None:
        """Called when a sub-node registers with this node.

        Updates local sub-route table and re-advertises to ALL gateways.
        v5.13: also carries action_specs so full specs cascade upward.
        """
        async with self._sub_routes_lock:
            self._sub_routes[sub_node_id] = SubRouteInfo(
                actions=list(sub_actions),
                action_specs=dict(action_specs or {}),
            )
        logger.info(
            "Sub-route added: %s → %s (actions=%s, specs=%d) — re-advertising to %d gateway(s)",
            self._node_id, sub_node_id, sub_actions,
            len(action_specs or {}), len(self._connections),
        )
        for conn in self._connections:
            try:
                await conn._register()
            except Exception as exc:
                logger.warning(
                    "Re-advertisement to [%s] failed after sub-node %s joined: %s",
                    conn._label, sub_node_id, exc,
                )

    # -- legacy poll interval property (backward compat) -------------------

    @property
    def poll_interval(self) -> float:
        """Current poll interval of the primary connection (for observability)."""
        if self._connections:
            return self._connections[0].current_poll_interval
        return float(self._poll_interval)

    # -- Legacy methods kept for backward compatibility --------------------
    # (These delegate to the primary GatewayConnection when available, or
    #  fall back to direct HTTP when no connections are started yet.)

    async def _register_with_retry(self) -> None:
        if self._connections:
            await self._connections[0]._register_with_retry()
        else:
            await self._legacy_register_with_retry()

    async def _legacy_register_with_retry(self) -> None:
        for attempt in range(1, MAX_REGISTER_RETRIES + 1):
            try:
                await self._register()
                return
            except Exception as exc:
                logger.warning("Registration attempt %d/%d failed: %s", attempt, MAX_REGISTER_RETRIES, exc)
                if attempt < MAX_REGISTER_RETRIES:
                    await asyncio.sleep(REGISTER_RETRY_DELAY_SECONDS)
        logger.error("Could not register with gateway after %d attempts", MAX_REGISTER_RETRIES)

    async def _register(self) -> None:
        if self._connections:
            await self._connections[0]._register()
            return
        # Fallback: direct registration before connections are created
        url = f"{self._gateway_url}/nodes/register"
        payload: dict[str, Any] = {"node_id": self._node_id}
        if self._self_address:
            payload["address"] = self._self_address
        payload["actions"] = list(self._action_registry.keys())

        if self._schema_registry:
            from runtime.schema_validator import ActionSchemaValidator
            validator = ActionSchemaValidator(self._schema_registry)
            specs = validator.build_all_action_specs(self._action_registry)
            payload["action_specs"] = {
                name: spec.model_dump() for name, spec in specs.items()
            }

        async with self._sub_routes_lock:
            sub_routes_snapshot = dict(self._sub_routes)

        if sub_routes_snapshot:
            payload["advertise_routes"] = list(sub_routes_snapshot.keys())
            cap: dict[str, list[str]] = {}
            sub_specs: dict[str, dict] = {}
            for sub_id, info in sub_routes_snapshot.items():
                cap[sub_id] = info.actions
                if info.action_specs:
                    sub_specs[sub_id] = info.action_specs
            payload["capabilities"] = cap
            if sub_specs:
                payload["sub_route_specs"] = sub_specs

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload, headers=self._auth_headers)
            resp.raise_for_status()

    async def _heartbeat_loop(self) -> None:
        # Legacy — handled by GatewayConnection in v6.0
        while self._running:
            try:
                await self._send_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Heartbeat failed: %s", exc)
            await asyncio.sleep(self._heartbeat_interval)

    async def _reregister_loop(self) -> None:
        interval = self._heartbeat_interval * RE_REGISTER_INTERVAL_MULTIPLIER
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self._register_with_live_routes()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Periodic re-registration failed: %s", exc)

    async def _register_with_live_routes(self) -> None:
        if self._connections:
            for conn in self._connections:
                try:
                    await conn._register_with_live_routes()
                except Exception as exc:
                    logger.warning("Live-route re-register [%s] failed: %s", conn._label, exc)

    async def _send_heartbeat(self) -> None:
        url = f"{self._gateway_url}/nodes/{self._node_id}/heartbeat"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json={"node_id": self._node_id}, headers=self._auth_headers)
            resp.raise_for_status()

    async def _poll_loop(self) -> None:
        # Legacy — handled by GatewayConnection in v6.0
        while self._running:
            try:
                jobs = await self._poll_jobs()
                for job in jobs:
                    asyncio.create_task(self._claim_and_execute(job), name=f"exec-{job.job_id}")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Poll error: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def _poll_jobs(self) -> list[QueuedJob]:
        url = f"{self._gateway_url}/jobs/poll"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, params={"node_id": self._node_id}, headers=self._auth_headers)
            resp.raise_for_status()
            data = resp.json()
        return [QueuedJob(**j) for j in data.get("jobs", [])]

    async def _claim_and_execute(self, job: QueuedJob) -> None:
        # Delegate to primary connection if available
        if self._connections:
            await self._connections[0]._claim_and_execute(job)

    async def _claim_job(self, job_id: str) -> bool:
        if self._connections:
            return await self._connections[0]._claim_job(job_id)
        return False

    async def _await_local_job(
        self, local_job_id: str, poll_interval: float = 1.0, max_wait: float = 600.0
    ) -> tuple[dict[str, Any] | None, str | None]:
        if self._connections:
            return await self._connections[0]._await_local_job(local_job_id, poll_interval, max_wait)
        return None, "no connections"

    async def _report_result(
        self, job_id: str, status: str, output: dict | None, error: str | None
    ) -> None:
        if self._connections:
            await self._connections[0]._report_result(job_id, status, output, error)

    async def _handle_mesh_forward(self, job: "QueuedJob") -> Any:
        """Backward-compat delegation to primary GatewayConnection."""
        if self._connections:
            return await self._connections[0]._handle_mesh_forward(job)
        # Fallback: direct implementation (pre-connections state)
        from runtime.models import SyncActionResponse
        self_url = self._self_url
        original_target = job.params.get("_mesh_forward_target", "")
        original_action = job.params.get("action", "")
        original_params = {k: v for k, v in job.params.items()
                           if k not in ("_mesh_forward_target", "action")}
        payload = {
            "target_node_id": original_target,
            "payload": {"action": original_action, "params": original_params},
            "trace": {"hop_count": 0, "route_path": []},
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS * 6) as client:
            resp = await client.post(
                f"{self_url}/action", json=payload, headers=self._auth_headers
            )
            resp.raise_for_status()
            data = resp.json()
        if "output" in data and "job_id" not in data:
            return SyncActionResponse(task_id=job.task_id, output=data.get("output", {}))
        downstream_job_id = data.get("job_id")
        if downstream_job_id:
            output, error = await self._await_downstream_job(downstream_job_id)
            if error:
                raise RuntimeError(f"Downstream job {downstream_job_id} failed: {error}")
            return SyncActionResponse(task_id=job.task_id, output=output or {})
        return SyncActionResponse(task_id=job.task_id, output=data)

    async def _await_downstream_job(
        self, job_id: str, poll_interval: float = 1.0
    ) -> tuple[dict | None, str | None]:
        """Backward-compat: poll downstream job on self_url."""
        if self._connections:
            return await self._connections[0]._await_downstream_job(job_id, poll_interval)
        max_wait = float(getattr(self._config, "pull_job_timeout_seconds", 300))
        waited = 0.0
        while waited < max_wait:
            await asyncio.sleep(poll_interval)
            waited += poll_interval
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    resp = await client.get(
                        f"{self._self_url}/result/{job_id}", headers=self._auth_headers
                    )
                    if resp.status_code == 404:
                        return None, f"job {job_id} not found"
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPError as exc:
                logger.warning("poll downstream job %s: %s", job_id, exc)
                continue
            status = data.get("status", "")
            if status == "completed":
                return data.get("output"), None
            if status == "failed":
                return None, data.get("error", "downstream job failed")
        return None, f"downstream job {job_id} timed out after {max_wait}s"
