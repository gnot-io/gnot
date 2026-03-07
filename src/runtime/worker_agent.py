"""Worker agent — background service running inside each worker node.

On startup the worker agent:
  1. Registers itself with the gateway (POST /nodes/register).
  2. Starts a heartbeat loop — POSTs to gateway every heartbeat_interval_seconds.
  3. Starts a poll loop — GETs /jobs/poll, claims + executes unclaimed jobs,
     reports results back to the gateway.

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

    This object is created and started by node_runtime.py when the node
    is configured as a worker (has gateway_node_id set).
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
        self._gateway_url = config.gateway_address.rstrip("/")
        self._node_id = config.node_id
        self._self_address = config.self_address  # may be None if behind NAT
        # v5.10: self-address for re-routing _mesh_forward jobs
        self._self_url: str = config.self_address or f"http://localhost:{config.port}"
        # v5.10: sub-nodes that have registered with this node
        self._sub_routes: dict[str, SubRouteInfo] = {}  # v5.13: {sub_node_id: SubRouteInfo}
        self._sub_routes_lock = asyncio.Lock()

        self._heartbeat_interval = config.heartbeat_interval_seconds
        self._poll_interval = config.poll_interval_seconds

        self._auth_headers: dict[str, str] = {}
        # v5.13: use gateway_auth_token when calling gateway (per-node tokens);
        # falls back to auth_token for backward compat.
        _outbound_token = getattr(config, 'gateway_auth_token', None) or config.auth_token
        if _outbound_token:
            self._auth_headers["Authorization"] = f"Bearer {_outbound_token}"

        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Register with gateway and start background loops."""
        self._running = True
        await self._register_with_retry()
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="worker-heartbeat"),
            asyncio.create_task(self._poll_loop(), name="worker-poll"),
            asyncio.create_task(self._reregister_loop(), name="worker-reregister"),
        ]
        logger.info(
            "WorkerAgent started for %s → gateway %s",
            self._node_id, self._gateway_url,
        )

    async def stop(self) -> None:
        """Cancel all background tasks gracefully."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("WorkerAgent stopped for %s", self._node_id)

    # -- registration -------------------------------------------------------

    async def _register_with_retry(self) -> None:
        """Attempt to register with the gateway, retrying on failure."""
        for attempt in range(1, MAX_REGISTER_RETRIES + 1):
            try:
                await self._register()
                return
            except Exception as exc:
                logger.warning(
                    "Registration attempt %d/%d failed: %s",
                    attempt, MAX_REGISTER_RETRIES, exc,
                )
                if attempt < MAX_REGISTER_RETRIES:
                    await asyncio.sleep(REGISTER_RETRY_DELAY_SECONDS)

        logger.error(
            "Could not register with gateway after %d attempts — "
            "heartbeat/poll loops will still run and retry registration",
            MAX_REGISTER_RETRIES,
        )

    async def _register(self) -> None:
        """Register with gateway, advertising own actions + all known sub-routes."""
        url = f"{self._gateway_url}/nodes/register"
        payload: dict[str, Any] = {"node_id": self._node_id}
        if self._self_address:
            payload["address"] = self._self_address

        # v5.10 — BGP-style advertisement
        own_actions = list(self._action_registry.keys())
        payload["actions"] = own_actions

        # v5.11: include action specs (description + caller credential requirements)
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
            # v5.13: include actions + action_specs per sub-route
            cap: dict[str, list[str]] = {}
            sub_specs: dict[str, dict] = {}
            for sub_id, info in sub_routes_snapshot.items():
                cap[sub_id] = info.actions
                if info.action_specs:
                    sub_specs[sub_id] = info.action_specs
            payload["capabilities"] = cap
            if sub_specs:
                payload["sub_route_specs"] = sub_specs  # v5.13: action_specs per sub-node

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload, headers=self._auth_headers)
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                "Registered with gateway: node=%s actions=%s sub_routes=%s",
                self._node_id, own_actions, list(sub_routes_snapshot.keys()),
            )

    async def add_sub_route(
        self,
        sub_node_id: str,
        sub_actions: list[str],
        action_specs: dict[str, dict] | None = None,  # v5.13
    ) -> None:
        """Called when a sub-node registers with this node.

        Updates local sub-route table and re-advertises to our own gateway
        so the capability tree propagates upward.
        v5.13: also carries action_specs so full specs cascade upward.
        """
        async with self._sub_routes_lock:
            self._sub_routes[sub_node_id] = SubRouteInfo(
                actions=list(sub_actions),
                action_specs=dict(action_specs or {}),
            )
        logger.info(
            "Sub-route added: %s → %s (actions=%s, specs=%d) — re-advertising to gateway",
            self._node_id, sub_node_id, sub_actions, len(action_specs or {}),
        )
        # Re-advertise upward so parent gateway learns about the new sub-node
        try:
            await self._register()
        except Exception as exc:
            logger.warning("Re-advertisement failed after sub-node %s joined: %s", sub_node_id, exc)

    # -- heartbeat loop -----------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                await self._send_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Heartbeat failed: %s", exc)
            await asyncio.sleep(self._heartbeat_interval)

    async def _reregister_loop(self) -> None:
        """Periodic re-registration to trigger BGP WITHDRAW cascade (v5.12).

        Every N heartbeat intervals, re-register advertising only ONLINE sub-nodes.
        Gateway's /nodes/register handler calls withdraw_routes() to remove stale
        entries — implementing route withdrawal without explicit WITHDRAW message.
        """
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
        """Re-register advertising only sub-nodes that are still ONLINE."""
        async with self._sub_routes_lock:
            full_routes = dict(self._sub_routes)

        live_routes: dict[str, SubRouteInfo] = {}
        registry = getattr(self, "_local_node_registry", None)
        for sub_id, info in full_routes.items():
            if registry is not None:
                from runtime.models import NodeStatus
                status = await registry.get_status(sub_id)
                if status == NodeStatus.UNREACHABLE:
                    logger.info(
                        "Excluding stale sub-route %s from re-advertisement (UNREACHABLE)",
                        sub_id,
                    )
                    continue
            live_routes[sub_id] = info

        # Temporarily use live_routes for this register call
        async with self._sub_routes_lock:
            saved = dict(self._sub_routes)
            self._sub_routes = live_routes
        try:
            await self._register()
            logger.debug(
                "Periodic re-registration: advertising %d/%d sub-routes",
                len(live_routes), len(full_routes),
            )
        finally:
            async with self._sub_routes_lock:
                self._sub_routes = saved

    async def _send_heartbeat(self) -> None:
        url = f"{self._gateway_url}/nodes/{self._node_id}/heartbeat"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                json={"node_id": self._node_id},
                headers=self._auth_headers,
            )
            resp.raise_for_status()
        logger.debug("Heartbeat sent → gateway")

    # -- poll loop ----------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                jobs = await self._poll_jobs()
                for job in jobs:
                    # Fire-and-forget — don't block poll loop on execution
                    asyncio.create_task(
                        self._claim_and_execute(job),
                        name=f"exec-{job.job_id}",
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Poll error: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def _poll_jobs(self) -> list[QueuedJob]:
        url = f"{self._gateway_url}/jobs/poll"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                url,
                params={"node_id": self._node_id},
                headers=self._auth_headers,
            )
            resp.raise_for_status()
            data = resp.json()
        jobs = [QueuedJob(**j) for j in data.get("jobs", [])]
        if jobs:
            logger.info("Polled %d job(s) from gateway", len(jobs))
        return jobs

    # -- claim + execute ----------------------------------------------------

    async def _claim_and_execute(self, job: QueuedJob) -> None:
        """Claim a polled job, execute it, then report the result."""

        # 1. Claim
        claimed = await self._claim_job(job.job_id)
        if not claimed:
            logger.debug("Job %s already claimed by another worker", job.job_id)
            return

        logger.info(
            "Executing pulled job %s (action=%s task=%s)",
            job.job_id, job.action, job.task_id,
        )

        # 2. Execute via local ActionExecutor (or re-route for _mesh_forward)
        try:
            if job.action == "_mesh_forward":
                response = await self._handle_mesh_forward(job)
            else:
                response = await self._executor.execute(
                    action_name=job.action,
                    params=job.params,
                    task_id=job.task_id,
                    caller_token=job.caller_token,
                    caller_credentials=dict(job.caller_credentials),
                )
            # If the action itself is async, wait for completion
            if hasattr(response, "job_id"):
                output, error = await self._await_local_job(response.job_id)
            else:
                output = response.output  # type: ignore[union-attr]
                error = None
            status = "completed" if error is None else "failed"
        except Exception as exc:
            logger.exception("Job %s execution raised exception", job.job_id)
            output = None
            error = str(exc)
            status = "failed"

        # 3. Report result to gateway
        await self._report_result(job.job_id, status, output, error)

    async def _handle_mesh_forward(self, job: "QueuedJob") -> Any:
        """Handle a _mesh_forward job: POST the original action to this node's own /action.

        The GatewayRouter on THIS node will route it onward to the real target.
        This is how pull-mode next-hop forwarding works: the intermediate node
        receives the forwarding instruction, then runs its own routing logic.
        """
        from runtime.models import SyncActionResponse

        original_target = job.params.get("_mesh_forward_target", "")
        original_action = job.params.get("action", "")
        original_params = {k: v for k, v in job.params.items()
                           if k not in ("_mesh_forward_target", "action")}

        logger.info(
            "Forwarding job %s → target=%s action=%s via local /action",
            job.job_id, original_target, original_action,
        )

        payload = {
            "target_node_id": original_target,
            "payload": {"action": original_action, "params": original_params},
            "trace": {"hop_count": 0, "route_path": []},
        }

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS * 6) as client:
                resp = await client.post(
                    f"{self._self_url}/action",
                    json=payload,
                    headers=self._auth_headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"_mesh_forward HTTP error: {exc}") from exc

        # Sync result — pass through directly
        if "output" in data and "job_id" not in data:
            return SyncActionResponse(
                task_id=job.task_id,
                output=data.get("output", {}),
            )

        # v5.12 — Async downstream job: poll-and-relay.
        # The intermediate node polls its OWN job queue until the downstream
        # action completes, then reports the REAL result to the original gateway.
        # This makes the entire pull-chain transparent to the top-level caller.
        downstream_job_id = data.get("job_id")
        if downstream_job_id:
            logger.info(
                "Poll-and-relay: waiting for downstream job %s (target=%s)",
                downstream_job_id, original_target,
            )
            output, error = await self._await_downstream_job(downstream_job_id)
            if error:
                raise RuntimeError(f"Downstream job {downstream_job_id} failed: {error}")
            return SyncActionResponse(task_id=job.task_id, output=output or {})

        # Unknown response format — return as-is
        return SyncActionResponse(task_id=job.task_id, output=data)

    async def _await_downstream_job(
        self, job_id: str, poll_interval: float = 1.0, max_wait: float | None = None
    ) -> tuple[dict | None, str | None]:
        """Poll for a job on this node's gateway's job queue until it completes.

        Used by pull-and-relay chain: intermediate node polls the local job
        spawned by the forward, then relays real output upstream.

        Differs from _await_local_job: uses HTTP GET /result/{job_id} on self
        rather than in-process job_manager lookup (downstream job may live on
        this node's own gateway router's job_manager).
        """
        max_wait = max_wait or getattr(self._config, "pull_job_timeout_seconds", 300)
        from runtime.job_manager import JobStatus
        waited = 0.0
        while waited < max_wait:
            await asyncio.sleep(poll_interval)
            waited += poll_interval
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    resp = await client.get(
                        f"{self._self_url}/result/{job_id}",
                        headers=self._auth_headers,
                    )
                    if resp.status_code == 404:
                        return None, f"job {job_id} not found on local gateway"
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPError as exc:
                logger.warning("poll downstream job %s: HTTP error %s", job_id, exc)
                continue

            status = data.get("status", "")
            if status == "completed":
                return data.get("output"), None
            if status == "failed":
                return None, data.get("error", "downstream job failed")
            # still running: queued / running / accepted
            logger.debug("downstream job %s status=%s, waiting %.0fs...", job_id, status, waited)

        return None, f"downstream job {job_id} timed out after {max_wait}s"

    async def _claim_job(self, job_id: str) -> bool:
        """POST /jobs/{job_id}/claim — returns True if claim succeeded."""
        url = f"{self._gateway_url}/jobs/{job_id}/claim"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    url,
                    json={"node_id": self._node_id},
                    headers=self._auth_headers,
                )
                resp.raise_for_status()
                return resp.json().get("claimed", False)
        except httpx.HTTPError as exc:
            logger.warning("Claim failed for %s: %s", job_id, exc)
            return False

    async def _await_local_job(
        self, local_job_id: str, poll_interval: float = 1.0, max_wait: float = 600.0
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Wait for a locally-spawned async job to finish."""
        from runtime.job_manager import JobStatus
        waited = 0.0
        while waited < max_wait:
            await asyncio.sleep(poll_interval)
            waited += poll_interval
            # Access executor's job_manager via the executor's internal reference
            job = await self._executor._job_manager.get_job(local_job_id)
            if job is None:
                return None, "local job disappeared"
            if job.status == JobStatus.COMPLETED:
                return job.output, None
            if job.status == JobStatus.FAILED:
                return None, job.error or "unknown error"
        return None, f"local job timed out after {max_wait}s"

    async def _report_result(
        self,
        job_id: str,
        status: str,
        output: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        url = f"{self._gateway_url}/jobs/{job_id}/result"
        payload: dict[str, Any] = {
            "node_id": self._node_id,
            "status": status,
        }
        if output is not None:
            payload["output"] = output
        if error is not None:
            payload["error"] = error

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    url, json=payload, headers=self._auth_headers
                )
                resp.raise_for_status()
            logger.info("Result reported for job %s → %s", job_id, status)
        except httpx.HTTPError as exc:
            logger.error("Failed to report result for job %s: %s", job_id, exc)
