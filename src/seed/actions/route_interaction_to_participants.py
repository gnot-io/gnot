"""route_interaction_to_participants — v6.0 Phase 6 seed action.

Called (typically by the ExternalAdapter node via its scheduler subscription)
when a participant.input_required event is received.

This action:
  1. Opens an InteractionThread in the ChannelLog for the cluster.
  2. Looks up participants with the required role.
  3. Notifies each participant via their transport (webhook / polling).
  4. Returns a summary of participants notified.

The LLM (or a scheduler subscription) calls:
    mesh_action("external-adapter", "route_interaction_to_participants", {
        "question_id": "q-abc123",
        "question": "Should we use PostgreSQL or MySQL?",
        "required_role": "pm",
        "source_agent": "dev-A",
        "cluster_id": "cluster-xyz",
    })
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ASYNC: bool = False


async def run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Route an agent question to participants with the required role.

    Args:
        params:
            question_id (str):   Correlation ID for the question (from checkpoint).
            question (str):      The question text.
            required_role (str): Role needed to answer (e.g. "pm").
            source_agent (str):  Node ID of the asking agent.
            cluster_id (str):    Cluster context for participant lookup.

    Returns:
        dict with question_id, required_role, notified participants list,
        participants_found count, and notification_errors list.
    """
    question_id = params.get("question_id", "")
    question = params.get("question", "")
    required_role = params.get("required_role", "")
    source_agent = params.get("source_agent", "")
    cluster_id = params.get("cluster_id", "")

    if not question_id or not question or not required_role:
        raise ValueError(
            "route_interaction_to_participants requires: question_id, question, required_role"
        )

    interaction_router = context.get("interaction_router")
    if interaction_router is None:
        # Fallback: try to use registry + channel_log directly
        participant_registry = context.get("participant_registry")
        channel_log = context.get("channel_log")
        if participant_registry is None or channel_log is None:
            logger.warning(
                "route_interaction_to_participants: no interaction_router or "
                "participant_registry/channel_log in context — skipping routing"
            )
            return {
                "status": "skipped",
                "reason": "interaction_router not available",
                "question_id": question_id,
            }
        # Build a temporary router
        from runtime.interaction_router import InteractionRouter
        interaction_router = InteractionRouter(
            participant_registry=participant_registry,
            channel_log=channel_log,
            event_bus=None,
            node_id=context.get("node_id", "unknown"),
        )

    result = await interaction_router.route(
        question_id=question_id,
        question_text=question,
        required_role=required_role,
        source_agent=source_agent or context.get("node_id", "unknown"),
        cluster_id=cluster_id,
    )

    logger.info(
        "route_interaction_to_participants: question=%s role=%s notified=%d",
        question_id, required_role, len(result.get("notified", [])),
    )
    return result


# ActionLoader expects 'run'
