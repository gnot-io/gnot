"""Gateway router — push/pull job dispatch for v5.3.

Extends the original router with two delivery modes:

  PUSH MODE  — worker is reachable: proxy action via HTTP, pass-through job_id.
  PULL MODE  — worker is behind NAT/offline: enqueue job, worker polls later.

Decision tree for a remote target:
  1. Validate target_node_id ∈ trusted_nodes → else 400 immediately.
  2. Ping target node.
     → Reachable  : push (proxy HTTP).
       - If proxy call fails (timeout/network) → fallback to pull.
     → Unreachable: pull (enqueue).
  3. Return job_id to LLM. LLM is transparent to the mode.

GET /result/{job_id} routing (also handled here via route_result()):
  - LOCAL job  → query local JobManager.
  - PUSH job   → proxy GET to worker node.
  - PULL job   → query local JobManager (gateway owns the state).
    v5.7: lazy timeout check — if job is still QUEUED and created_at age
    exceeds pull_job_timeout_seconds, mark FAILED immediately and return.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from runtime.action_executor import ActionExecutor
from runtime.config import NodeConfig
from runtime.job_manager import JobManager, JobStatus
from runtime.job_queue import JobQueue
from runtime.models import (
    ActionRequest,
    AsyncActionResponse,
    ErrorResponse,
    JobMode,
    JobStatusResponse,
    SyncActionResponse,
)
from runtime.node_registry import NodeRegistry
from runtime.resolver import LOCAL_SENTINEL, NodeNotFoundError, NodeResolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROXY_TIMEOUT_SECONDS: float = 120.0
RESULT_PROXY_TIMEOUT_SECONDS: float = 30.0
DEFAULT_ESTIMATED_SECONDS: int = 30


# ---------------------------------------------------------------------------
# Gateway Router
# ---------------------------------------------------------------------------

class GatewayRouter:
    """Routes action requests with push/pull awareness.

    For local targets: execute directly (unchanged from v5.0–5.2).
    For remote targets: push if reachable, pull otherwise.
    """

    def __init__(
        self,
        config: NodeConfig,
        resolver: NodeResolver,
        executor: ActionExecutor,
        job_manager: JobManager,
        node_registry: NodeRegistry,
        job_queue: JobQueue,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._executor = executor
        self._job_manager = job_manager
        self._node_registry = node_registry
        self._job_queue = job_queue

    # -- main entry point ---------------------------------------------------

    async def route(
        self,
        request: ActionRequest,
    ) -> SyncActionResponse | AsyncActionResponse | ErrorResponse:
        """Route an incoming action request from the LLM."""

        # -- loop / hop protection (unchanged) --------------------------------
        if request.trace.hop_count > self._config.max_hop:
            msg = f"Max hop count exceeded: {request.trace.hop_count} > {self._config.max_hop}"
            logger.warning(msg)
            return ErrorResponse(error=msg, node_id=self._config.node_id)

        if self._config.node_id in request.trace.route_path:
            msg = f"Routing loop detected: {self._config.node_id} already in route_path"
            logger.warning(msg)
            return ErrorResponse(error=msg, node_id=self._config.node_id)

        target = request.target_node_id

        # -- local execution (self) -------------------------------------------
        if target == self._config.node_id:
            return await self._execute_local(request)

        # -- v5.10: next-hop routing ------------------------------------------
        # If target is not a direct child but IS known via an advertised route,
        # forward the original request to the next-hop node unchanged.
        # That next-hop node will repeat the same routing logic for the target.
        # This mirrors BGP next-hop forwarding: we don't need to know the full
        # path — we only need to know the immediate next hop.
        next_hop = self._node_registry.get_next_hop(target)
        if next_hop is not None and next_hop != target:
            # target is reachable but not directly — forward via next_hop
            logger.info(
                "NEXT-HOP: %s → %s (target=%s hop=%d)",
                self._config.node_id, next_hop, target, request.trace.hop_count,
            )
            # Re-route the same request but aimed at the next-hop node;
            # the original target_node_id stays so the intermediate gateway
            # can continue resolving it toward its own children.
            forwarded = request.model_copy(deep=True)
            forwarded.trace.hop_count += 1
            forwarded.trace.route_path.append(self._config.node_id)
            return await self._forward_via_next_hop(forwarded, next_hop)

        # -- remote target: must be trusted -----------------------------------
        if not self._node_registry.is_trusted(target):
            # Also check static resolver as fallback (backward compat with node.yaml nodes:{})
            try:
                address = await self._resolver.resolve(target)
            except NodeNotFoundError:
                address = None

            if address is None or address == LOCAL_SENTINEL:
                msg = f"UNTRUSTED_NODE: {target} is not in the trusted-node list"
                logger.warning(msg)
                return ErrorResponse(error=msg, node_id=self._config.node_id)
            # Node found in resolver but not registry — treat as trusted static node
            # and push directly (legacy mesh behaviour)
            return await self._push(request, address)

        # -- try to get worker address ----------------------------------------
        worker_address = await self._node_registry.get_address(target)

        # -- static resolver fallback (node.yaml nodes: section) ---------------
        if not worker_address:
            try:
                resolved = await self._resolver.resolve(target)
                if resolved != LOCAL_SENTINEL:
                    worker_address = resolved
            except NodeNotFoundError:
                pass

        if not worker_address:
            # Trusted but no address known → must queue
            logger.info(
                "Node %s trusted but has no address — queuing (pull mode)", target
            )
            return await self._pull(request)

        # -- ping to decide push vs pull --------------------------------------
        reachable = await self._node_registry.ping(target)
        if reachable:
            result = await self._push(request, worker_address)
            # If push failed, fallback to pull transparently
            if isinstance(result, ErrorResponse):
                logger.info(
                    "Push to %s failed (%s) — falling back to pull mode",
                    target, result.error,
                )
                return await self._pull(request)
            return result
        else:
            logger.info("Node %s unreachable — queuing (pull mode)", target)
            return await self._pull(request)

    # -- result routing -----------------------------------------------------

    async def route_result(
        self, job_id: str
    ) -> JobStatusResponse | ErrorResponse:
        """Answer GET /result/{job_id} for the LLM.

        - LOCAL/PULL job → query local JobManager.
          v5.7: if job is still QUEUED and age > pull_job_timeout_seconds,
          mark it FAILED lazily (no worker claimed it in time).
        - PUSH job       → proxy to worker node.
        """
        # Check local JobManager first (handles LOCAL + PULL jobs)
        job = await self._job_manager.get_job(job_id)
        if job:
            # v5.7 — lazy pull job timeout check
            if job.status == JobStatus.QUEUED:
                queued_job = await self._job_queue.get_queued_job(job_id)
                if queued_job is not None:
                    age = time.time() - queued_job.created_at
                    timeout = getattr(self._config, "pull_job_timeout_seconds", 300)
                    if age > timeout:
                        logger.warning(
                            "Pull job %s timed out after %.0fs (timeout=%ds) — marking FAILED",
                            job_id, age, timeout,
                        )
                        await self._job_manager.update_job(
                            job_id,
                            status=JobStatus.FAILED,
                            error=f"Pull job timed out after {int(age)}s — no worker claimed it",
                        )
                        # Re-fetch updated job
                        job = await self._job_manager.get_job(job_id)

            return JobStatusResponse(
                task_id=job.task_id,
                job_id=job.job_id,
                status=job.status.value,
                progress=job.progress,
                output=job.output,
                error=job.error,
            )

        # Check job queue routing table (PUSH jobs)
        route = await self._job_queue.get_route(job_id)
        if route and route.mode == JobMode.PUSH and route.worker_address:
            return await self._proxy_result(job_id, route.worker_address)

        return ErrorResponse(
            error=f"JOB_NOT_FOUND: {job_id}",
            node_id=self._config.node_id,
        )

    # -- push mode ----------------------------------------------------------

    async def _forward_via_next_hop(
        self,
        request: ActionRequest,
        next_hop_node_id: str,
    ) -> "SyncActionResponse | AsyncActionResponse | ErrorResponse":
        """Forward request to next_hop_node_id, which will route onward to target.

        If next_hop has a known address → push directly to its /action endpoint.
        If next_hop is pull-only (no address) → enqueue for it to poll.

        The next_hop node receives the full original request (target_node_id
        unchanged) so it can apply its own routing table.
        """
        hop_address = await self._node_registry.get_address(next_hop_node_id)

        if not hop_address:
            # Try resolver fallback
            try:
                resolved = await self._resolver.resolve(next_hop_node_id)
                if resolved != LOCAL_SENTINEL:
                    hop_address = resolved
            except NodeNotFoundError:
                pass

        if hop_address:
            reachable = await self._node_registry.ping(next_hop_node_id)
            if reachable:
                result = await self._push(request, hop_address)
                if not isinstance(result, ErrorResponse):
                    return result
                logger.info(
                    "Push to next-hop %s failed — falling back to pull", next_hop_node_id
                )

        # Fallback: enqueue for next_hop to pull.
        # Swap target to next_hop_node_id so the pull queue routes to the right node.
        pull_req = request.model_copy(deep=True)
        pull_req.target_node_id = next_hop_node_id
        # Embed the original target inside params so next_hop knows where to re-route.
        # The original target_node_id travels inside the job payload; next_hop's
        # WorkerAgent will POST it back to its own /action endpoint.
        pull_req.payload.params["_mesh_forward_target"] = request.target_node_id
        pull_req.payload.action = "_mesh_forward"
        return await self._pull(pull_req)

    async def _push(
        self,
        request: ActionRequest,
        worker_address: str,
    ) -> SyncActionResponse | AsyncActionResponse | ErrorResponse:
        """Proxy the action directly to the worker node (push mode)."""
        forwarded = request.model_copy(deep=True)
        forwarded.trace.hop_count += 1
        forwarded.trace.route_path.append(self._config.node_id)

        url = f"{worker_address}/action"
        logger.info(
            "PUSH → %s: action=%s task=%s",
            url, request.payload.action, request.task_id,
        )

        try:
            async with httpx.AsyncClient(timeout=PROXY_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=forwarded.model_dump())
                resp.raise_for_status()
                data = resp.json()

                if "job_id" in data:
                    # Register push-mode job so /result can be proxied
                    await self._job_queue.register_push_job(
                        job_id=data["job_id"],
                        task_id=request.task_id,
                        target_node_id=request.target_node_id,
                        worker_address=worker_address,
                    )
                    return AsyncActionResponse(**data)
                if "error" in data:
                    return ErrorResponse(**data)
                return SyncActionResponse(**data)

        except httpx.HTTPError as exc:
            logger.warning("Push to %s failed: %s", url, exc)
            return ErrorResponse(
                error=f"Push failed: {exc}",
                node_id=self._config.node_id,
            )

    # -- pull mode ----------------------------------------------------------

    async def _pull(
        self, request: ActionRequest
    ) -> AsyncActionResponse:
        """Enqueue the job for the worker to pull later."""
        target = request.target_node_id

        # Create the job record in local JobManager (gateway owns it)
        job = await self._job_manager.create_job(
            node_id=self._config.node_id,
            task_id=request.task_id,
            estimated_completion_seconds=DEFAULT_ESTIMATED_SECONDS,
        )
        # Transition to QUEUED state
        await self._job_manager.update_job(job.job_id, status=JobStatus.QUEUED)

        # Enqueue in the per-node queue using the same job_id
        await self._job_queue.enqueue(
            node_id=target,
            task_id=request.task_id,
            action=request.payload.action,
            params=request.payload.params,
            job_id=job.job_id,
            timeout_seconds=getattr(self._config, "pull_job_timeout_seconds", 300),
            caller_token=request.caller_token,
            caller_credentials=dict(request.caller_credentials),
        )

        logger.info(
            "PULL ← enqueued job %s for node %s (action=%s)",
            job.job_id, target, request.payload.action,
        )

        return AsyncActionResponse(
            task_id=request.task_id,
            job_id=job.job_id,
            status="accepted",
            estimated_completion_seconds=job.estimated_completion_seconds,
        )

    # -- local execution ----------------------------------------------------

    async def _execute_local(
        self, request: ActionRequest
    ) -> SyncActionResponse | AsyncActionResponse | ErrorResponse:
        logger.info(
            "LOCAL: action=%s task=%s", request.payload.action, request.task_id
        )
        from runtime.action_executor import CallerNotAllowedError, MissingCallerCredentialError
        try:
            return await self._executor.execute(
                action_name=request.payload.action,
                params=request.payload.params,
                task_id=request.task_id,
                caller_token=request.caller_token,
                caller_credentials=request.caller_credentials,
            )
        except CallerNotAllowedError as exc:
            return ErrorResponse(error=str(exc), node_id=self._config.node_id)
        except MissingCallerCredentialError as exc:
            return ErrorResponse(error=str(exc), node_id=self._config.node_id)
        except KeyError as exc:
            return ErrorResponse(error=str(exc), node_id=self._config.node_id)

    # -- proxy result -------------------------------------------------------

    async def _proxy_result(
        self, job_id: str, worker_address: str
    ) -> JobStatusResponse | ErrorResponse:
        url = f"{worker_address}/result/{job_id}"
        try:
            async with httpx.AsyncClient(timeout=RESULT_PROXY_TIMEOUT_SECONDS) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    return ErrorResponse(**data)
                return JobStatusResponse(**data)
        except httpx.HTTPError as exc:
            logger.error("Result proxy failed for %s: %s", job_id, exc)
            return ErrorResponse(
                error=f"Result proxy failed: {exc}",
                node_id=self._config.node_id,
            )
