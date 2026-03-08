"""Node registry — tracks trusted worker nodes and their reachability.

Gateway-side component that:
  - Maintains the trusted-node whitelist (from config + dynamic registration)
  - Accepts heartbeats from workers and tracks liveness
  - Exposes reachability status used by the gateway router to decide
    between push mode (proxy) and pull mode (queue)

v5.7 — Lazy staleness check:
  Previously, staleness detection required a separate background loop calling
  mark_stale_nodes_unreachable() on a timer. This was unnecessary complexity.

  Alternative: check heartbeat age lazily whenever node state is consulted
  (get_address, get_status, ping). If last_heartbeat is older than
  heartbeat_timeout_seconds, the node is marked UNREACHABLE in-place before
  the result is returned. No background task needed.

  mark_stale_nodes_unreachable() is kept for explicit sweeps but is no longer
  required for correctness.

Thread-safety: all mutations are protected by asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, field
from typing import Optional

import httpx

from runtime.models import ActionSpec, CapabilityNode, NodeInfo, NodeStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PING_TIMEOUT_SECONDS: float = 3.0
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS: int = 30


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

@dataclass
class _NodeEntry:
    """Mutable internal record for a registered node."""
    node_id: str
    address: Optional[str] = None          # self-reported or config-provided HTTP address
    last_heartbeat: Optional[float] = None  # unix timestamp of most recent heartbeat
    status: NodeStatus = NodeStatus.UNKNOWN
    # v5.10 — BGP-style routing
    actions: list = field(default_factory=list)   # actions this node provides
    next_hop: Optional[str] = None                 # None=direct child, str=route via this node
    # v5.11 — full action specs (description + caller credential requirements)
    action_specs: dict = field(default_factory=dict)  # {action_name: ActionSpec}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class NodeRegistry:
    """Tracks trusted nodes and their live status.

    Populated from two sources:
      1. ``trusted_nodes`` list in node.yaml (static whitelist)
      2. Dynamic registrations via POST /nodes/register

    Only nodes present in this registry can receive jobs from the gateway.
    """

    def __init__(
        self,
        trusted_node_ids: list[str] | None = None,
        heartbeat_timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        ping_timeout_seconds: float = PING_TIMEOUT_SECONDS,
        registration_policy: str = "open",   # v6.0: "open" | "whitelist" | "invite_only"
    ) -> None:
        self._entries: dict[str, _NodeEntry] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_timeout = heartbeat_timeout_seconds
        self._ping_timeout = ping_timeout_seconds
        self._registration_policy = registration_policy   # v6.0

        # Pre-populate from static config
        for node_id in (trusted_node_ids or []):
            self._entries[node_id] = _NodeEntry(node_id=node_id)

        logger.info(
            "NodeRegistry initialised — %d static trusted node(s): %s, policy=%s",
            len(self._entries),
            list(self._entries.keys()),
            registration_policy,
        )

    # -- staleness helper ---------------------------------------------------

    def _is_heartbeat_stale(self, entry: _NodeEntry) -> bool:
        """Return True if the entry's heartbeat has exceeded the timeout.

        Only applies to ONLINE nodes that have sent at least one heartbeat.
        Nodes that have never sent a heartbeat (UNKNOWN status) are not
        considered stale — they may simply not be heartbeat-capable.
        """
        if entry.status != NodeStatus.ONLINE:
            return False
        if entry.last_heartbeat is None:
            return False
        return (time.time() - entry.last_heartbeat) > self._heartbeat_timeout

    def _apply_lazy_staleness(self, entry: _NodeEntry) -> None:
        """Mark entry UNREACHABLE in-place if its heartbeat is stale.

        Called (under lock) whenever node state is about to be used for
        routing decisions. No background task required.
        """
        if self._is_heartbeat_stale(entry):
            age = time.time() - entry.last_heartbeat  # type: ignore[operator]
            logger.warning(
                "Node %s lazily marked UNREACHABLE — heartbeat %.0fs ago (timeout=%ds)",
                entry.node_id, age, self._heartbeat_timeout,
            )
            entry.status = NodeStatus.UNREACHABLE

    # -- public API ---------------------------------------------------------

    def is_trusted(self, node_id: str) -> bool:
        """Return True if node_id is in the trusted-node set."""
        return node_id in self._entries

    async def register(
        self,
        node_id: str,
        address: str | None = None,
        actions: list[str] | None = None,
        advertise_routes: list[str] | None = None,
        capabilities: dict[str, list[str]] | None = None,
        action_specs: dict[str, dict] | None = None,
        sub_route_specs: dict[str, dict] | None = None,  # v5.13: specs per sub-node
    ) -> None:
        """Add or update a worker node entry.

        Called when a worker POSTs to /nodes/register.

        v5.10: accepts BGP-style route advertisement.
        v6.0: honours registration_policy:
          - whitelist (default): only pre-configured trusted_nodes accepted
          - open: any authenticated node accepted (auto-added to registry)
          - invite_only: future — treated as whitelist for now

        If node is already trusted (from config), this updates its entry.
        If it is new, it is added and any advertised sub-routes are installed.
        """
        async with self._lock:
            existing = self._entries.get(node_id)
            if existing:
                if address:
                    existing.address = address
                if actions is not None:
                    existing.actions = list(actions)
                if action_specs:
                    existing.action_specs = dict(action_specs)
                logger.info(
                    "Node re-registered: %s (address=%s, actions=%s)",
                    node_id, address, actions,
                )
            else:
                # v6.0: open policy → auto-accept any authenticated node
                if self._registration_policy == "open":
                    self._entries[node_id] = _NodeEntry(
                        node_id=node_id,
                        address=address,
                        actions=list(actions or []),
                        action_specs=dict(action_specs or {}),
                    )
                    logger.info(
                        "New node auto-registered (open policy): %s (address=%s, actions=%s)",
                        node_id, address, actions,
                    )
                else:
                    # whitelist / invite_only: do NOT accept unknown nodes
                    logger.warning(
                        "Registration denied for unknown node %s (policy=%s) — "
                        "add to trusted_nodes or set registration_policy: open",
                        node_id, self._registration_policy,
                    )
                    return

            # v5.10 — install advertised sub-routes
            # For every sub-node the registering node claims to reach,
            # add a routing entry with next_hop = node_id (BGP next-hop).
            for sub_id in (advertise_routes or []):
                sub_actions = (capabilities or {}).get(sub_id, [])
                sub_specs = (sub_route_specs or {}).get(sub_id, {})  # v5.13
                sub_existing = self._entries.get(sub_id)
                if sub_existing:
                    # Update actions + specs, keep whatever else is known
                    if sub_actions:
                        sub_existing.actions = list(sub_actions)
                    if sub_specs:
                        sub_existing.action_specs = dict(sub_specs)  # v5.13
                    if sub_existing.next_hop is None:
                        sub_existing.next_hop = node_id
                else:
                    self._entries[sub_id] = _NodeEntry(
                        node_id=sub_id,
                        address=None,
                        actions=list(sub_actions),
                        action_specs=dict(sub_specs),  # v5.13
                        next_hop=node_id,
                    )
                logger.info(
                    "Route advertised: %s → reachable via %s (actions=%s, specs=%d)",
                    sub_id, node_id, sub_actions, len(sub_specs),
                )

    async def heartbeat(self, node_id: str) -> bool:
        """Record a heartbeat from a worker node.

        Returns True if the node is trusted (heartbeat accepted).
        """
        async with self._lock:
            entry = self._entries.get(node_id)
            if entry is None:
                logger.warning("Heartbeat from unknown node: %s (ignored)", node_id)
                return False
            entry.last_heartbeat = time.time()
            entry.status = NodeStatus.ONLINE
            logger.debug("Heartbeat from %s", node_id)
            return True

    async def get_address(self, node_id: str) -> str | None:
        """Return the stored address for a node, or None.

        Applies lazy staleness check — if the node's heartbeat is stale,
        it is marked UNREACHABLE before the address is returned, ensuring
        the gateway router will choose pull mode on next routing decision.
        """
        async with self._lock:
            entry = self._entries.get(node_id)
            if entry is None:
                return None
            self._apply_lazy_staleness(entry)
            return entry.address

    async def get_status(self, node_id: str) -> NodeStatus:
        """Return the current status of a node, applying lazy staleness check."""
        async with self._lock:
            entry = self._entries.get(node_id)
            if entry is None:
                return NodeStatus.UNKNOWN
            self._apply_lazy_staleness(entry)
            return entry.status

    async def set_status(self, node_id: str, status: NodeStatus) -> None:
        """Directly set a node's status (used after ping results)."""
        async with self._lock:
            entry = self._entries.get(node_id)
            if entry:
                entry.status = status

    async def withdraw_routes(self, via_node_id: str, keep_routes: list[str]) -> list[str]:
        """Remove indirect routes that were advertised via via_node_id but are no longer in keep_routes.

        Called when via_node_id re-registers with a potentially smaller advertise_routes list.
        Returns the list of node_ids that were withdrawn.

        This implements BGP WITHDRAW semantics: when a node re-advertises, the difference
        between its previous advertisement and the new one represents withdrawn routes.
        """
        withdrawn: list[str] = []
        async with self._lock:
            to_remove = [
                nid for nid, entry in self._entries.items()
                if entry.next_hop == via_node_id and nid not in keep_routes
            ]
            for nid in to_remove:
                del self._entries[nid]
                withdrawn.append(nid)
                logger.info(
                    "Route withdrawn: %s (was reachable via %s — no longer advertised)",
                    nid, via_node_id,
                )
        return withdrawn

    def get_next_hop(self, target_node_id: str) -> str | None:
        """Return the direct next-hop node ID for reaching target_node_id.

        Returns:
            None        — target is not known at all
            target_id   — target is a direct child (next_hop is None in entry)
            some_node   — target is reachable via that intermediate node
        """
        entry = self._entries.get(target_node_id)
        if entry is None:
            return None
        # next_hop == None means the node registered directly with us → it IS the next hop
        return entry.next_hop or target_node_id

    def get_actions(self, node_id: str) -> list[str]:
        """Return the action list for a known node (empty if unknown)."""
        entry = self._entries.get(node_id)
        return list(entry.actions) if entry else []

    def build_capability_tree(self, own_actions: list[str]) -> dict[str, "CapabilityNode"]:
        """Build reachable capability tree for GET /capabilities response.

        Returns a flat dict of all non-self entries. v5.11: includes full
        ActionSpec (description + caller_credentials) for each action.
        """
        reachable: dict[str, CapabilityNode] = {}
        for entry in list(self._entries.values()):
            # Deserialise stored action_specs dicts back to ActionSpec objects
            specs: dict[str, ActionSpec] = {}
            for aname, raw_spec in entry.action_specs.items():
                if isinstance(raw_spec, ActionSpec):
                    specs[aname] = raw_spec
                elif isinstance(raw_spec, dict):
                    try:
                        specs[aname] = ActionSpec(**raw_spec)
                    except Exception:
                        pass
            # v5.12: apply lazy staleness before including in tree
            self._apply_lazy_staleness(entry)
            cap = CapabilityNode(
                node_id=entry.node_id,
                actions=list(entry.actions),
                action_specs=specs,
                next_hop=entry.next_hop,
                status=entry.status.value,  # v5.12: expose liveness to LLM
                reachable={},
            )
            reachable[entry.node_id] = cap
        return reachable

    def get_all_statuses(self) -> dict[str, str]:
        """Return {node_id: status_str} for all registered nodes (sync snapshot, for system prompt).

        Reads _entries without acquiring the async lock — safe for read-only
        snapshot since dict iteration in CPython is GIL-protected and entries
        are only added/updated (never deleted during normal operation).
        """
        return {
            e.node_id: e.status.value
            for e in list(self._entries.values())
        }

    async def list_nodes(self) -> list[NodeInfo]:
        """Return public info for all registered nodes."""
        async with self._lock:
            return [
                NodeInfo(
                    node_id=e.node_id,
                    address=e.address,
                    status=e.status,
                    last_heartbeat=e.last_heartbeat,
                )
                for e in self._entries.values()
            ]

    # -- liveness check -----------------------------------------------------

    async def ping(self, node_id: str) -> bool:
        """Actively ping a worker's /ping endpoint to check reachability.

        Applies lazy staleness check first. If the node is already stale
        (heartbeat timed out), skips the network probe and returns False
        immediately to avoid unnecessary latency on hot-path routing.

        Updates the node's status in-place.
        Returns True if node responded within timeout.
        """
        async with self._lock:
            entry = self._entries.get(node_id)
            if entry is None:
                return False
            self._apply_lazy_staleness(entry)
            if entry.status == NodeStatus.UNREACHABLE:
                # Already known unreachable (stale or prior ping failure) — skip probe
                logger.debug(
                    "Skipping ping for %s — already UNREACHABLE (lazy stale check)", node_id
                )
                return False
            address = entry.address

        if not address:
            logger.debug("Cannot ping %s — no address known", node_id)
            await self.set_status(node_id, NodeStatus.UNREACHABLE)
            return False

        url = f"{address}/ping"
        try:
            async with httpx.AsyncClient(timeout=self._ping_timeout) as client:
                resp = await client.get(url)
                reachable = resp.status_code == 200
        except httpx.HTTPError:
            reachable = False

        new_status = NodeStatus.ONLINE if reachable else NodeStatus.UNREACHABLE
        await self.set_status(node_id, new_status)
        logger.info("Ping %s → %s (address=%s)", node_id, new_status.value, address)
        return reachable

    # -- heartbeat staleness ------------------------------------------------

    async def mark_stale_nodes_unreachable(self) -> list[str]:
        """Mark nodes whose heartbeat is older than the timeout as UNREACHABLE.

        Called by the background stale-checker loop.
        Returns the list of node_ids that were marked unreachable.
        """
        now = time.time()
        marked: list[str] = []
        async with self._lock:
            for entry in self._entries.values():
                if entry.status != NodeStatus.ONLINE:
                    continue
                if entry.last_heartbeat is None:
                    continue
                age = now - entry.last_heartbeat
                if age > self._heartbeat_timeout:
                    entry.status = NodeStatus.UNREACHABLE
                    marked.append(entry.node_id)
                    logger.warning(
                        "Node %s marked UNREACHABLE — heartbeat %.0fs ago (timeout=%ds)",
                        entry.node_id, age, self._heartbeat_timeout,
                    )
        return marked
