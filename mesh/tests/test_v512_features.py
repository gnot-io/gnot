"""Tests for v5.12 — Route Withdrawal, Result Forwarding Chain,
Session Credential Storage, Credential Encryption.

Test classes:
  TestCredentialEncryption        — AES-GCM encrypt/decrypt, key derivation, fallback
  TestCredentialStore             — merge, get, TTL expiry, isolation, purge
  TestCredentialStoreIntegration  — IntentHandler stores & retrieves creds across turns
  TestRouteWithdrawal             — withdraw_routes(), cascade via periodic re-registration
  TestStatusInCapabilityTree      — UNREACHABLE shown in GET /capabilities + system prompt
  TestResultForwardingChain       — _handle_mesh_forward polls downstream, relays real result
  TestEndToEndV512                — HTTP-level tests via ASGI
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.credential_store import CredentialStore, _decrypt, _derive_key, _encrypt
from runtime.models import CapabilityNode, NodeStatus


# ===========================================================================
# 1. Credential Encryption — crypto layer unit tests
# ===========================================================================

class TestCredentialEncryption:

    def test_derive_key_produces_32_bytes(self):
        key = _derive_key("sk-secret", "sess-1")
        assert len(key) == 32

    def test_derive_key_deterministic(self):
        k1 = _derive_key("sk-secret", "sess-1")
        k2 = _derive_key("sk-secret", "sess-1")
        assert k1 == k2

    def test_derive_key_different_sessions_differ(self):
        k1 = _derive_key("sk-secret", "sess-1")
        k2 = _derive_key("sk-secret", "sess-2")
        assert k1 != k2

    def test_derive_key_different_tokens_differ(self):
        k1 = _derive_key("sk-a", "sess-1")
        k2 = _derive_key("sk-b", "sess-1")
        assert k1 != k2

    def test_derive_key_none_token_ok(self):
        # Should not raise; uses "no-auth" placeholder
        key = _derive_key(None, "sess-1")
        assert len(key) == 32

    def test_roundtrip_encryption(self):
        key = _derive_key("sk-secret", "sess-1")
        ct = _encrypt("my-api-key-abc123", key)
        pt = _decrypt(ct, key)
        assert pt == "my-api-key-abc123"

    def test_encrypt_produces_different_ciphertext_each_call(self):
        """Random nonce ensures same plaintext → different ciphertext."""
        key = _derive_key("sk-secret", "sess-1")
        ct1 = _encrypt("same-value", key)
        ct2 = _encrypt("same-value", key)
        assert ct1 != ct2

    def test_decrypt_with_wrong_key_raises(self):
        key1 = _derive_key("sk-a", "sess-1")
        key2 = _derive_key("sk-b", "sess-1")
        ct = _encrypt("secret", key1)
        with pytest.raises(ValueError, match="decryption failed"):
            _decrypt(ct, key2)

    def test_ciphertext_not_contain_plaintext(self):
        key = _derive_key("sk-secret", "sess-1")
        plaintext = "sk-super-secret-api-key"
        ct = _encrypt(plaintext, key)
        # Raw base64 decode shouldn't contain the plaintext either
        raw = base64.b64decode(ct)
        assert plaintext.encode() not in raw


# ===========================================================================
# 2. CredentialStore — store-level behaviour
# ===========================================================================

class TestCredentialStore:

    def _store(self, ttl: int = 60) -> CredentialStore:
        return CredentialStore(encryption_key="sk-node", ttl_seconds=ttl)

    @pytest.mark.asyncio
    async def test_merge_and_get(self):
        s = self._store()
        await s.merge("s1", {"crm_user_token": "sk-abc", "erp_key": "ek-xyz"})
        got = await s.get("s1")
        assert got["crm_user_token"] == "sk-abc"
        assert got["erp_key"] == "ek-xyz"

    @pytest.mark.asyncio
    async def test_merge_adds_without_removing_existing(self):
        s = self._store()
        await s.merge("s1", {"key_a": "val_a"})
        await s.merge("s1", {"key_b": "val_b"})
        got = await s.get("s1")
        assert got["key_a"] == "val_a"
        assert got["key_b"] == "val_b"

    @pytest.mark.asyncio
    async def test_merge_overwrites_existing_key(self):
        s = self._store()
        await s.merge("s1", {"crm_user_token": "old"})
        await s.merge("s1", {"crm_user_token": "new"})
        assert (await s.get("s1"))["crm_user_token"] == "new"

    @pytest.mark.asyncio
    async def test_get_unknown_session_returns_empty(self):
        s = self._store()
        assert await s.get("nonexistent") == {}

    @pytest.mark.asyncio
    async def test_sessions_are_isolated(self):
        s = self._store()
        await s.merge("s1", {"token": "val-for-s1"})
        await s.merge("s2", {"token": "val-for-s2"})
        assert (await s.get("s1"))["token"] == "val-for-s1"
        assert (await s.get("s2"))["token"] == "val-for-s2"

    @pytest.mark.asyncio
    async def test_clear_removes_credentials(self):
        s = self._store()
        await s.merge("s1", {"k": "v"})
        await s.clear("s1")
        assert await s.get("s1") == {}

    @pytest.mark.asyncio
    async def test_clear_nonexistent_does_not_raise(self):
        s = self._store()
        await s.clear("nonexistent")  # should not raise

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        s = CredentialStore(encryption_key="sk-x", ttl_seconds=0)
        await s.merge("s1", {"k": "v"})
        await asyncio.sleep(0.01)
        assert await s.get("s1") == {}

    @pytest.mark.asyncio
    async def test_touch_extends_ttl(self):
        s = CredentialStore(encryption_key="sk-x", ttl_seconds=1)
        await s.merge("s1", {"k": "v"})
        # touch resets expiry
        await s.touch("s1")
        assert (await s.get("s1"))["k"] == "v"

    @pytest.mark.asyncio
    async def test_purge_expired(self):
        s = CredentialStore(encryption_key="sk-x", ttl_seconds=0)
        await s.merge("s1", {"k": "v"})
        await s.merge("s2", {"k": "v"})
        await asyncio.sleep(0.01)
        count = await s.purge_expired()
        assert count == 2
        assert s.session_count == 0

    @pytest.mark.asyncio
    async def test_merge_empty_dict_is_noop(self):
        s = self._store()
        await s.merge("s1", {})
        assert await s.get("s1") == {}

    @pytest.mark.asyncio
    async def test_credentials_not_equal_to_stored_ciphertext(self):
        """Verify the stored values are ciphertext, not plaintext."""
        s = self._store()
        plaintext_key = "sk-super-secret"
        await s.merge("s1", {"crm_token": plaintext_key})
        # Internal store should not contain the plaintext
        entry = s._store["s1"]
        stored_encrypted = entry["creds"]["crm_token"]
        assert plaintext_key not in stored_encrypted
        # But get() returns the original plaintext
        got = await s.get("s1")
        assert got["crm_token"] == plaintext_key


# ===========================================================================
# 3. CredentialStore × IntentHandler integration
# ===========================================================================

class TestCredentialStoreIntegration:
    """Test that IntentHandler persists credentials across turns."""

    def _make_handler(self):
        from runtime.intent_handler import IntentHandler
        from runtime.conversation_store import ConversationStore
        from runtime.schema_validator import ActionSchemaValidator

        config = MagicMock()
        config.node_id = "node-0"
        config.intent_max_turns = 3
        config.pull_job_timeout_seconds = 30
        config.intent_system_prompt = None
        config.llm_enabled = True

        node_registry = MagicMock()
        node_registry.build_capability_tree.return_value = {}

        store = ConversationStore()
        cred_store = CredentialStore(encryption_key="sk-node", ttl_seconds=3600)
        sv = ActionSchemaValidator({})

        return IntentHandler(
            config=config,
            llm_client=MagicMock(),
            gateway_router=MagicMock(),
            node_registry=node_registry,
            action_registry={},
            conversation_store=store,
            schema_validator=sv,
            credential_store=cred_store,
        ), cred_store

    @pytest.mark.asyncio
    async def test_credentials_stored_on_first_turn(self):
        from runtime.models import IntentRequest
        from runtime.llm_client import LLMResponse

        handler, cred_store = self._make_handler()

        async def fake_llm(**kwargs):
            return LLMResponse(content="Done", tool_calls=[], usage={}, model="gpt-4o")
        handler._llm.chat = fake_llm

        req = IntentRequest(
            prompt="hello",
            session_id="sess-123",
            caller_credentials={"crm_user_token": "sk-abc"},
        )
        await handler.handle(req)

        stored = await cred_store.get("sess-123")
        assert stored.get("crm_user_token") == "sk-abc"

    @pytest.mark.asyncio
    async def test_stored_credentials_used_on_subsequent_turn(self):
        """Second request without caller_credentials still has access to stored creds."""
        from runtime.models import IntentRequest, SyncActionResponse, ActionRequest
        from runtime.llm_client import LLMResponse, ToolCall

        handler, cred_store = self._make_handler()

        # Pre-populate credential store
        await cred_store.merge("sess-456", {"crm_user_token": "sk-stored"})

        captured: list[dict] = []
        call_count = 0

        async def fake_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                tc = ToolCall(
                    id="tc1", name="mesh_action",
                    arguments={"target_node_id": "n", "action": "get_order_info",
                               "params": {"order_id": "1"}},
                )
                return LLMResponse(content=None, tool_calls=[tc], usage={}, model="gpt-4o")
            return LLMResponse(content="Done", tool_calls=[], usage={}, model="gpt-4o")

        handler._llm.chat = fake_llm

        async def fake_route(req: ActionRequest):
            captured.append(dict(req.caller_credentials))
            return SyncActionResponse(task_id=req.task_id, output={"ok": True})
        handler._router.route = fake_route

        # Second turn — no caller_credentials in request
        req = IntentRequest(prompt="get order 1", session_id="sess-456")
        await handler.handle(req)

        assert len(captured) == 1
        assert captured[0].get("crm_user_token") == "sk-stored"

    @pytest.mark.asyncio
    async def test_request_credentials_override_stored(self):
        """New credentials in request take precedence over stored."""
        from runtime.models import IntentRequest, SyncActionResponse, ActionRequest
        from runtime.llm_client import LLMResponse, ToolCall

        handler, cred_store = self._make_handler()
        await cred_store.merge("sess-789", {"crm_user_token": "sk-old"})

        captured: list[dict] = []
        call_count = 0

        async def fake_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                tc = ToolCall(id="tc1", name="mesh_action",
                              arguments={"target_node_id": "n", "action": "a", "params": {}})
                return LLMResponse(content=None, tool_calls=[tc], usage={}, model="gpt-4o")
            return LLMResponse(content="Done", tool_calls=[], usage={}, model="gpt-4o")

        handler._llm.chat = fake_llm

        async def fake_route(req: ActionRequest):
            captured.append(dict(req.caller_credentials))
            return SyncActionResponse(task_id=req.task_id, output={})
        handler._router.route = fake_route

        req = IntentRequest(
            prompt="do it",
            session_id="sess-789",
            caller_credentials={"crm_user_token": "sk-new"},
        )
        await handler.handle(req)

        assert captured[0]["crm_user_token"] == "sk-new"
        # Also updated in store
        stored = await cred_store.get("sess-789")
        assert stored["crm_user_token"] == "sk-new"


# ===========================================================================
# 4. Route Withdrawal
# ===========================================================================

class TestRouteWithdrawal:

    @pytest.mark.asyncio
    async def test_withdraw_removes_stale_sub_route(self):
        from runtime.node_registry import NodeRegistry
        reg = NodeRegistry()
        await reg.register(
            "node-1",
            advertise_routes=["node-1a", "node-1b"],
            capabilities={"node-1a": ["get_order_info"], "node-1b": ["read_file"]},
        )
        assert "node-1a" in reg._entries
        assert "node-1b" in reg._entries

        # node-1 re-registers with only node-1b (node-1a went down)
        withdrawn = await reg.withdraw_routes("node-1", keep_routes=["node-1b"])
        assert "node-1a" in withdrawn
        assert "node-1a" not in reg._entries
        assert "node-1b" in reg._entries

    @pytest.mark.asyncio
    async def test_withdraw_empty_list_removes_all_via_node(self):
        from runtime.node_registry import NodeRegistry
        reg = NodeRegistry()
        await reg.register(
            "node-1",
            advertise_routes=["node-1a"],
            capabilities={"node-1a": []},
        )
        withdrawn = await reg.withdraw_routes("node-1", keep_routes=[])
        assert "node-1a" in withdrawn
        assert "node-1a" not in reg._entries

    @pytest.mark.asyncio
    async def test_withdraw_does_not_touch_other_node_routes(self):
        from runtime.node_registry import NodeRegistry
        reg = NodeRegistry()
        await reg.register(
            "node-1",
            advertise_routes=["node-1a"],
            capabilities={"node-1a": []},
        )
        await reg.register(
            "node-2",
            advertise_routes=["node-2a"],
            capabilities={"node-2a": []},
        )
        await reg.withdraw_routes("node-1", keep_routes=[])
        assert "node-1a" not in reg._entries
        assert "node-2a" in reg._entries  # unaffected

    @pytest.mark.asyncio
    async def test_withdraw_returns_empty_when_nothing_to_remove(self):
        from runtime.node_registry import NodeRegistry
        reg = NodeRegistry()
        await reg.register("node-1", advertise_routes=["node-1a"],
                           capabilities={"node-1a": []})
        # Keep all routes — nothing withdrawn
        withdrawn = await reg.withdraw_routes("node-1", keep_routes=["node-1a"])
        assert withdrawn == []

    @pytest.mark.asyncio
    async def test_withdraw_does_not_remove_direct_nodes(self):
        """Direct nodes (next_hop=None) must NOT be withdrawn by a different node."""
        from runtime.node_registry import NodeRegistry
        reg = NodeRegistry()
        await reg.register("direct-node")          # next_hop=None
        withdrawn = await reg.withdraw_routes("some-other-node", keep_routes=[])
        assert "direct-node" not in withdrawn
        assert "direct-node" in reg._entries


# ===========================================================================
# 5. Status in Capability Tree
# ===========================================================================

class TestStatusInCapabilityTree:

    @pytest.mark.asyncio
    async def test_online_node_has_online_status(self):
        from runtime.node_registry import NodeRegistry
        reg = NodeRegistry()
        await reg.register("node-1")
        await reg.heartbeat("node-1")
        tree = reg.build_capability_tree([])
        assert tree["node-1"].status == "online"

    @pytest.mark.asyncio
    async def test_never_heartbeat_node_has_unknown_status(self):
        from runtime.node_registry import NodeRegistry
        reg = NodeRegistry()
        await reg.register("node-1")
        tree = reg.build_capability_tree([])
        assert tree["node-1"].status == "unknown"

    @pytest.mark.asyncio
    async def test_stale_node_marked_unreachable_in_tree(self):
        from runtime.node_registry import NodeRegistry
        reg = NodeRegistry(heartbeat_timeout_seconds=0)
        await reg.register("node-1")
        await reg.heartbeat("node-1")
        await asyncio.sleep(0.01)  # heartbeat now stale
        tree = reg.build_capability_tree([])
        assert tree["node-1"].status == "unreachable"

    @pytest.mark.asyncio
    async def test_unreachable_shown_in_system_prompt(self):
        from runtime.intent_handler import IntentHandler
        from runtime.conversation_store import ConversationStore
        from runtime.node_registry import NodeRegistry
        from runtime.schema_validator import ActionSchemaValidator

        config = MagicMock()
        config.node_id = "node-0"
        config.intent_system_prompt = None

        # Create node-1 with stale heartbeat
        reg = NodeRegistry(heartbeat_timeout_seconds=0)
        await reg.register("node-1", actions=["read_file"])
        await reg.heartbeat("node-1")
        await asyncio.sleep(0.01)

        handler = IntentHandler(
            config=config,
            llm_client=MagicMock(),
            gateway_router=MagicMock(),
            node_registry=reg,
            action_registry={},
            conversation_store=ConversationStore(),
            schema_validator=ActionSchemaValidator({}),
        )
        prompt = handler._build_system_prompt()
        assert "UNREACHABLE" in prompt
        assert "node-1" in prompt


# ===========================================================================
# 6. Result Forwarding Chain (poll-and-relay)
# ===========================================================================

class TestResultForwardingChain:

    @pytest.mark.asyncio
    async def test_handle_mesh_forward_sync_passthrough(self):
        """When downstream returns sync result, it's passed through directly."""
        from runtime.worker_agent import WorkerAgent
        from runtime.action_executor import ActionExecutor
        from runtime.job_manager import JobManager
        from runtime.models import QueuedJob, SyncActionResponse

        config = MagicMock()
        config.node_id = "node-1"
        config.heartbeat_interval_seconds = 10
        config.poll_interval_seconds = 5
        config.pull_job_timeout_seconds = 60

        executor = MagicMock()
        agent = WorkerAgent(config=config, executor=executor)
        agent._self_url = "http://node-1:8080"
        agent._auth_headers = {}

        job = QueuedJob(
            job_id="j1", task_id="t1", target_node_id="node-1a",
            action="_mesh_forward",
            params={"_mesh_forward_target": "node-1a", "action": "read_file",
                    "path": "/tmp/x"},
            created_at=time.time(),
        )

        # Local /action returns sync result (no job_id)
        import httpx
        with patch("httpx.AsyncClient") as mock_client:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"output": {"content": "hello"}, "status": "completed"}
            mock_resp.raise_for_status = MagicMock()
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.post = AsyncMock(return_value=mock_resp)

            result = await agent._handle_mesh_forward(job)

        assert isinstance(result, SyncActionResponse)
        assert result.output.get("content") == "hello"

    @pytest.mark.asyncio
    async def test_handle_mesh_forward_async_polls_downstream(self):
        """When downstream returns job_id (async), agent polls and returns real result."""
        from runtime.worker_agent import WorkerAgent
        from runtime.models import QueuedJob, SyncActionResponse
        import httpx

        config = MagicMock()
        config.node_id = "node-1"
        config.heartbeat_interval_seconds = 10
        config.poll_interval_seconds = 5
        config.pull_job_timeout_seconds = 60

        agent = WorkerAgent(config=config, executor=MagicMock())
        agent._self_url = "http://node-1:8080"
        agent._auth_headers = {}

        job = QueuedJob(
            job_id="j1", task_id="t1", target_node_id="node-1a",
            action="_mesh_forward",
            params={"_mesh_forward_target": "node-1a", "action": "slow_action",
                    "param1": "v"},
            created_at=time.time(),
        )

        # POST /action returns async response with job_id
        post_resp = MagicMock()
        post_resp.status_code = 202
        post_resp.json.return_value = {"job_id": "j2", "status": "accepted"}
        post_resp.raise_for_status = MagicMock()

        # GET /result/j2 returns running then completed
        get_calls = [0]
        get_resp_running = MagicMock()
        get_resp_running.status_code = 200
        get_resp_running.json.return_value = {"status": "running"}
        get_resp_running.raise_for_status = MagicMock()

        get_resp_done = MagicMock()
        get_resp_done.status_code = 200
        get_resp_done.json.return_value = {"status": "completed", "output": {"result": "real_data"}}
        get_resp_done.raise_for_status = MagicMock()

        async def mock_get(url, **kwargs):
            get_calls[0] += 1
            if get_calls[0] == 1:
                return get_resp_running
            return get_resp_done

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.post = AsyncMock(return_value=post_resp)
            mock_client.return_value.get = mock_get

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await agent._handle_mesh_forward(job)

        assert isinstance(result, SyncActionResponse)
        assert result.output.get("result") == "real_data"
        assert get_calls[0] == 2  # one running + one completed poll

    @pytest.mark.asyncio
    async def test_handle_mesh_forward_downstream_failure_raises(self):
        """If downstream job fails, RuntimeError is raised to mark forwarding job as failed."""
        from runtime.worker_agent import WorkerAgent
        from runtime.models import QueuedJob
        import httpx

        config = MagicMock()
        config.node_id = "node-1"
        config.heartbeat_interval_seconds = 10
        config.poll_interval_seconds = 5
        config.pull_job_timeout_seconds = 60

        agent = WorkerAgent(config=config, executor=MagicMock())
        agent._self_url = "http://node-1:8080"
        agent._auth_headers = {}

        job = QueuedJob(
            job_id="j1", task_id="t1", target_node_id="node-1a",
            action="_mesh_forward",
            params={"_mesh_forward_target": "node-1a", "action": "bad_action"},
            created_at=time.time(),
        )

        post_resp = MagicMock()
        post_resp.status_code = 202
        post_resp.json.return_value = {"job_id": "j2"}
        post_resp.raise_for_status = MagicMock()

        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {"status": "failed", "error": "command not found"}
        get_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.post = AsyncMock(return_value=post_resp)
            mock_client.return_value.get = AsyncMock(return_value=get_resp)

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(RuntimeError, match="command not found"):
                    await agent._handle_mesh_forward(job)


# ===========================================================================
# 7. End-to-End HTTP tests
# ===========================================================================

try:
    from httpx import AsyncClient, ASGITransport
    from runtime.action_loader import load_actions
    from runtime.config import load_config
    from runtime.server import create_app
    from runtime.schema_validator import load_schemas
    INTEGRATION_AVAILABLE = True
except ImportError:
    INTEGRATION_AVAILABLE = False


def _make_app(tmpdir: str, extra_yaml: str = "") -> Any:
    p = os.path.join(tmpdir, "node.yaml")
    with open(p, "w") as f:
        f.write("node_id: node-0\nlisten: 0.0.0.0:8080\nauth_token: sk-test\n")
        f.write(extra_yaml)
    cfg = load_config(p)
    seed = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
    return create_app(cfg, load_actions(seed), schema_registry=load_schemas(seed))


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not available")
@pytest.mark.asyncio
class TestEndToEndV512:

    async def test_capabilities_include_node_status(self):
        with tempfile.TemporaryDirectory() as d:
            app = _make_app(d)
            AUTH = {"Authorization": "Bearer sk-test"}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/nodes/register", json={
                    "node_id": "node-1",
                    "actions": ["read_file"],
                }, headers=AUTH)
                resp = await c.get("/capabilities", headers=AUTH)
            data = resp.json()
            assert "node-1" in data["reachable"]
            assert "status" in data["reachable"]["node-1"]

    async def test_route_withdrawal_via_register(self):
        """Re-registering with smaller advertise_routes withdraws stale entries."""
        with tempfile.TemporaryDirectory() as d:
            app = _make_app(d)
            AUTH = {"Authorization": "Bearer sk-test"}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                # First: node-1 advertises node-1a and node-1b
                await c.post("/nodes/register", json={
                    "node_id": "node-1",
                    "actions": ["read_file"],
                    "advertise_routes": ["node-1a", "node-1b"],
                    "capabilities": {"node-1a": ["get_order_info"], "node-1b": ["read_file"]},
                }, headers=AUTH)
                caps_before = (await c.get("/capabilities", headers=AUTH)).json()
                assert "node-1a" in caps_before["reachable"]
                assert "node-1b" in caps_before["reachable"]

                # node-1a goes down; node-1 re-registers with only node-1b
                await c.post("/nodes/register", json={
                    "node_id": "node-1",
                    "actions": ["read_file"],
                    "advertise_routes": ["node-1b"],
                    "capabilities": {"node-1b": ["read_file"]},
                }, headers=AUTH)
                caps_after = (await c.get("/capabilities", headers=AUTH)).json()
                assert "node-1a" not in caps_after["reachable"]
                assert "node-1b" in caps_after["reachable"]

    async def test_intent_stores_credentials_in_session(self):
        """POST /intent with caller_credentials persists them for the session."""
        # This test checks the HTTP path; actual persistence is unit-tested above
        with tempfile.TemporaryDirectory() as d:
            import yaml
            p = os.path.join(d, "node.yaml")
            with open(p, "w") as f:
                f.write("node_id: node-0\nlisten: 0.0.0.0:8080\n")
            cfg = load_config(p)
            seed = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
            app = create_app(cfg, load_actions(seed), schema_registry=load_schemas(seed))

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post("/intent", json={
                    "prompt": "test",
                    "session_id": "s-abc",
                    "caller_credentials": {"crm_user_token": "sk-123"},
                })
            # LLM not configured so 503, but credentials should have been attempted
            # Just verify the endpoint accepted the new field
            assert resp.status_code in (200, 503)
            # No 422 validation error
            assert "validation" not in resp.text.lower() or resp.status_code != 422
