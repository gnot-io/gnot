"""Request router — local execution vs. proxy forwarding.

Implements the mesh routing algorithm with hop-count and loop protection.
If the target node is local, the action is executed directly.
Otherwise, the request is proxied to the resolved address.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from runtime.action_executor import ActionExecutor
from runtime.config import NodeConfig
from runtime.models import (
    ActionRequest,
    AsyncActionResponse,
    ErrorResponse,
    SyncActionResponse,
)
from runtime.resolver import LOCAL_SENTINEL, NodeNotFoundError, NodeResolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROXY_TIMEOUT_SECONDS: float = 120.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RoutingError(Exception):
    """Raised when a request cannot be routed."""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class RequestRouter:
    """Routes action requests to the correct node.

    For local targets the action is executed in-process.
    For remote targets the request envelope is proxied over HTTP.
    """

    def __init__(
        self,
        config: NodeConfig,
        resolver: NodeResolver,
        executor: ActionExecutor,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._executor = executor

    async def route(
        self,
        request: ActionRequest,
    ) -> SyncActionResponse | AsyncActionResponse | ErrorResponse:
        """Route an incoming action request.

        Args:
            request: The full action request envelope.

        Returns:
            A response from either local execution or the proxied node.
        """
        # -- loop protection --------------------------------------------------
        if request.trace.hop_count > self._config.max_hop:
            msg = (
                f"Max hop count exceeded: {request.trace.hop_count} > "
                f"{self._config.max_hop}"
            )
            logger.warning(msg)
            return ErrorResponse(error=msg, node_id=self._config.node_id)

        if self._config.node_id in request.trace.route_path:
            msg = f"Routing loop detected: {self._config.node_id} already in route_path"
            logger.warning(msg)
            return ErrorResponse(error=msg, node_id=self._config.node_id)

        # -- resolve target ----------------------------------------------------
        try:
            address = await self._resolver.resolve(request.target_node_id)
        except NodeNotFoundError:
            return ErrorResponse(
                error=f"NODE_NOT_FOUND: {request.target_node_id}",
                node_id=self._config.node_id,
            )

        # -- local execution ---------------------------------------------------
        if address == LOCAL_SENTINEL:
            logger.info(
                "Executing locally: action=%s task=%s",
                request.payload.action,
                request.task_id,
            )
            try:
                return await self._executor.execute(
                    action_name=request.payload.action,
                    params=request.payload.params,
                    task_id=request.task_id,
                )
            except KeyError as exc:
                return ErrorResponse(
                    error=str(exc),
                    node_id=self._config.node_id,
                )

        # -- proxy to remote node ----------------------------------------------
        return await self._proxy(request, address)

    async def _proxy(
        self,
        request: ActionRequest,
        target_address: str,
    ) -> SyncActionResponse | AsyncActionResponse | ErrorResponse:
        """Forward the request to a remote node.

        Increments hop_count and appends self to route_path before sending.
        """
        # Mutate trace for the next hop
        forwarded = request.model_copy(deep=True)
        forwarded.trace.hop_count += 1
        forwarded.trace.route_path.append(self._config.node_id)

        url = f"{target_address}/action"
        logger.info(
            "Proxying to %s: action=%s task=%s hop=%d",
            url,
            request.payload.action,
            request.task_id,
            forwarded.trace.hop_count,
        )

        try:
            async with httpx.AsyncClient(timeout=PROXY_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=forwarded.model_dump())
                resp.raise_for_status()
                data = resp.json()

                # Determine response type from presence of job_id
                if "job_id" in data:
                    return AsyncActionResponse(**data)
                if "error" in data:
                    return ErrorResponse(**data)
                return SyncActionResponse(**data)

        except httpx.HTTPError as exc:
            logger.error("Proxy request failed to %s: %s", url, exc)
            return ErrorResponse(
                error=f"Proxy failed: {exc}",
                node_id=self._config.node_id,
            )
