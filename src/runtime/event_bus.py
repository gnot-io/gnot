"""EventBus — in-process publish/subscribe event system for GNOT v6.0.

Architecture:
  - Append-only in-memory event log (capped at max_log_size).
  - Subscription registry with pattern matching, payload filtering,
    debounce, and max_deliveries limits.
  - Background asyncio delivery worker: matches events to subscriptions,
    dispatches ActionRequests via GatewayRouter, retries on failure.
  - Optional JSONL persistence: every emitted event appended to a file
    for replay across restarts.

Pattern matching (event_type_pattern):
  - "*"          — matches everything
  - "test.failed"— exact match
  - "test.*"     — prefix wildcard (all events under "test." namespace)
  - "*.failed"   — suffix wildcard (all "*.failed" events)

Thread-safety: all mutable state protected by asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from runtime.models import (
    ActionPayload,
    ActionRequest,
    Event,
    Subscription,
)

if TYPE_CHECKING:
    from runtime.gateway_router import GatewayRouter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants (overrideable via EventBusConfig)
# ---------------------------------------------------------------------------

DEFAULT_MAX_LOG_SIZE: int = 10_000
DEFAULT_DELIVERY_TIMEOUT: float = 10.0
DEFAULT_DELIVERY_RETRY_COUNT: int = 3
DEFAULT_DELIVERY_RETRY_BACKOFF: float = 2.0

# Maximum items queued for delivery before back-pressure
_DELIVERY_QUEUE_MAX: int = 50_000


# ---------------------------------------------------------------------------
# Internal state holder
# ---------------------------------------------------------------------------

@dataclass
class _SubState:
    """Mutable runtime state for one registered subscription."""
    sub: Subscription
    delivery_count: int = 0
    last_delivered_at: float | None = None
    _debounce_timer: float | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Pattern / filter helpers
# ---------------------------------------------------------------------------

def _matches_pattern(pattern: str, event_type: str) -> bool:
    """Return True if event_type matches the subscription pattern."""
    if pattern == "*":
        return True
    if "*" not in pattern:
        return pattern == event_type
    if pattern.endswith(".*"):
        # "test.*" → match "test.anything" but NOT "test"
        prefix = pattern[:-2]           # "test"
        return event_type.startswith(prefix + ".")
    if pattern.startswith("*."):
        # "*.failed" → match "test.failed", "review.failed"
        suffix = pattern[1:]            # ".failed"
        return event_type.endswith(suffix)
    # Mixed wildcards not supported — treat as no-match
    return False


def _matches_payload_filter(filter_dict: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Return True if all key:value pairs in filter_dict appear in payload."""
    for key, expected in filter_dict.items():
        if payload.get(key) != expected:
            return False
    return True


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """Central event pub/sub bus for one GNOT node.

    Lifecycle:
        bus = EventBus(config, gateway_router, node_id)
        await bus.start()     # starts delivery worker
        ...
        await bus.stop()      # drains queue, stops worker
    """

    def __init__(
        self,
        *,
        max_log_size: int = DEFAULT_MAX_LOG_SIZE,
        delivery_timeout_seconds: float = DEFAULT_DELIVERY_TIMEOUT,
        delivery_retry_count: int = DEFAULT_DELIVERY_RETRY_COUNT,
        delivery_retry_backoff: float = DEFAULT_DELIVERY_RETRY_BACKOFF,
        persistence_path: str | None = None,
        gateway_router: "GatewayRouter | None" = None,
        node_id: str = "unknown",
        caller_token: str | None = None,
    ) -> None:
        self._max_log_size = max_log_size
        self._delivery_timeout = delivery_timeout_seconds
        self._retry_count = delivery_retry_count
        self._retry_backoff = delivery_retry_backoff
        self._persistence_path = Path(persistence_path) if persistence_path else None
        self._gateway_router = gateway_router
        self._node_id = node_id
        self._caller_token = caller_token

        # Core state
        self._event_log: deque[Event] = deque(maxlen=max_log_size)
        self._subscriptions: dict[str, _SubState] = {}  # sub_id → _SubState
        self._lock = asyncio.Lock()

        # Delivery pipeline
        self._delivery_queue: asyncio.Queue[tuple[Event, str]] = asyncio.Queue(
            maxsize=_DELIVERY_QUEUE_MAX
        )
        self._delivery_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background delivery worker."""
        if self._running:
            return
        self._running = True
        # Ensure persistence file directory exists
        if self._persistence_path:
            self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
        self._delivery_task = asyncio.create_task(
            self._delivery_worker(), name="event-bus-delivery"
        )
        logger.info(
            "EventBus started — node=%s, max_log=%d, persist=%s",
            self._node_id, self._max_log_size,
            str(self._persistence_path) if self._persistence_path else "disabled",
        )

    async def stop(self) -> None:
        """Stop the delivery worker gracefully."""
        self._running = False
        if self._delivery_task and not self._delivery_task.done():
            self._delivery_task.cancel()
            try:
                await self._delivery_task
            except asyncio.CancelledError:
                pass
        logger.info("EventBus stopped — node=%s", self._node_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def emit(self, event: Event) -> int:
        """Publish an event. Returns number of matched subscriptions queued."""
        async with self._lock:
            self._event_log.append(event)
            matched = self._match_subscriptions(event)

        # Persist outside lock (I/O operation)
        if self._persistence_path:
            await self._persist_event(event)

        # Enqueue matched deliveries
        queued = 0
        for sub_id in matched:
            try:
                self._delivery_queue.put_nowait((event, sub_id))
                queued += 1
            except asyncio.QueueFull:
                logger.warning(
                    "EventBus delivery queue full — dropping delivery for sub=%s", sub_id
                )

        logger.debug(
            "EventBus emit: type=%s source=%s matched=%d",
            event.event_type, event.source_node, queued,
        )
        return queued

    async def subscribe(self, sub: Subscription) -> str:
        """Register a subscription. Returns sub_id."""
        async with self._lock:
            self._subscriptions[sub.sub_id] = _SubState(sub=sub)
        logger.info(
            "EventBus subscribe: sub_id=%s pattern=%s node=%s action=%s",
            sub.sub_id, sub.event_type_pattern, sub.subscriber_node, sub.callback_action,
        )
        return sub.sub_id

    async def unsubscribe(self, sub_id: str) -> bool:
        """Cancel a subscription. Returns True if it existed."""
        async with self._lock:
            if sub_id not in self._subscriptions:
                return False
            del self._subscriptions[sub_id]
        logger.info("EventBus unsubscribe: sub_id=%s", sub_id)
        return True

    async def get_subscriptions(self) -> list[Subscription]:
        """Return all active subscriptions (with current delivery counts)."""
        async with self._lock:
            result = []
            for state in self._subscriptions.values():
                # Return a copy with up-to-date delivery_count
                updated = state.sub.model_copy(update={
                    "delivery_count": state.delivery_count,
                    "last_delivered_at": state.last_delivered_at,
                })
                result.append(updated)
        return result

    async def get_events(
        self,
        *,
        event_type: str | None = None,
        source_node: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[Event]:
        """Query the in-memory event log with optional filters."""
        async with self._lock:
            events = list(self._event_log)

        if event_type:
            events = [e for e in events if _matches_pattern(event_type, e.event_type)]
        if source_node:
            events = [e for e in events if e.source_node == source_node]
        if since is not None:
            events = [e for e in events if e.timestamp >= since]

        return events[-limit:]  # most recent N

    # ------------------------------------------------------------------
    # Internal: subscription matching
    # ------------------------------------------------------------------

    def _match_subscriptions(self, event: Event) -> list[str]:
        """Return sub_ids that match the event (call inside lock)."""
        matched: list[str] = []
        now = time.time()

        for sub_id, state in list(self._subscriptions.items()):
            sub = state.sub

            # 1. Pattern match
            if not _matches_pattern(sub.event_type_pattern, event.event_type):
                continue

            # 2. Source node filter
            if sub.source_node and sub.source_node != event.source_node:
                continue

            # 3. Payload filter
            if sub.payload_filter and not _matches_payload_filter(
                sub.payload_filter, event.payload
            ):
                continue

            # 4. Debounce
            if sub.debounce_seconds > 0 and state.last_delivered_at is not None:
                elapsed = now - state.last_delivered_at
                if elapsed < sub.debounce_seconds:
                    logger.debug(
                        "EventBus debounce: sub=%s elapsed=%.2f < %.2f",
                        sub_id, elapsed, sub.debounce_seconds,
                    )
                    continue

            # 5. Max deliveries
            if sub.max_deliveries is not None and state.delivery_count >= sub.max_deliveries:
                continue

            matched.append(sub_id)
            # Update tracking state eagerly (will be confirmed on successful delivery)
            state.last_delivered_at = now

        return matched

    # ------------------------------------------------------------------
    # Internal: background delivery worker
    # ------------------------------------------------------------------

    async def _delivery_worker(self) -> None:
        """Background task: dequeues (event, sub_id) pairs and dispatches."""
        logger.debug("EventBus delivery worker started")
        while self._running:
            try:
                try:
                    event, sub_id = await asyncio.wait_for(
                        self._delivery_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                await self._deliver_with_retry(event, sub_id)
                self._delivery_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception("EventBus delivery worker unexpected error: %s", exc)

        logger.debug("EventBus delivery worker stopped")

    async def _deliver_with_retry(self, event: Event, sub_id: str) -> None:
        """Attempt delivery with exponential backoff, up to retry_count times."""
        async with self._lock:
            state = self._subscriptions.get(sub_id)
        if state is None:
            return  # subscription was cancelled between match and delivery

        sub = state.sub
        delay = 1.0
        last_error: str = ""

        for attempt in range(1, self._retry_count + 1):
            try:
                await self._dispatch_action(event, sub)
                # Success — update delivery count
                async with self._lock:
                    st = self._subscriptions.get(sub_id)
                    if st:
                        st.delivery_count += 1
                        # Auto-remove if max_deliveries reached
                        if (
                            sub.max_deliveries is not None
                            and st.delivery_count >= sub.max_deliveries
                        ):
                            del self._subscriptions[sub_id]
                            logger.info(
                                "EventBus: sub=%s max_deliveries=%d reached — auto-unsubscribed",
                                sub_id, sub.max_deliveries,
                            )
                return

            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if attempt < self._retry_count:
                    logger.warning(
                        "EventBus delivery attempt %d/%d failed for sub=%s: %s — retry in %.1fs",
                        attempt, self._retry_count, sub_id, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= self._retry_backoff

        logger.error(
            "EventBus delivery FAILED after %d attempts for sub=%s event=%s: %s",
            self._retry_count, sub_id, event.event_id, last_error,
        )

    async def _dispatch_action(self, event: Event, sub: Subscription) -> None:
        """Construct and dispatch an ActionRequest for one subscription delivery."""
        if self._gateway_router is None:
            # No router (unit test / standalone mode) — log only
            logger.debug(
                "EventBus dispatch (no router): node=%s action=%s event=%s",
                sub.subscriber_node, sub.callback_action, event.event_type,
            )
            return

        # Merge callback_params_template with the full event object
        params: dict[str, Any] = {
            **sub.callback_params_template,
            "event": event.model_dump(),
        }

        request = ActionRequest(
            target_node_id=sub.subscriber_node,
            payload=ActionPayload(action=sub.callback_action, params=params),
            caller_token=self._caller_token,
        )

        try:
            result = await asyncio.wait_for(
                self._gateway_router.route(request),
                timeout=self._delivery_timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Delivery timeout ({self._delivery_timeout}s) for sub={sub.sub_id}"
            )

        from runtime.models import ErrorResponse
        if isinstance(result, ErrorResponse):
            raise RuntimeError(f"Delivery error: {result.error}")

        logger.debug(
            "EventBus delivered: event=%s → node=%s action=%s",
            event.event_id, sub.subscriber_node, sub.callback_action,
        )

    # ------------------------------------------------------------------
    # Internal: JSONL persistence
    # ------------------------------------------------------------------

    async def _persist_event(self, event: Event) -> None:
        """Append event as a JSON line to the persistence file."""
        try:
            loop = asyncio.get_event_loop()
            line = event.model_dump_json() + "\n"
            await loop.run_in_executor(None, self._write_line, line)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventBus persistence write failed: %s", exc)

    def _write_line(self, line: str) -> None:
        """Synchronous file write (runs in thread pool)."""
        assert self._persistence_path is not None
        with self._persistence_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    async def load_persisted_events(self) -> int:
        """Load events from JSONL persistence file into in-memory log.

        Returns count of events loaded. Call during startup if persistence
        is enabled to replay events across restarts.
        """
        if not self._persistence_path or not self._persistence_path.exists():
            return 0

        loaded = 0
        try:
            with self._persistence_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        event = Event.model_validate(data)
                        self._event_log.append(event)
                        loaded += 1
                    except (json.JSONDecodeError, ValueError) as exc:
                        logger.warning("EventBus: skipping malformed persisted event: %s", exc)
        except OSError as exc:
            logger.warning("EventBus: could not read persistence file: %s", exc)

        logger.info("EventBus: loaded %d persisted events from %s", loaded, self._persistence_path)
        return loaded
