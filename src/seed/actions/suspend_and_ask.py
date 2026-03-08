"""suspend_and_ask — v6.0 Phase 4 seed action.

This action is never executed directly through the ActionExecutor pipeline.
Instead, it is a **sentinel action** intercepted by IntentHandler._execute_tool_call()
before routing occurs.

The LLM calls:
    mesh_action("self", "suspend_and_ask", {
        "question": "Should null user throw exception or return empty?",
        "ask_node": "architect-A",       # agent-to-agent (optional)
        "target_role": "pm",             # agent-to-human (optional, Phase 6)
        "timeout_seconds": 7200,
        "timeout_action": "use_assumption",
        "assumption": "throw UserNotFoundException",
    })

The IntentHandler catches the TaskSuspendedException raised by the interception
logic and saves a TaskCheckpoint before returning TaskSuspendedResponse to the
HTTP client.

This file exists so ActionLoader can discover and advertise the action spec,
making the LLM aware that suspend_and_ask is a valid capability.
"""

from __future__ import annotations

from typing import Any


async def suspend_and_ask(
    question: str,
    ask_node: str = "",
    target_role: str = "",
    timeout_seconds: int = 86400,
    timeout_action: str = "use_assumption",
    assumption: str = "",
    **_kwargs: Any,
) -> dict[str, Any]:
    """Sentinel action — intercepted by IntentHandler, never runs here.

    If called directly (e.g. in unit tests), raises RuntimeError to surface
    the misconfiguration.
    """
    raise RuntimeError(
        "suspend_and_ask must be called via mesh_action('self', 'suspend_and_ask', ...) "
        "from inside an IntentHandler loop — not invoked directly."
    )

# ActionLoader expects a callable named 'run' — alias the main function.
run = suspend_and_ask
