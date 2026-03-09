"""Transport Bridge ABC — plugin architecture for external channel transports.

v6.0 Phase 7 — Multi-Channel Transport.

Design rule: runtime/ NEVER imports from transports/.
Coupling is strictly one-way: transports/ imports from runtime/.
The only shared surface is this ABC + runtime.models.

Adding a new channel (Slack, Discord, ...):
  1. Create transports/{channel}/bridge.py
  2. Subclass ChannelTransportBridge, set transport_id
  3. Enable in node.yaml under transports:
  4. Zero changes to runtime/ required.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from fastapi import APIRouter
    from runtime.models import ExternalParticipant, InteractionThread

logger = logging.getLogger(__name__)


class ChannelTransportBridge(ABC):
    """Base class for all external channel transport bridges.

    Core rule: runtime/ NEVER imports from transports/.
    Coupling = only this ABC + runtime.models (ExternalParticipant, InteractionThread).

    Subclasses must:
      - Set transport_id class variable (e.g. "telegram", "slack")
      - Implement startup(), shutdown(), notify()
      - Optionally override get_fastapi_router() for inbound webhooks
      - Optionally override is_ready to expose readiness state
    """

    transport_id: ClassVar[str]  # "telegram" | "slack" | "discord"

    @abstractmethod
    async def startup(self) -> None:
        """Initialize bridge. Called during node lifespan startup.

        Should connect to external services, load persisted state, and
        start any background tasks (e.g. polling loops).
        Must be idempotent — safe to call multiple times.
        """

    @abstractmethod
    async def shutdown(self) -> None:
        """Graceful disconnect. Called during node lifespan shutdown.

        Should stop background tasks, flush state, and close connections.
        Must not raise — catch and log all errors internally.
        """

    @abstractmethod
    async def notify(
        self,
        participant: "ExternalParticipant",
        thread: "InteractionThread",
    ) -> None:
        """Push question notification to participant (Phase 6 transactional path).

        Called by InteractionRouter when an agent suspends and asks a question
        targeting a participant whose transport matches this bridge's transport_id.

        Must not raise — catch and log all errors internally.
        The router will log a warning if notify() raises, but won't retry.
        """

    def get_fastapi_router(self) -> "APIRouter | None":
        """Return FastAPI router for inbound channel webhooks.

        Auto-mounted at /transports/{transport_id}/...
        Return None if bridge uses polling-only (no inbound HTTP routes needed).
        Default: None (polling-only bridge).
        """
        return None

    @property
    def is_ready(self) -> bool:
        """True if bridge is initialized and ready to send notifications.

        Default: False. Subclasses should override to reflect actual state.
        """
        return False


class TransportBridgeRegistry:
    """Holds all active transport bridge plugins.

    Singleton-like — one instance per node, injected into InteractionRouter
    and referenced by server.py for startup/shutdown/router mounting.
    """

    def __init__(self) -> None:
        self._bridges: dict[str, ChannelTransportBridge] = {}

    def register(self, bridge: ChannelTransportBridge) -> None:
        """Register a bridge plugin. Overwrites if transport_id already registered."""
        tid = bridge.transport_id
        if tid in self._bridges:
            logger.warning(
                "TransportBridgeRegistry: overwriting existing bridge for transport_id=%s", tid
            )
        self._bridges[tid] = bridge
        logger.info("TransportBridgeRegistry: registered bridge transport_id=%s", tid)

    def get(self, transport_id: str) -> "ChannelTransportBridge | None":
        """Return bridge for given transport_id, or None if not registered."""
        return self._bridges.get(transport_id)

    def all(self) -> list[ChannelTransportBridge]:
        """Return all registered bridge instances."""
        return list(self._bridges.values())

    def list_ids(self) -> list[str]:
        """Return list of registered transport_ids."""
        return list(self._bridges.keys())

    def __len__(self) -> int:
        return len(self._bridges)

    def __repr__(self) -> str:
        return f"TransportBridgeRegistry(bridges={self.list_ids()})"
