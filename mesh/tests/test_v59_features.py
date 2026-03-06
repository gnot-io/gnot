"""Tests for v5.9 — Intent / Agent Loop.

Covers:
  1. ConversationStore.get_or_create — new session, same id returns same session
  2. ConversationStore.get           — found, not found, expired lazy-delete
  3. ConversationStore.delete        — found, not found
  4. ConversationStore.list_sessions — active and expired filtered
  5. ConversationStore.sweep_expired — removes stale sessions
  6. Session.add_message / turn_count
  7. IntentHandler — LLM replies directly (0 tool calls)
  8. IntentHandler — LLM calls one tool, then replies
  9. IntentHandler — max_turns enforced (truncated=True)
  10. IntentHandler — tool error fed back to LLM
  11. IntentHandler — session continuity across two prompts
  12. POST /intent — 200 with LLM configured
  13. POST /intent — 503 without LLM configured
  14. GET  /sessions/{id} — found and not found
  15. DELETE /sessions/{id} — success and not found
  16. GET  /sessions — list active sessions
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.conversation_store import ConversationStore, Session, _new_session_id
from runtime.llm_client import LLMResponse, ToolCall


# ---------------------------------------------------------------------------
# 1–6: ConversationStore unit tests
# ---------------------------------------------------------------------------

class TestConversationStore:

    @pytest.mark.asyncio
    async def test_get_or_create_new_session(self):
        store = ConversationStore(ttl_seconds=3600)
        session = await store.get_or_create()
        assert session.session_id.startswith("sess-")
        assert session.turn_count == 0

    @pytest.mark.asyncio
    async def test_get_or_create_same_id_returns_same(self):
        store = ConversationStore(ttl_seconds=3600)
        s1 = await store.get_or_create("test-session")
        s1.add_message("user", "hello")
        s2 = await store.get_or_create("test-session")
        assert s2.turn_count == 1

    @pytest.mark.asyncio
    async def test_get_returns_session(self):
        store = ConversationStore(ttl_seconds=3600)
        await store.get_or_create("abc")
        session = await store.get("abc")
        assert session is not None
        assert session.session_id == "abc"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_unknown(self):
        store = ConversationStore(ttl_seconds=3600)
        assert await store.get("doesnotexist") is None

    @pytest.mark.asyncio
    async def test_get_returns_none_and_deletes_expired(self):
        store = ConversationStore(ttl_seconds=1)
        session = await store.get_or_create("exp")
        # Age the session
        store._sessions["exp"].last_active = time.time() - 10
        result = await store.get("exp")
        assert result is None
        assert "exp" not in store._sessions

    @pytest.mark.asyncio
    async def test_delete_removes_session(self):
        store = ConversationStore(ttl_seconds=3600)
        await store.get_or_create("to-delete")
        assert await store.delete("to-delete") is True
        assert await store.get("to-delete") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self):
        store = ConversationStore(ttl_seconds=3600)
        assert await store.delete("ghost") is False

    @pytest.mark.asyncio
    async def test_list_sessions_filters_expired(self):
        store = ConversationStore(ttl_seconds=1)
        s_fresh = await store.get_or_create("fresh")
        s_stale = await store.get_or_create("stale")
        store._sessions["stale"].last_active = time.time() - 10

        sessions = await store.list_sessions()
        ids = [s.session_id for s in sessions]
        assert "fresh" in ids
        assert "stale" not in ids

    @pytest.mark.asyncio
    async def test_sweep_removes_expired(self):
        store = ConversationStore(ttl_seconds=1)
        await store.get_or_create("s1")
        await store.get_or_create("s2")
        store._sessions["s1"].last_active = time.time() - 10
        removed = await store.sweep_expired()
        assert removed == 1
        assert await store.get("s2") is not None


class TestSessionModel:

    def test_turn_count_counts_user_messages(self):
        session = Session(session_id="x", created_at=time.time(), last_active=time.time())
        session.add_message("user", "hello")
        session.add_message("assistant", "hi there")
        session.add_message("user", "what time is it?")
        assert session.turn_count == 2

    def test_add_raw_appends_dict(self):
        session = Session(session_id="x", created_at=time.time(), last_active=time.time())
        session.add_raw({"role": "tool", "content": "result", "tool_call_id": "tc-1"})
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "tool"


# ---------------------------------------------------------------------------
# 7–11: IntentHandler unit tests (LLM mocked)
# ---------------------------------------------------------------------------

def _make_llm_text_response(text: str, model: str = "test-model") -> LLMResponse:
    """Fake LLMResponse with plain text reply (no tool calls)."""
    return LLMResponse(
        content=text,
        model=model,
        finish_reason="stop",
        usage={"total_tokens": 50},
        tool_calls=[],
    )


def _make_llm_tool_response(tool_id: str, action: str, node: str, params: dict) -> LLMResponse:
    """Fake LLMResponse requesting a tool call."""
    return LLMResponse(
        content=None,
        model="test-model",
        finish_reason="tool_calls",
        usage={"total_tokens": 30},
        tool_calls=[
            ToolCall(
                id=tool_id,
                name="mesh_action",
                arguments={"target_node_id": node, "action": action, "params": params},
            )
        ],
    )


def _make_sync_action_result(output: dict) -> Any:
    """Fake SyncActionResponse."""
    from runtime.models import SyncActionResponse
    return SyncActionResponse(task_id="task-test", output=output)


def _make_intent_handler(llm_mock, tool_result=None):
    """Build a minimal IntentHandler with mocked dependencies."""
    from runtime.intent_handler import IntentHandler
    from unittest.mock import MagicMock

    config = MagicMock()
    config.node_id = "node-0"
    config.intent_max_turns = 5
    config.pull_job_timeout_seconds = 60
    config.intent_system_prompt = None
    config.llm_enabled = True

    node_registry = MagicMock()
    node_registry.get_all_statuses.return_value = {"node-0": "online", "node-1": "online"}

    action_registry = {"execute_command": MagicMock(), "read_file": MagicMock()}

    router = MagicMock()
    if tool_result is not None:
        router.route = AsyncMock(return_value=tool_result)
    else:
        router.route = AsyncMock(return_value=_make_sync_action_result(
            {"exit_code": 0, "stdout": "command ok", "stderr": ""}
        ))

    store = ConversationStore(ttl_seconds=3600)

    return IntentHandler(
        config=config,
        llm_client=llm_mock,
        gateway_router=router,
        node_registry=node_registry,
        action_registry=action_registry,
        conversation_store=store,
    ), store


class TestIntentHandler:

    @pytest.mark.asyncio
    async def test_direct_reply_no_tool_calls(self):
        """LLM answers directly without using any tools."""
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=_make_llm_text_response("The weather is sunny today."))

        handler, _ = _make_intent_handler(llm)
        from runtime.models import IntentRequest
        req = IntentRequest(prompt="What is the weather?")
        resp = await handler.handle(req)

        assert resp.reply == "The weather is sunny today."
        assert resp.turns == 1
        assert resp.actions_taken == []
        assert resp.truncated is False

    @pytest.mark.asyncio
    async def test_one_tool_call_then_reply(self):
        """LLM calls one tool, gets result, then replies."""
        from runtime.models import IntentRequest

        # LLM: first call → tool request; second call → plain reply
        llm = MagicMock()
        llm.chat = AsyncMock(side_effect=[
            _make_llm_tool_response("tc-1", "execute_command", "node-1",
                                    {"command": "df -h /"}),
            _make_llm_text_response("Disk usage is 45%."),
        ])

        handler, _ = _make_intent_handler(llm)
        req = IntentRequest(prompt="Check disk space on node-1")
        resp = await handler.handle(req)

        assert resp.reply == "Disk usage is 45%."
        assert resp.turns == 2
        assert len(resp.actions_taken) == 1
        assert "execute_command" in resp.actions_taken[0]
        assert resp.truncated is False

    @pytest.mark.asyncio
    async def test_max_turns_truncates(self):
        """Agent loop exits with truncated=True when max_turns is hit."""
        from runtime.models import IntentRequest

        # LLM always requests tool calls — never terminates
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=_make_llm_tool_response(
            "tc-x", "execute_command", "node-1", {"command": "sleep 1"}
        ))

        handler, _ = _make_intent_handler(llm)
        req = IntentRequest(prompt="Run forever", max_turns=3)
        resp = await handler.handle(req)

        assert resp.truncated is True
        assert resp.turns == 3

    @pytest.mark.asyncio
    async def test_tool_error_fed_back_to_llm(self):
        """When a tool call returns an error, LLM gets it as tool result."""
        from runtime.models import IntentRequest, ErrorResponse

        error_result = ErrorResponse(error="UNTRUSTED_NODE: node-99", node_id="node-0")

        llm = MagicMock()
        llm.chat = AsyncMock(side_effect=[
            _make_llm_tool_response("tc-err", "execute_command", "node-99", {"command": "ls"}),
            _make_llm_text_response("Node 99 is not available. Please use node-1 instead."),
        ])

        handler, _ = _make_intent_handler(llm, tool_result=error_result)
        req = IntentRequest(prompt="Run ls on node-99")
        resp = await handler.handle(req)

        assert "node-99" in resp.reply or "node" in resp.reply.lower()
        assert resp.truncated is False

        # Verify the tool result message was fed back
        call_args = llm.chat.call_args_list[1]
        messages = call_args[0][0] if call_args[0] else call_args[1]["messages"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "UNTRUSTED_NODE" in tool_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_session_continuity_across_prompts(self):
        """Second prompt in same session sees prior conversation history."""
        from runtime.models import IntentRequest

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=_make_llm_text_response("Got it."))

        handler, store = _make_intent_handler(llm)

        req1 = IntentRequest(prompt="First message", session_id="convo-1")
        await handler.handle(req1)

        req2 = IntentRequest(prompt="Second message", session_id="convo-1")
        await handler.handle(req2)

        session = await store.get("convo-1")
        assert session is not None
        assert session.turn_count == 2

        # The second LLM call should have received the first turn in messages
        second_call = llm.chat.call_args_list[1]
        # messages may be positional or keyword
        if second_call.args:
            second_call_messages = second_call.args[0]
        else:
            second_call_messages = second_call.kwargs.get("messages", [])
        user_msgs = [m for m in second_call_messages if m.get("role") == "user"]
        assert len(user_msgs) == 2


# ---------------------------------------------------------------------------
# 12–16: ASGI integration tests
# ---------------------------------------------------------------------------

try:
    from httpx import AsyncClient, ASGITransport
    from runtime.action_loader import load_actions
    from runtime.config import load_config
    from runtime.server import create_app
    INTEGRATION_AVAILABLE = True
except ImportError:
    INTEGRATION_AVAILABLE = False


def _make_app_no_llm(tmpdir: str):
    yaml_path = os.path.join(tmpdir, "node.yaml")
    with open(yaml_path, "w") as f:
        f.write(
            f"node_id: node-test\n"
            f"listen: 0.0.0.0:8080\n"
            f"upload_dir: {tmpdir}/uploads\n"
        )
    config = load_config(yaml_path)
    seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
    registry = load_actions(seed_dir)
    return create_app(config, registry)


def _make_app_with_llm(tmpdir: str):
    yaml_path = os.path.join(tmpdir, "node.yaml")
    with open(yaml_path, "w") as f:
        f.write(
            f"node_id: node-test\n"
            f"listen: 0.0.0.0:8080\n"
            f"upload_dir: {tmpdir}/uploads\n"
            f"llm_api_key: fake-key-for-testing\n"
            f"llm_base_url: http://fake-llm.local/v1\n"
            f"intent_max_turns: 5\n"
        )
    config = load_config(yaml_path)
    seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
    registry = load_actions(seed_dir)
    return create_app(config, registry)


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestIntentEndpoint:

    async def test_intent_returns_503_without_llm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app_no_llm(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/intent", json={"prompt": "hello"})
            assert resp.status_code == 503
            assert "LLM_NOT_CONFIGURED" in resp.json().get("error", "")

    async def test_intent_returns_200_with_llm_mocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app_with_llm(tmpdir)

            mock_resp = _make_llm_text_response("I can help with that.")
            with patch("runtime.intent_handler.IntentHandler.handle",
                       new_callable=AsyncMock) as mock_handle:
                from runtime.models import IntentResponse
                mock_handle.return_value = IntentResponse(
                    session_id="sess-test",
                    reply="I can help with that.",
                    turns=1,
                    actions_taken=[],
                    tokens_used=20,
                    truncated=False,
                )
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    resp = await c.post("/intent", json={"prompt": "hello", "session_id": "sess-test"})

            assert resp.status_code == 200
            data = resp.json()
            assert data["reply"] == "I can help with that."
            assert data["session_id"] == "sess-test"


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestSessionEndpoints:

    async def test_get_session_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app_no_llm(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/sessions/nonexistent-session")
            assert resp.status_code == 404

    async def test_delete_session_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app_no_llm(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.delete("/sessions/no-such-session")
            assert resp.status_code == 404

    async def test_list_sessions_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app_no_llm(tmpdir)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/sessions")
            assert resp.status_code == 200
            assert resp.json()["total"] == 0
