"""Scheduler — 3-level autonomy engine for GNOT v6.0 (Phase 2).

Autonomy levels:
  Level 1 — Condition self-check: periodically call check_action on target;
             if output["should_run"] is truthy, dispatch run_action.
  Level 2 — Adaptive poll: handled in WorkerAgent (backoff logic), not here.
  Level 3 — Event-triggered: subscribe to EventBus; on match dispatch run_action.
  Bonus   — Cron: fire run_action at cron-scheduled intervals.
  Bonus   — Once: fire run_action at a specific unix timestamp.

Thread-safety: schedule registry protected by asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from runtime.models import (
    ActionPayload,
    ActionRequest,
    ScheduleEntry,
    Subscription,
)

if TYPE_CHECKING:
    from runtime.event_bus import EventBus
    from runtime.gateway_router import GatewayRouter

logger = logging.getLogger(__name__)

# Cron polling resolution: check every N seconds whether a cron trigger should fire.
CRON_TICK_SECONDS: float = 10.0
ONCE_TICK_SECONDS: float = 0.1     # fine-grained check for once triggers
CONDITION_JITTER_SECONDS: float = 0.5     # small jitter to spread condition checks


class Scheduler:
    """Central scheduler managing all trigger types for one GNOT node.

    Lifecycle:
        scheduler = Scheduler(gateway_router, event_bus, node_id)
        await scheduler.start()   # starts background loops per trigger type
        ...
        await scheduler.stop()    # cancels all background tasks
    """

    def __init__(
        self,
        *,
        gateway_router: "GatewayRouter | None" = None,
        event_bus: "EventBus | None" = None,
        node_id: str = "unknown",
        caller_token: str | None = None,
    ) -> None:
        self._gateway_router = gateway_router
        self._event_bus = event_bus
        self._node_id = node_id
        self._caller_token = caller_token

        self._entries: dict[str, ScheduleEntry] = {}
        self._lock = asyncio.Lock()
        self._running = False

        # Per-entry background tasks (condition + cron + once)
        self._entry_tasks: dict[str, asyncio.Task] = {}
        # Cron ticker tasks (one per cron entry)
        self._cron_tasks: dict[str, asyncio.Task] = {}
        # Once tasks
        self._once_tasks: dict[str, asyncio.Task] = {}
        # Active run semaphores (per schedule_id)
        self._running_counts: dict[str, int] = {}
        self._running_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start all background loops."""
        if self._running:
            return
        self._running = True
        logger.info("Scheduler started — node=%s", self._node_id)
        # Start existing entries (loaded from config before start())
        async with self._lock:
            for entry in self._entries.values():
                if entry.enabled:
                    self._spawn_entry_task(entry)

    async def stop(self) -> None:
        """Cancel all background tasks."""
        self._running = False
        tasks = list(self._entry_tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._entry_tasks.clear()
        logger.info("Scheduler stopped — node=%s", self._node_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_entry(self, entry: ScheduleEntry) -> str:
        """Register a new schedule entry. Returns schedule_id."""
        async with self._lock:
            self._entries[entry.schedule_id] = entry
        if self._running and entry.enabled:
            self._spawn_entry_task(entry)
        logger.info(
            "Scheduler add: id=%s type=%s target=%s action=%s",
            entry.schedule_id, entry.trigger_type, entry.target_node, entry.run_action,
        )
        return entry.schedule_id

    async def remove_entry(self, schedule_id: str) -> bool:
        """Cancel and remove a schedule entry."""
        async with self._lock:
            if schedule_id not in self._entries:
                return False
            del self._entries[schedule_id]
        # Cancel background task if running
        task = self._entry_tasks.pop(schedule_id, None)
        if task:
            task.cancel()
        logger.info("Scheduler remove: id=%s", schedule_id)
        return True

    async def patch_entry(self, schedule_id: str, updates: dict[str, Any]) -> ScheduleEntry | None:
        """Partially update a schedule entry."""
        async with self._lock:
            entry = self._entries.get(schedule_id)
            if entry is None:
                return None
            updated = entry.model_copy(update={
                k: v for k, v in updates.items() if v is not None
            })
            self._entries[schedule_id] = updated

        # Restart task if enabled state changed or config changed
        old_task = self._entry_tasks.pop(schedule_id, None)
        if old_task:
            old_task.cancel()
        if self._running and updated.enabled:
            self._spawn_entry_task(updated)

        return updated

    async def list_entries(self) -> list[ScheduleEntry]:
        """Return all registered schedule entries."""
        async with self._lock:
            return list(self._entries.values())

    async def trigger_manual(self, schedule_id: str) -> bool:
        """Manually trigger a schedule entry (debug). Returns True if dispatched."""
        async with self._lock:
            entry = self._entries.get(schedule_id)
        if entry is None:
            return False
        await self._dispatch_run_guarded(entry)
        return True

    # ------------------------------------------------------------------
    # Internal: task spawning
    # ------------------------------------------------------------------

    def _spawn_entry_task(self, entry: ScheduleEntry) -> None:
        """Create background task for an entry based on trigger_type."""
        if entry.trigger_type == "condition":
            task = asyncio.create_task(
                self._condition_loop(entry),
                name=f"sched-condition-{entry.schedule_id}",
            )
        elif entry.trigger_type == "cron":
            task = asyncio.create_task(
                self._cron_loop(entry),
                name=f"sched-cron-{entry.schedule_id}",
            )
        elif entry.trigger_type == "once":
            task = asyncio.create_task(
                self._once_task(entry),
                name=f"sched-once-{entry.schedule_id}",
            )
        elif entry.trigger_type == "event":
            task = asyncio.create_task(
                self._event_subscribe(entry),
                name=f"sched-event-{entry.schedule_id}",
            )
        else:
            logger.warning("Unknown trigger_type=%s for schedule_id=%s", entry.trigger_type, entry.schedule_id)
            return
        self._entry_tasks[entry.schedule_id] = task

    # ------------------------------------------------------------------
    # Level 1: Condition trigger loop
    # ------------------------------------------------------------------

    async def _condition_loop(self, entry: ScheduleEntry) -> None:
        """Periodically call check_action; fire run_action if should_run is truthy."""
        if not entry.check_action:
            logger.warning(
                "Condition schedule %s has no check_action — disabled", entry.schedule_id
            )
            return

        logger.debug(
            "Condition loop started: id=%s check=%s interval=%ds",
            entry.schedule_id, entry.check_action, entry.check_interval_seconds,
        )

        # Small startup jitter to spread condition checks across time
        await asyncio.sleep(CONDITION_JITTER_SECONDS)

        while self._running:
            try:
                # Re-fetch entry in case it was patched
                async with self._lock:
                    current = self._entries.get(entry.schedule_id)
                if current is None or not current.enabled:
                    break

                should_run = await self._run_check_action(current)
                if should_run:
                    await self._dispatch_run_guarded(current)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "Condition check error for schedule %s: %s",
                    entry.schedule_id, exc,
                )

            try:
                await asyncio.sleep(current.check_interval_seconds)
            except asyncio.CancelledError:
                break

    async def _run_check_action(self, entry: ScheduleEntry) -> bool:
        """Call check_action on target_node; return True if should_run."""
        if self._gateway_router is None:
            logger.debug("Condition check: no router (standalone mode) — assuming True")
            return False

        request = ActionRequest(
            target_node_id=entry.target_node,
            payload=ActionPayload(
                action=entry.check_action,  # type: ignore[arg-type]
                params=entry.check_params,
            ),
            caller_token=self._caller_token,
        )

        try:
            from runtime.models import ErrorResponse, SyncActionResponse
            result = await asyncio.wait_for(
                self._gateway_router.route(request),
                timeout=float(entry.timeout_seconds),
            )
            if isinstance(result, ErrorResponse):
                logger.warning(
                    "Condition check for %s returned error: %s",
                    entry.schedule_id, result.error,
                )
                return False
            if isinstance(result, SyncActionResponse):
                return bool(result.output.get("should_run", False))
            return False
        except asyncio.TimeoutError:
            logger.warning("Condition check timed out for schedule %s", entry.schedule_id)
            return False
        except Exception as exc:
            logger.warning("Condition check failed for %s: %s", entry.schedule_id, exc)
            return False

    # ------------------------------------------------------------------
    # Cron trigger loop
    # ------------------------------------------------------------------

    async def _cron_loop(self, entry: ScheduleEntry) -> None:
        """Fire run_action at intervals defined by cron_expression."""
        if not entry.cron_expression:
            logger.warning(
                "Cron schedule %s has no cron_expression — disabled", entry.schedule_id
            )
            return

        try:
            from croniter import croniter
        except ImportError:
            logger.error(
                "croniter not installed — cannot run cron schedule %s. "
                "Install with: pip install croniter",
                entry.schedule_id,
            )
            return

        logger.debug(
            "Cron loop started: id=%s expr='%s'",
            entry.schedule_id, entry.cron_expression,
        )

        # Calculate first next run time
        cron = croniter(entry.cron_expression, time.time())
        next_run = cron.get_next(float)

        while self._running:
            try:
                async with self._lock:
                    current = self._entries.get(entry.schedule_id)
                if current is None or not current.enabled:
                    break

                now = time.time()
                if now >= next_run:
                    await self._dispatch_run_guarded(current)
                    # Advance to next scheduled time
                    cron = croniter(current.cron_expression, now)
                    next_run = cron.get_next(float)
                    logger.debug(
                        "Cron %s fired — next run at %.0f", entry.schedule_id, next_run
                    )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Cron loop error for %s: %s", entry.schedule_id, exc)

            try:
                await asyncio.sleep(CRON_TICK_SECONDS)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Once trigger task
    # ------------------------------------------------------------------

    async def _once_task(self, entry: ScheduleEntry) -> None:
        """Fire run_action at the specified unix timestamp (once)."""
        if entry.run_at is None:
            logger.warning("Once schedule %s has no run_at — disabled", entry.schedule_id)
            return

        logger.debug(
            "Once task: id=%s run_at=%.0f (in %.1fs)",
            entry.schedule_id, entry.run_at, entry.run_at - time.time(),
        )

        while self._running:
            try:
                now = time.time()
                if now >= entry.run_at:
                    async with self._lock:
                        current = self._entries.get(entry.schedule_id)
                    if current and current.enabled:
                        await self._dispatch_run_guarded(current)
                    # Auto-disable after firing
                    await self.patch_entry(entry.schedule_id, {"enabled": False})
                    logger.info("Once schedule %s fired and auto-disabled", entry.schedule_id)
                    return  # task done

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Once task error for %s: %s", entry.schedule_id, exc)
                return  # don't loop on once-trigger error

            try:
                await asyncio.sleep(ONCE_TICK_SECONDS)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Level 3: Event trigger subscription
    # ------------------------------------------------------------------

    async def _event_subscribe(self, entry: ScheduleEntry) -> None:
        """Register EventBus subscription that fires run_action on match."""
        if self._event_bus is None:
            logger.warning(
                "Event schedule %s: EventBus not available — disabled", entry.schedule_id
            )
            return
        if not entry.on_event_type:
            logger.warning(
                "Event schedule %s: no on_event_type — disabled", entry.schedule_id
            )
            return

        # Create a synthetic callback action name — the subscription will invoke
        # a local _scheduler_event_callback action routed to self.
        # We build a closure-based delivery by using an internal action name.
        # Since we own the EventBus, we register a subscription that calls our
        # internal _handle_event_trigger action on self (the gateway).

        sub = Subscription(
            sub_id=f"sched-ev-{entry.schedule_id}",
            subscriber_node=self._node_id,
            callback_action="_scheduler_event_trigger",
            callback_params_template={"schedule_id": entry.schedule_id},
            event_type_pattern=entry.on_event_type,
            payload_filter=entry.on_payload_filter,
            description=f"Scheduler trigger for {entry.schedule_id}",
        )

        await self._event_bus.subscribe(sub)
        logger.info(
            "Event schedule subscribed: id=%s pattern=%s",
            entry.schedule_id, entry.on_event_type,
        )

        # Keep task alive while running (subscription is removed on stop)
        try:
            while self._running:
                async with self._lock:
                    current = self._entries.get(entry.schedule_id)
                if current is None or not current.enabled:
                    break
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            pass
        finally:
            await self._event_bus.unsubscribe(sub.sub_id)
            logger.debug("Event schedule unsubscribed: id=%s", entry.schedule_id)

    async def handle_event_trigger(self, schedule_id: str, event_payload: dict) -> None:
        """Called by the event delivery system when a matching event arrives.

        This is invoked by the _scheduler_event_trigger action in gateway_router,
        which bridges EventBus delivery into the Scheduler.
        """
        async with self._lock:
            entry = self._entries.get(schedule_id)
        if entry is None or not entry.enabled:
            return
        logger.info(
            "Event trigger fired for schedule %s (event payload keys: %s)",
            schedule_id, list(event_payload.keys()),
        )
        # Merge event payload into run_params for this execution
        merged_params = {**entry.run_params, "trigger_event": event_payload}
        run_entry = entry.model_copy(update={"run_params": merged_params})
        await self._dispatch_run_guarded(run_entry)

    # ------------------------------------------------------------------
    # Internal: run dispatch with concurrency guard
    # ------------------------------------------------------------------

    async def _dispatch_run_guarded(self, entry: ScheduleEntry) -> None:
        """Dispatch run_action respecting max_concurrent and skip_if_running."""
        async with self._running_lock:
            count = self._running_counts.get(entry.schedule_id, 0)
            if entry.skip_if_running and count >= entry.max_concurrent:
                logger.debug(
                    "Skipping schedule %s — already %d running (max=%d)",
                    entry.schedule_id, count, entry.max_concurrent,
                )
                return
            if count >= entry.max_concurrent:
                logger.debug(
                    "Max concurrent reached for %s (%d/%d) — skipping",
                    entry.schedule_id, count, entry.max_concurrent,
                )
                return
            self._running_counts[entry.schedule_id] = count + 1

        try:
            await self._dispatch_run(entry)
        finally:
            async with self._running_lock:
                self._running_counts[entry.schedule_id] = max(
                    0, self._running_counts.get(entry.schedule_id, 0) - 1
                )

    async def _dispatch_run(self, entry: ScheduleEntry, *, attempt: int = 1) -> None:
        """Dispatch run_action to target_node via GatewayRouter."""
        # Update last_run_at on the stored entry
        async with self._lock:
            stored = self._entries.get(entry.schedule_id)
            if stored:
                self._entries[entry.schedule_id] = stored.model_copy(
                    update={"last_run_at": time.time()}
                )

        if self._gateway_router is None:
            # Standalone / test mode — log only
            logger.info(
                "Scheduler dispatch (no router): id=%s target=%s action=%s",
                entry.schedule_id, entry.target_node, entry.run_action,
            )
            return

        request = ActionRequest(
            target_node_id=entry.target_node,
            payload=ActionPayload(action=entry.run_action, params=entry.run_params),
            caller_token=self._caller_token,
        )

        try:
            from runtime.models import ErrorResponse
            result = await asyncio.wait_for(
                self._gateway_router.route(request),
                timeout=float(entry.timeout_seconds),
            )

            if isinstance(result, ErrorResponse):
                logger.warning(
                    "Schedule %s run_action returned error: %s",
                    entry.schedule_id, result.error,
                )
                await self._maybe_retry(entry, attempt, error=result.error)
                await self._update_last_result(entry.schedule_id, "failed")
                return

            await self._update_last_result(entry.schedule_id, "success")
            logger.info(
                "Schedule %s dispatched successfully (attempt=%d)",
                entry.schedule_id, attempt,
            )

        except asyncio.TimeoutError:
            logger.warning(
                "Schedule %s run_action timed out after %ds",
                entry.schedule_id, entry.timeout_seconds,
            )
            await self._maybe_retry(entry, attempt, error="timeout")
            await self._update_last_result(entry.schedule_id, "failed")

        except Exception as exc:
            logger.warning(
                "Schedule %s run_action raised: %s", entry.schedule_id, exc
            )
            await self._maybe_retry(entry, attempt, error=str(exc))
            await self._update_last_result(entry.schedule_id, "failed")

    async def _maybe_retry(
        self, entry: ScheduleEntry, attempt: int, error: str | None = None
    ) -> None:
        """Retry run_action if retry_on_failure allows it."""
        if attempt <= entry.retry_on_failure:
            delay = 2.0 ** (attempt - 1)  # exponential backoff: 1s, 2s, 4s...
            logger.info(
                "Retrying schedule %s (attempt %d/%d) in %.0fs: %s",
                entry.schedule_id, attempt + 1, entry.retry_on_failure + 1, delay, error,
            )
            await asyncio.sleep(delay)
            await self._dispatch_run(entry, attempt=attempt + 1)

    async def _update_last_result(self, schedule_id: str, result: str) -> None:
        async with self._lock:
            entry = self._entries.get(schedule_id)
            if entry:
                self._entries[schedule_id] = entry.model_copy(
                    update={"last_result": result}
                )
