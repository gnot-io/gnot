"""ChannelRegistry — thin view of NodeRegistry from a channel perspective.

v6.1 insight: Gateway IS the channel. This class is a thin wrapper (~30 LOC)
that exposes channel semantics on top of NodeRegistry.

Channel scope = set of nodes registered with this gateway.
Channel ID    = this gateway's own node_id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.node_registry import NodeRegistry


class ChannelRegistry:
    """Thin channel-oriented view over NodeRegistry.

    Does not own any state — all queries delegate to NodeRegistry.
    """

    def __init__(self, node_registry: "NodeRegistry", gateway_node_id: str) -> None:
        self._registry = node_registry
        self._channel_id = gateway_node_id

    def get_channel_id(self) -> str:
        """Return this channel's ID (= gateway's node_id)."""
        return self._channel_id

    def get_members(self) -> list[str]:
        """Return node_ids of all registered members in this channel."""
        # NodeRegistry._entries keys are the registered node IDs
        return list(self._registry._entries.keys())

    def is_member(self, node_id: str) -> bool:
        """Return True if node_id is a registered member of this channel."""
        return self._registry.is_trusted(node_id)

    def member_count(self) -> int:
        """Return the number of registered members."""
        return len(self.get_members())
