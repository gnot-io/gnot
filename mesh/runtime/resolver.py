"""Node resolver — maps node IDs to network addresses.

Implements the three-tier resolution algorithm:
1. Local (self)  →  return LOCAL sentinel
2. Static config  →  return from ``config.nodes``
3. Default resolver  →  HTTP call to upstream resolver, cached with TTL
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from cachetools import TTLCache

from runtime.config import NodeConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOCAL_SENTINEL: str = "__LOCAL__"
RESOLVE_TIMEOUT_SECONDS: float = 5.0
DEFAULT_CACHE_MAX_SIZE: int = 256


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class NodeNotFoundError(Exception):
    """Raised when a node cannot be resolved."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        super().__init__(f"NODE_NOT_FOUND: {node_id}")


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

class NodeResolver:
    """Resolves node IDs to their HTTP addresses.

    Uses a TTL cache backed by ``cachetools`` so that repeated
    lookups to the default resolver are amortised.
    """

    def __init__(self, config: NodeConfig) -> None:
        self._config = config
        self._cache: TTLCache[str, str] = TTLCache(
            maxsize=DEFAULT_CACHE_MAX_SIZE,
            ttl=config.cache_ttl_seconds,
        )

    async def resolve(self, node_id: str) -> str:
        """Resolve a node ID to an HTTP address.

        Args:
            node_id: The node to look up.

        Returns:
            The HTTP address string, or ``LOCAL_SENTINEL`` when
            the node is the current instance.

        Raises:
            NodeNotFoundError: If the node cannot be resolved.
        """
        # 1. Local
        if node_id == self._config.node_id:
            return LOCAL_SENTINEL

        # 2. Static config
        if node_id in self._config.nodes:
            return self._config.nodes[node_id]

        # 3. Cache hit
        cached = self._cache.get(node_id)
        if cached is not None:
            logger.debug("Resolve cache hit: %s → %s", node_id, cached)
            return cached

        # 4. Default resolver (upstream HTTP)
        if self._config.default_resolver and self._config.default_resolver != self._config.node_id:
            address = await self._resolve_via_upstream(node_id)
            self._cache[node_id] = address
            return address

        raise NodeNotFoundError(node_id)

    async def _resolve_via_upstream(self, node_id: str) -> str:
        """Query the default resolver node for an address.

        Args:
            node_id: The node to resolve.

        Returns:
            The resolved HTTP address.

        Raises:
            NodeNotFoundError: If the upstream returns an error.
        """
        resolver_addr = self._config.nodes.get(self._config.default_resolver or "")
        if not resolver_addr:
            raise NodeNotFoundError(node_id)

        url = f"{resolver_addr}/resolve/{node_id}"
        logger.info("Resolving %s via upstream: %s", node_id, url)

        try:
            async with httpx.AsyncClient(timeout=RESOLVE_TIMEOUT_SECONDS) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise NodeNotFoundError(node_id)
                return data["address"]
        except httpx.HTTPError as exc:
            logger.error("Upstream resolve failed for %s: %s", node_id, exc)
            raise NodeNotFoundError(node_id) from exc
