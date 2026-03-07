"""Intent handler — ReAct agent loop for POST /intent.

v5.9 — Accepts a natural-language prompt and autonomously orchestrates
the mesh to fulfil the request, returning a human-readable reply.

Architecture:
  This module replicates what Claude Web does manually:
  1. Receive user prompt
  2. Build context: available nodes + actions
  3. Call LLM with tool spec (mesh_action tool)
  4. If LLM calls a tool: execute action on mesh, feed result back
  5. Loop until LLM returns a plain reply (no more tool calls)
  6. Return final reply to caller

Tool design — single generic tool:
  mesh_action(target_node_id, action, params)
  Instead of N×M tools (one per action×node), we expose one generic tool.
  The system prompt explains available nodes and actions, so the LLM can
  choose the right combination. This keeps the tool spec compact.

ReAct loop depth:
  Controlled by config.intent_max_turns (default 10). If limit is hit,
  returns whatever partial reply the LLM has produced + truncated=True.

Async jobs:
  When an action returns a job_id (async), the handler polls until
  completion with exponential backoff (cap 10s). The poll timeout
  mirrors config.pull_job_timeout_seconds.

Error handling:
  Tool errors are fed back to LLM as tool result with error content.
  The LLM can then decide to retry, try a different node, or explain
  the error to the user. We never raise from the loop — always return.

System prompt:
  A default system prompt is built dynamically from live mesh state
  (node list from NodeRegistry + action list from ActionRegistry).
  Operators can override via config.intent_system_prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from runtime.action_loader import ActionRegistry
from runtime.config import NodeConfig
from runtime.conversation_store import ConversationStore, Session
from runtime.gateway_router import GatewayRouter
from runtime.llm_client import LLMClient, LLMResponse, ToolCall
from runtime.models import (
    ActionRequest,
    AsyncActionResponse,
    ErrorResponse,
    IntentRequest,
    IntentResponse,
    SyncActionResponse,
    TraceInfo,
)
from runtime.credential_store import CredentialStore
from runtime.node_registry import NodeRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool spec — single generic mesh_action tool
# ---------------------------------------------------------------------------

MESH_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mesh_action",
        "description": (
            "Execute an action on a specific node in the execution mesh. "
            "Use this to run shell commands, read/write files, or any registered action. "
            "For long-running commands, the tool automatically polls until completion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_node_id": {
                    "type": "string",
                    "description": "ID of the target node (e.g. 'node-0', 'centos-dbserver').",
                },
                "action": {
                    "type": "string",
                    "description": (
                        "Action name to execute. Common actions: "
                        "'execute_command' (run shell command), "
                        "'read_file' (read file content), "
                        "'write_file' (write content to file). "
                        "Other actions depend on node capabilities."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Action parameters. "
                        "For execute_command: {command: str, timeout_seconds?: int}. "
                        "For read_file: {path: str}. "
                        "For write_file: {path: str, content: str}."
                    ),
                },
            },
            "required": ["target_node_id", "action", "params"],
        },
    },
}

# Poll backoff for async jobs: [1, 2, 4, 5, 5, 5, ...] seconds
_POLL_BACKOFF = [1, 2, 4, 5, 5, 5, 5, 5, 5, 5]

# ---------------------------------------------------------------------------
# IntentHandler
# ---------------------------------------------------------------------------

class IntentHandler:
    """Runs the ReAct agent loop for a single POST /intent call.

    Injected dependencies mirror what server.py already initialises.
    """

    def __init__(
        self,
        config: NodeConfig,
        llm_client: LLMClient,
        gateway_router: GatewayRouter,
        node_registry: NodeRegistry,
        action_registry: ActionRegistry,
        conversation_store: ConversationStore,
        schema_validator: object | None = None,
        credential_store: CredentialStore | None = None,
    ) -> None:
        self._config = config
        self._llm = llm_client
        self._router = gateway_router
        self._node_registry = node_registry
        self._action_registry = action_registry
        self._store = conversation_store
        self._schema_validator = schema_validator  # v5.11: for action spec in system prompt
        self._credential_store = credential_store  # v5.12: encrypted session credential store

    # ── public ───────────────────────────────────────────────────────────────

    async def handle(self, req: IntentRequest) -> IntentResponse:
        """Entry point — process one user prompt and return a reply."""
        max_turns = req.max_turns or self._config.intent_max_turns
        session = await self._store.get_or_create(req.session_id)
        # v5.12: merge new credentials into session store, retrieve full set
        caller_credentials: dict[str, str] = dict(req.caller_credentials)
        if self._credential_store is not None:
            if req.caller_credentials:
                await self._credential_store.merge(session.session_id, req.caller_credentials)
            stored_creds = await self._credential_store.get(session.session_id)
            # Merge: request creds take precedence over stored (allow update)
            caller_credentials = {**stored_creds, **caller_credentials}
            await self._credential_store.touch(session.session_id)

        # Append the new user message
        session.add_message("user", req.prompt)

        system_prompt = (
            self._config.intent_system_prompt
            or self._build_system_prompt(node_hint=req.node_hint)
        )

        actions_taken: list[str] = []
        tokens_used = 0
        turns = 0
        truncated = False
        reply = ""

        for turn in range(max_turns):
            turns = turn + 1

            # Build messages list for this LLM call
            messages = list(session.messages)

            try:
                llm_resp: LLMResponse = await self._llm.chat(
                    messages=messages,
                    system=system_prompt,
                    tools=[MESH_TOOL_SPEC],
                    tool_choice="auto",
                    temperature=0.2,   # low temp for deterministic tool use
                )
            except Exception as exc:
                logger.error("LLM call failed on turn %d: %s", turn, exc)
                reply = f"I encountered an error calling the LLM: {exc}"
                session.add_message("assistant", reply)
                break

            tokens_used += llm_resp.usage.get("total_tokens", 0)

            # ── Case 1: LLM wants to use a tool ──────────────────────────────
            if llm_resp.tool_calls:
                # Persist the assistant message (with tool_calls) to history
                session.add_raw(_build_assistant_tool_call_msg(llm_resp))

                # Execute all requested tool calls (usually 1, rarely parallel)
                for tc in llm_resp.tool_calls:
                    tool_result = await self._execute_tool_call(tc, caller_credentials)
                    brief = f"{tc.arguments.get('action', tc.name)} on {tc.arguments.get('target_node_id', '?')}"
                    actions_taken.append(brief)
                    logger.info("[intent] tool call: %s → %s", brief, tool_result[:120])

                    # Feed tool result back into history
                    session.add_raw({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": tool_result,
                    })

                # Loop continues — LLM will interpret the tool result
                continue

            # ── Case 2: LLM gave a plain reply (done) ────────────────────────
            reply = llm_resp.content or ""
            session.add_message("assistant", reply)
            break

        else:
            # Loop exhausted without a plain reply
            truncated = True
            reply = (
                reply
                or "I reached the maximum number of steps without completing the task. "
                   "Please refine your request or break it into smaller steps."
            )
            if not any(m.get("role") == "assistant" and m.get("content") == reply
                       for m in session.messages):
                session.add_message("assistant", reply)

        return IntentResponse(
            session_id=session.session_id,
            reply=reply,
            turns=turns,
            actions_taken=actions_taken,
            tokens_used=tokens_used,
            truncated=truncated,
        )

    # ── tool execution ───────────────────────────────────────────────────────

    async def _execute_tool_call(
        self, tc: ToolCall, caller_credentials: dict[str, str] | None = None
    ) -> str:
        """Execute a mesh_action tool call and return result as a string."""
        args = tc.arguments

        # Validate required fields
        target = args.get("target_node_id", "")
        action = args.get("action", "")
        params = args.get("params", {})

        if not target or not action:
            return json.dumps({"error": "INVALID_TOOL_ARGS", "detail": "target_node_id and action are required"})

        action_request = ActionRequest(
            target_node_id=target,
            payload={"action": action, "params": params},
            trace=TraceInfo(hop_count=0, route_path=[]),
            caller_credentials=caller_credentials or {},
        )

        try:
            result = await self._router.route(action_request)
        except Exception as exc:
            logger.error("[intent] route failed: %s", exc)
            return json.dumps({"error": "ROUTE_FAILED", "detail": str(exc)})

        if isinstance(result, ErrorResponse):
            return json.dumps({"error": result.error, "node_id": result.node_id})

        if isinstance(result, SyncActionResponse):
            return json.dumps({"status": "completed", "output": result.output})

        if isinstance(result, AsyncActionResponse):
            # Poll until done
            return await self._poll_job(result.job_id)

        return json.dumps({"error": "UNKNOWN_RESPONSE", "detail": str(result)})

    async def _poll_job(self, job_id: str) -> str:
        """Poll a job until completion, returning the result as JSON string."""
        timeout = self._config.pull_job_timeout_seconds
        start = time.time()
        backoff_iter = iter(_POLL_BACKOFF)

        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                return json.dumps({
                    "error": "POLL_TIMEOUT",
                    "job_id": job_id,
                    "elapsed_seconds": round(elapsed, 1),
                })

            try:
                status_resp = await self._router.route_result(job_id)
            except Exception as exc:
                return json.dumps({"error": "POLL_FAILED", "detail": str(exc)})

            if isinstance(status_resp, ErrorResponse):
                return json.dumps({"error": status_resp.error})

            status = getattr(status_resp, "status", "")
            status_str = status.value if hasattr(status, "value") else str(status)

            if status_str in ("completed", "failed"):
                return json.dumps({
                    "status": status_str,
                    "output": getattr(status_resp, "output", None),
                    "error": getattr(status_resp, "error", None),
                })

            # Still running — wait with backoff
            wait = next(backoff_iter, 5)
            logger.debug("[intent] job %s status=%s, waiting %ds", job_id, status_str, wait)
            await asyncio.sleep(wait)

    # ── system prompt ────────────────────────────────────────────────────────

    def _build_system_prompt(self, node_hint: str | None = None) -> str:
        """Build rich system prompt from capability tree including action specs (v5.11)."""
        own_actions = list(self._action_registry.keys())
        reachable = self._node_registry.build_capability_tree(own_actions)

        # Build own action specs from local schema_validator if available
        own_specs: dict[str, object] = {}
        if hasattr(self, "_schema_validator") and self._schema_validator:
            own_specs = self._schema_validator.build_all_action_specs(self._action_registry)

        def _render_node(node_id: str, actions: list[str],
                          action_specs: dict, via: str | None,
                          status: str = "unknown") -> list[str]:
            result = []
            via_str = f" via {via}" if via else " (direct)"
            status_marker = " [UNREACHABLE — do not call]" if status == "unreachable" else ""
            result.append(f"  - {node_id}{via_str}{status_marker}")
            for aname in sorted(actions):
                spec = action_specs.get(aname)
                if spec and spec.description:
                    result.append(f"    ┌─ {aname}: {spec.description}")
                else:
                    result.append(f"    ┌─ {aname}")
                if spec and spec.params_schema:
                    for pname, pdef in spec.params_schema.items():
                        ptype = pdef.get("type", "any") if isinstance(pdef, dict) else "any"
                        pdesc = pdef.get("description", "") if isinstance(pdef, dict) else ""
                        result.append(f"    │  param {pname} ({ptype}): {pdesc}")
                if spec and spec.caller_credentials:
                    for cname, cspec in spec.caller_credentials.items():
                        req_marker = "required" if cspec.required else "optional"
                        result.append(
                            f"    │  ⚠ caller_credential {cname} ({req_marker}): {cspec.description}"
                        )
                        if cspec.hint:
                            result.append(f"    │    hint: {cspec.hint}")
            return result

        cap_lines: list[str] = []
        # Render own node using the same _render_node helper so specs + creds are shown
        cap_lines.extend(_render_node(
            self._config.node_id + " [gateway, THIS NODE]",
            own_actions, own_specs, None,
        ))

        for node_id, cap in sorted(reachable.items()):
            cap_lines.extend(
                _render_node(node_id, cap.actions, cap.action_specs, cap.next_hop, cap.status)
            )

        capability_str = "\n".join(cap_lines) or "  (no nodes registered)"
        node_hint_str = (
            f"\n**Preferred node:** Start with `{node_hint}` if relevant.\n"
            if node_hint else ""
        )
        gw = self._config.node_id

        return (
            "You are an AI orchestrator for an Execution Mesh.\n\n"
            "You autonomously execute tasks using the `mesh_action` tool. Keep working until\n"
            "the goal is fully achieved. Ask only for information you cannot discover yourself.\n\n"
            "**You never ask the user to run commands manually. You run them yourself.**\n"
            f"{node_hint_str}\n"
            "## Mesh topology — nodes, actions, and requirements\n"
            f"{capability_str}\n\n"
            "## Routing\n"
            "- Target ANY node listed above; routing is automatic through all NAT layers.\n"
            f"- Gateway (entry point): `{gw}`\n\n"
            "## Caller credentials\n"
            "- Actions marked ⚠ caller_credential require a key the user must supply.\n"
            "- Check if `caller_credentials` already contains the required key.\n"
            "- If missing, ask the user for it once; do not ask again in the same session.\n\n"
            "## Response format\n"
            "After completing the task, reply with a concise summary:\n"
            "- What was done and on which node(s)\n"
            "- Key outputs\n"
            "- Any warnings\n"
        )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_assistant_tool_call_msg(resp: LLMResponse) -> dict[str, Any]:
    """Build the assistant message dict that includes tool_calls."""
    return {
        "role": "assistant",
        "content": resp.content or None,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in resp.tool_calls
        ],
    }
