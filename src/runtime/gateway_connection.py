"""GatewayConnection — per-gateway register + heartbeat + poll lifecycle.

v6.0 Phase 2 refactor: extracted from WorkerAgent to support multi-gateway
membership. Each GatewayConnection manages one gateway independently.
WorkerAgent becomes an orchestrator that holds N GatewayConnections.

Adaptive polling (Level 2 enhancement):
  - Base interval: poll_interval_seconds (default 5s)
  - Empty poll → interval × backoff_multiplier (default 1.5)
  - Maximum: poll_interval_max_seconds (default 60s)
  - Job received → reset to base interval
  Reduces ~70% unnecessary HTTP calls when cluster is idle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from runtime.action_executor import ActionExecutor
from runtime.config import NodeConfig
from runtime.models import QueuedJob

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTER_RETRY_DELAY_SECONDS: float = 5.0
MAX_REGISTER_RETRIES: int = 12
HTTP_TIMEOUT_SECONDS: float = 10.0
RE_REGISTER_INTERVAL_MULTIPLIER: int = 6


class GatewayConnection:
    """Manages register + heartbeat + pull-poll lifecycle for ONE gateway.

    Created by WorkerAgent for each gateway the node should join.
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        gateway_label: str,          # human-readable label (e.g. "primary", "gateway-B")
        node_id: str,
        node_config: NodeConfig,
        executor: ActionExecutor,
        action_registry: dict,
        schema_registry: dict,
        self_address: str | None,
        auth_headers: dict[str, str],
        heartbeat_interval: int,
        poll_interval: int,
        poll_interval_max: int,
        poll_backoff_multiplier: float,
        sub_routes_getter,           # callable: () -> dict[str, SubRouteInfo]
        sub_routes_lock: asyncio.Lock,
        local_node_registry=None,
    ) -> None:
        self._gateway_url = gateway_url.rstrip("/")
        self._label = gateway_label
        self._node_id = node_id
        self._config = node_config
        self._executor = executor
        self._action_registry = action_registry
        self._schema_registry = schema_registry
        self._self_address = self_address
        self._auth_headers = dict(auth_headers)
        self._heartbeat_interval = heartbeat_interval
        self._poll_interval_base = poll_interval
        self._poll_interval_max = poll_interval_max
        self._poll_backoff = poll_backoff_multiplier
        self._sub_routes_getter = sub_routes_getter
        self._sub_routes_lock = sub_routes_lock
        self._local_registry = local_node_registry

        self._running = False
        self._tasks: list[asyncio.Task] = []

        # Adaptive poll state
        self._current_poll_interval = float(poll_interval)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Register with gateway and start background loops."""
        self._running = True
        await self._register_with_retry()
        self._tasks = [
            asyncio.create_task(
                self._heartbeat_loop(),
                name=f"heartbeat-{self._label}-{self._node_id}",
            ),
            asyncio.create_task(
                self._poll_loop(),
                name=f"poll-{self._label}-{self._node_id}",
            ),
            asyncio.create_task(
                self._reregister_loop(),
                name=f"reregister-{self._label}-{self._node_id}",
            ),
        ]
        logger.info(
            "GatewayConnection started: node=%s → gateway=%s (%s)",
            self._node_id, self._gateway_url, self._label,
        )

    async def stop(self) -> None:
        """Cancel all background tasks."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info(
            "GatewayConnection stopped: node=%s → gateway=%s (%s)",
            self._node_id, self._gateway_url, self._label,
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def _register_with_retry(self) -> None:
        for attempt in range(1, MAX_REGISTER_RETRIES + 1):
            try:
                await self._register()
                return
            except Exception as exc:
                logger.warning(
                    "GatewayConnection [%s] register attempt %d/%d failed: %s",
                    self._label, attempt, MAX_REGISTER_RETRIES, exc,
                )
                if attempt < MAX_REGISTER_RETRIES:
                    await asyncio.sleep(REGISTER_RETRY_DELAY_SECONDS)
        logger.error(
            "GatewayConnection [%s] could not register after %d attempts",
            self._label, MAX_REGISTER_RETRIES,
        )

    async def _register(self) -> None:
        url = f"{self._gateway_url}/nodes/register"
        payload: dict[str, Any] = {"node_id": self._node_id}
        if self._self_address:
            payload["address"] = self._self_address

        own_actions = list(self._action_registry.keys())
        payload["actions"] = own_actions

        if self._schema_registry:
            from runtime.schema_validator import ActionSchemaValidator
            validator = ActionSchemaValidator(self._schema_registry)
            specs = validator.build_all_action_specs(self._action_registry)
            payload["action_specs"] = {
                name: spec.model_dump() for name, spec in specs.items()
            }

        async with self._sub_routes_lock:
            sub_routes_snapshot = dict(self._sub_routes_getter())

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
        logger.info(
            "GatewayConnection [%s] registered: node=%s actions=%s",
            self._label, self._node_id, own_actions,
        )

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                await self._send_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Heartbeat [%s] failed: %s", self._label, exc)
            await asyncio.sleep(self._heartbeat_interval)

    async def _send_heartbeat(self) -> None:
        url = f"{self._gateway_url}/nodes/{self._node_id}/heartbeat"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                json={"node_id": self._node_id},
                headers=self._auth_headers,
            )
            resp.raise_for_status()
        logger.debug("Heartbeat sent [%s]", self._label)

    # ------------------------------------------------------------------
    # Re-register loop (BGP-style liveness cascade)
    # ------------------------------------------------------------------

    async def _reregister_loop(self) -> None:
        interval = self._heartbeat_interval * RE_REGISTER_INTERVAL_MULTIPLIER
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self._register_with_live_routes()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Re-register [%s] failed: %s", self._label, exc)

    async def _register_with_live_routes(self) -> None:
        """Re-register advertising only ONLINE sub-nodes."""
        async with self._sub_routes_lock:
            full_routes = dict(self._sub_routes_getter())

        live_routes = {}
        for sub_id, info in full_routes.items():
            if self._local_registry is not None:
                from runtime.models import NodeStatus
                status = await self._local_registry.get_status(sub_id)
                if status == NodeStatus.UNREACHABLE:
                    logger.info(
                        "[%s] Excluding stale sub-route %s (UNREACHABLE)",
                        self._label, sub_id,
                    )
                    continue
            live_routes[sub_id] = info

        # Temporarily swap sub_routes for this register call
        async with self._sub_routes_lock:
            original = self._sub_routes_getter()

        # Build a temporary snapshot object
        _snapshot_func = lambda: live_routes  # noqa: E731
        saved_getter = self._sub_routes_getter
        self._sub_routes_getter = _snapshot_func
        try:
            await self._register()
        finally:
            self._sub_routes_getter = saved_getter

    # ------------------------------------------------------------------
    # Adaptive poll loop (Level 2 autonomy)
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Poll for jobs with exponential backoff when idle."""
        self._current_poll_interval = float(self._poll_interval_base)

        while self._running:
            try:
                jobs = await self._poll_jobs()
                if jobs:
                    # Jobs found → reset interval to base
                    self._current_poll_interval = float(self._poll_interval_base)
                    for job in jobs:
                        asyncio.create_task(
                            self._claim_and_execute(job),
                            name=f"exec-{self._label}-{job.job_id}",
                        )
                else:
                    # No jobs → apply backoff
                    new_interval = min(
                        self._current_poll_interval * self._poll_backoff,
                        float(self._poll_interval_max),
                    )
                    if new_interval != self._current_poll_interval:
                        logger.debug(
                            "Adaptive poll [%s]: backoff %.1f → %.1f s",
                            self._label, self._current_poll_interval, new_interval,
                        )
                    self._current_poll_interval = new_interval

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Poll error [%s]: %s", self._label, exc)

            try:
                await asyncio.sleep(self._current_poll_interval)
            except asyncio.CancelledError:
                break

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
            logger.info("Polled %d job(s) from gateway [%s]", len(jobs), self._label)
        return jobs

    # ------------------------------------------------------------------
    # Claim + execute (same as WorkerAgent v5.x)
    # ------------------------------------------------------------------

    async def _claim_and_execute(self, job: QueuedJob) -> None:
        claimed = await self._claim_job(job.job_id)
        if not claimed:
            return

        logger.info(
            "Executing pulled job %s (action=%s) [gateway=%s]",
            job.job_id, job.action, self._label,
        )

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

        await self._report_result(job.job_id, status, output, error)

    async def _handle_mesh_forward(self, job: QueuedJob) -> Any:
        from runtime.models import SyncActionResponse
        self_url = self._config.self_address or f"http://localhost:{self._config.port}"
        original_target = job.params.get("_mesh_forward_target", "")
        original_action = job.params.get("action", "")
        original_params = {
            k: v for k, v in job.params.items()
            if k not in ("_mesh_forward_target", "action")
        }
        payload = {
            "target_node_id": original_target,
            "payload": {"action": original_action, "params": original_params},
            "trace": {"hop_count": 0, "route_path": []},
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS * 6) as client:
            resp = await client.post(
                f"{self_url}/action",
                json=payload,
                headers=self._auth_headers,
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
        self_url = self._config.self_address or f"http://localhost:{self._config.port}"
        max_wait = float(getattr(self._config, "pull_job_timeout_seconds", 300))
        waited = 0.0
        while waited < max_wait:
            await asyncio.sleep(poll_interval)
            waited += poll_interval
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    resp = await client.get(
                        f"{self_url}/result/{job_id}",
                        headers=self._auth_headers,
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

    async def _claim_job(self, job_id: str) -> bool:
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
        from runtime.job_manager import JobStatus
        waited = 0.0
        while waited < max_wait:
            await asyncio.sleep(poll_interval)
            waited += poll_interval
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
        payload: dict[str, Any] = {"node_id": self._node_id, "status": status}
        if output is not None:
            payload["output"] = output
        if error is not None:
            payload["error"] = error
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload, headers=self._auth_headers)
                resp.raise_for_status()
            logger.info("Result reported for job %s → %s [%s]", job_id, status, self._label)
        except httpx.HTTPError as exc:
            logger.error("Failed to report result for job %s: %s", job_id, exc)

    @property
    def current_poll_interval(self) -> float:
        """Current adaptive poll interval (for observability)."""
        return self._current_poll_interval
