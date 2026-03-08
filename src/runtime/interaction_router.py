"""InteractionRouter — role-based routing for external participant interactions.

v6.0 Phase 6 — External Participant Interaction.

When an agent calls suspend_and_ask with a target_role, the runtime emits a
participant.input_required event.  The InteractionRouter picks up this event,
creates an InteractionThread in the ChannelLog, and notifies all participants
with the matching role.

Routing semantics (from spec §9.2):
  - Notify ALL matching participants (no load balancing, no priority).
  - First valid "answer" reply resolves the thread.
  - Others may still add comments or tags.
  - No availability check — pure role match.

Transport support:
  - webhook:  POST to transport_target with question payload (async HTTP).
  - polling:  Nothing extra — participants poll GET /channels/{id}/pending.
  - session:  Future (Phase 7+ SSE push).  Currently treated as polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from runtime.models import ExternalParticipant, InteractionThread

logger = logging.getLogger(__name__)


class InteractionRouter:
    """Routes agent questions to external participants based on role.

    Injected dependencies:
        participant_registry: ExternalParticipantRegistry
        channel_log:          ChannelLog
        event_bus:            EventBus | None  (for emitting participant.* events)
        node_id:              str              (source node for emitted events)
    """

    def __init__(
        self,
        participant_registry: Any,   # ExternalParticipantRegistry
        channel_log: Any,            # ChannelLog
        event_bus: Any | None = None,
        node_id: str = "unknown",
    ) -> None:
        self._registry = participant_registry
        self._channel_log = channel_log
        self._event_bus = event_bus
        self._node_id = node_id

    # ── public API ─────────────────────────────────────────────────────────

    async def route(
        self,
        question_id: str,
        question_text: str,
        required_role: str,
        source_agent: str,
        cluster_id: str,
    ) -> dict[str, Any]:
        """Open an InteractionThread and notify matching participants.

        Returns a summary dict with notified participant IDs and thread info.
        """
        # Create the thread
        thread = InteractionThread(
            question_id=question_id,
            source_agent=source_agent,
            required_role=required_role,
            question_text=question_text,
            cluster_id=cluster_id,
            status="open",
        )
        await self._channel_log.open_thread(thread)

        # Find participants with the required role
        participants = await self._registry.list_by_role(
            role=required_role,
            cluster_id=cluster_id,
            active_only=True,
        )

        if not participants:
            logger.warning(
                "InteractionRouter: no participants with role=%s in cluster=%s",
                required_role, cluster_id,
            )

        # Notify each participant
        notified: list[str] = []
        notification_errors: list[str] = []

        for p in participants:
            try:
                await self._notify_participant(p, thread)
                notified.append(p.participant_id)
            except Exception as exc:
                logger.warning(
                    "InteractionRouter: failed to notify %s via %s — %s",
                    p.participant_id, p.transport, exc,
                )
                notification_errors.append(f"{p.participant_id}: {exc}")

        # Emit participant.input_required event
        await self._emit_input_required(thread, notified)

        logger.info(
            "InteractionRouter: routed question=%s role=%s → notified %d participant(s)",
            question_id, required_role, len(notified),
        )
        return {
            "question_id": question_id,
            "required_role": required_role,
            "cluster_id": cluster_id,
            "thread_status": "open",
            "participants_found": len(participants),
            "notified": notified,
            "notification_errors": notification_errors,
        }

    async def handle_answer(
        self,
        cluster_id: str,
        question_id: str,
        participant_id: str,
        content: str,
    ) -> None:
        """Called after a thread is resolved — emit participant.answered event."""
        await self._emit_participant_answered(
            cluster_id=cluster_id,
            question_id=question_id,
            participant_id=participant_id,
            content=content,
        )
        # Also emit clarification.answered so TaskPool can resume the suspended task
        await self._emit_clarification_answered(
            question_id=question_id,
            answer=content,
            answered_by=participant_id,
        )

    # ── notification helpers ────────────────────────────────────────────────

    async def _notify_participant(
        self,
        participant: ExternalParticipant,
        thread: InteractionThread,
    ) -> None:
        """Notify a participant based on their transport preference."""
        if participant.transport == "webhook" and participant.transport_target:
            await self._notify_webhook(participant, thread)
        elif participant.transport in ("polling", "session"):
            # Polling: participant will discover the thread via GET /channels/{id}/pending
            # Session: future SSE push (Phase 7) — polling fallback for now
            logger.debug(
                "InteractionRouter: participant %s is polling — no push needed",
                participant.participant_id,
            )
        else:
            logger.debug(
                "InteractionRouter: unrecognised transport=%s for participant=%s",
                participant.transport, participant.participant_id,
            )

    async def _notify_webhook(
        self,
        participant: ExternalParticipant,
        thread: InteractionThread,
    ) -> None:
        """POST notification payload to participant's webhook URL."""
        try:
            import aiohttp  # type: ignore
        except ImportError:
            logger.warning("InteractionRouter: aiohttp not installed — webhook skipped")
            return

        payload = {
            "event": "participant.input_required",
            "question_id": thread.question_id,
            "question": thread.question_text,
            "required_role": thread.required_role,
            "source_agent": thread.source_agent,
            "cluster_id": thread.cluster_id,
            "participant_id": participant.participant_id,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                participant.transport_target,
                json=payload,
                headers={"Authorization": f"Bearer {participant.auth_token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"Webhook returned HTTP {resp.status}: {text[:200]}")
                logger.info(
                    "InteractionRouter: webhook delivered to %s → HTTP %d",
                    participant.participant_id, resp.status,
                )

    # ── event emission helpers ──────────────────────────────────────────────

    async def _emit_input_required(
        self,
        thread: InteractionThread,
        notified: list[str],
    ) -> None:
        if self._event_bus is None:
            return
        try:
            from runtime.models import Event
            event = Event(
                event_type="participant.input_required",
                source_node=self._node_id,
                payload={
                    "question_id": thread.question_id,
                    "question": thread.question_text,
                    "required_role": thread.required_role,
                    "source_agent": thread.source_agent,
                    "cluster_id": thread.cluster_id,
                    "notified_participants": notified,
                },
                correlation_id=thread.question_id,
                reply_to=self._node_id,
            )
            await self._event_bus.emit(event)
        except Exception as exc:
            logger.error("InteractionRouter: failed to emit participant.input_required — %s", exc)

    async def _emit_participant_answered(
        self,
        cluster_id: str,
        question_id: str,
        participant_id: str,
        content: str,
    ) -> None:
        if self._event_bus is None:
            return
        try:
            from runtime.models import Event
            event = Event(
                event_type="participant.answered",
                source_node=self._node_id,
                payload={
                    "question_id": question_id,
                    "cluster_id": cluster_id,
                    "participant_id": participant_id,
                    "answer": content,
                },
                correlation_id=question_id,
            )
            await self._event_bus.emit(event)
        except Exception as exc:
            logger.error("InteractionRouter: failed to emit participant.answered — %s", exc)

    async def _emit_clarification_answered(
        self,
        question_id: str,
        answer: str,
        answered_by: str,
    ) -> None:
        """Emit clarification.answered so TaskPool can resume the suspended task."""
        if self._event_bus is None:
            return
        try:
            from runtime.models import Event
            event = Event(
                event_type="clarification.answered",
                source_node=self._node_id,
                payload={
                    "question_id": question_id,
                    "answer": answer,
                    "answered_by": answered_by,
                },
                correlation_id=question_id,
            )
            await self._event_bus.emit(event)
        except Exception as exc:
            logger.error("InteractionRouter: failed to emit clarification.answered — %s", exc)
