"""Tests for v5.11 — Caller Policies, Action Schema Exposure, Credential Delivery.

Test classes:
  TestCallerPolicy            — CallerPolicy logic, check_caller_policy()
  TestSchemaValidatorV511     — x-caller-credentials parsing, check/build methods
  TestActionExecutorV511      — Policy enforcement, credential validation, context injection
  TestNodeRegistryV511        — action_specs stored + exposed in capability tree
  TestCapabilityEndpointV511  — GET /capabilities includes action specs + credentials
  TestIntentHandlerV511       — caller_credentials propagated through agent loop
  TestConfigV511              — caller_policies loaded from node.yaml
  TestEndToEndV511            — POST /action rejected/allowed based on policy + credentials
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.config import CallerPolicy, check_caller_policy
from runtime.models import (
    ActionRequest,
    ActionPayload,
    ActionSpec,
    CallerCredentialSpec,
    TraceInfo,
    QueuedJob,
)
from runtime.schema_validator import ActionSchemaValidator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SCHEMA_WITH_CREDS = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "get_order_info",
    "description": "Retrieve order details",
    "x-caller-credentials": {
        "crm_user_token": {
            "description": "CRM API key",
            "required": True,
            "hint": "From CRM admin panel",
        }
    },
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "Order ID"},
    },
    "required": ["order_id"],
    "additionalProperties": False,
}

SCHEMA_NO_CREDS = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "execute_command",
    "description": "Run a shell command",
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Shell command"},
    },
    "required": ["command"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# 1. CallerPolicy
# ---------------------------------------------------------------------------

class TestCallerPolicy:

    def test_allows_specific_action(self):
        p = CallerPolicy(token="tk-sales", allowed_actions=["get_order_info", "list_orders"])
        assert p.allows("get_order_info")
        assert p.allows("list_orders")
        assert not p.allows("execute_command")

    def test_wildcard_allows_all(self):
        p = CallerPolicy(token="tk-admin", allowed_actions="*")
        assert p.allows("execute_command")
        assert p.allows("get_order_info")
        assert p.allows("anything_at_all")

    def test_check_no_policies_is_open(self):
        """No policies → level 1, all callers allowed."""
        assert check_caller_policy([], None, "any_action") is True
        assert check_caller_policy([], "some-token", "any_action") is True

    def test_check_token_in_policy_allowed(self):
        p = CallerPolicy(token="tk-sales", allowed_actions=["get_order_info"])
        assert check_caller_policy([p], "tk-sales", "get_order_info") is True

    def test_check_token_in_policy_denied(self):
        p = CallerPolicy(token="tk-sales", allowed_actions=["get_order_info"])
        assert check_caller_policy([p], "tk-sales", "execute_command") is False

    def test_check_unknown_token_denied_when_policies_exist(self):
        p = CallerPolicy(token="tk-sales", allowed_actions=["get_order_info"])
        assert check_caller_policy([p], "unknown-token", "get_order_info") is False

    def test_check_no_token_denied_when_policies_exist(self):
        p = CallerPolicy(token="tk-sales", allowed_actions=["get_order_info"])
        assert check_caller_policy([p], None, "get_order_info") is False

    def test_multiple_policies_first_match_wins(self):
        policies = [
            CallerPolicy(token="tk-sales", allowed_actions=["get_order_info"]),
            CallerPolicy(token="tk-devops", allowed_actions=["execute_command"]),
        ]
        assert check_caller_policy(policies, "tk-sales", "get_order_info") is True
        assert check_caller_policy(policies, "tk-sales", "execute_command") is False
        assert check_caller_policy(policies, "tk-devops", "execute_command") is True
        assert check_caller_policy(policies, "tk-devops", "get_order_info") is False


# ---------------------------------------------------------------------------
# 2. SchemaValidator v5.11
# ---------------------------------------------------------------------------

class TestSchemaValidatorV511:

    def _sv(self):
        return ActionSchemaValidator({
            "get_order_info": SCHEMA_WITH_CREDS,
            "execute_command": SCHEMA_NO_CREDS,
        })

    def test_get_caller_credential_requirements(self):
        sv = self._sv()
        reqs = sv.get_caller_credential_requirements("get_order_info")
        assert "crm_user_token" in reqs
        spec = reqs["crm_user_token"]
        assert spec.required is True
        assert "CRM API key" in spec.description
        assert spec.hint != ""

    def test_no_credentials_for_action_without_spec(self):
        sv = self._sv()
        reqs = sv.get_caller_credential_requirements("execute_command")
        assert reqs == {}

    def test_no_credentials_for_unknown_action(self):
        sv = self._sv()
        reqs = sv.get_caller_credential_requirements("nonexistent")
        assert reqs == {}

    def test_check_caller_credentials_missing(self):
        sv = self._sv()
        missing = sv.check_caller_credentials("get_order_info", {})
        assert "crm_user_token" in missing

    def test_check_caller_credentials_satisfied(self):
        sv = self._sv()
        missing = sv.check_caller_credentials("get_order_info", {"crm_user_token": "sk-abc"})
        assert missing == []

    def test_check_caller_credentials_no_requirement(self):
        sv = self._sv()
        missing = sv.check_caller_credentials("execute_command", {})
        assert missing == []

    def test_build_action_spec(self):
        sv = self._sv()
        spec = sv.build_action_spec("get_order_info")
        assert spec.description == "Retrieve order details"
        assert "crm_user_token" in spec.caller_credentials
        assert "order_id" in spec.params_schema

    def test_build_all_action_specs(self):
        sv = self._sv()
        all_specs = sv.build_all_action_specs()
        assert "get_order_info" in all_specs
        assert "execute_command" in all_specs
        assert all_specs["execute_command"].caller_credentials == {}


# ---------------------------------------------------------------------------
# 3. ActionExecutor v5.11
# ---------------------------------------------------------------------------

class TestActionExecutorV511:

    def _make_executor(
        self,
        policies: list[CallerPolicy] | None = None,
        schema_registry: dict | None = None,
    ):
        from runtime.action_executor import ActionExecutor
        from runtime.job_manager import JobManager

        registry = {
            "get_order_info": MagicMock(ASYNC=False, run=lambda p, ctx: {"order_id": p["order_id"]}),
            "execute_command": MagicMock(ASYNC=False, run=lambda p, ctx: {"exit_code": 0}),
        }

        sv = ActionSchemaValidator(schema_registry or {})
        return ActionExecutor(
            registry=registry,
            job_manager=JobManager(),
            node_id="node-test",
            schema_validator=sv,
            caller_policies=policies or [],
        )

    @pytest.mark.asyncio
    async def test_no_policies_allows_all_callers(self):
        ex = self._make_executor(policies=[])
        result = await ex.execute("execute_command", {"command": "ls"}, "t1",
                                  caller_token=None, caller_credentials={})
        assert result.output.get("exit_code") == 0

    @pytest.mark.asyncio
    async def test_policy_allows_correct_action(self):
        from runtime.action_executor import CallerNotAllowedError
        ex = self._make_executor(policies=[
            CallerPolicy(token="tk-sales", allowed_actions=["get_order_info"]),
        ])
        result = await ex.execute(
            "get_order_info", {"order_id": "123"}, "t1",
            caller_token="tk-sales", caller_credentials={"crm_user_token": "sk-x"},
        )
        assert result.output.get("order_id") == "123"

    @pytest.mark.asyncio
    async def test_policy_blocks_disallowed_action(self):
        from runtime.action_executor import CallerNotAllowedError
        ex = self._make_executor(policies=[
            CallerPolicy(token="tk-sales", allowed_actions=["get_order_info"]),
        ])
        with pytest.raises(CallerNotAllowedError) as exc_info:
            await ex.execute("execute_command", {"command": "ls"}, "t1",
                             caller_token="tk-sales", caller_credentials={})
        assert "execute_command" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_missing_caller_credential_raises(self):
        from runtime.action_executor import MissingCallerCredentialError
        ex = self._make_executor(
            policies=[],
            schema_registry={"get_order_info": SCHEMA_WITH_CREDS},
        )
        with pytest.raises(MissingCallerCredentialError) as exc_info:
            await ex.execute("get_order_info", {"order_id": "123"}, "t1",
                             caller_token=None, caller_credentials={})
        assert "crm_user_token" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_caller_credentials_injected_into_context(self):
        from runtime.action_executor import ActionExecutor
        from runtime.job_manager import JobManager

        received_ctx: dict = {}

        def capture_run(params, ctx):
            received_ctx.update(ctx)
            return {"ok": True}

        registry = {"get_order_info": MagicMock(ASYNC=False, run=capture_run)}
        sv = ActionSchemaValidator({"get_order_info": SCHEMA_WITH_CREDS})
        ex = ActionExecutor(registry=registry, job_manager=JobManager(),
                            node_id="n", schema_validator=sv)
        await ex.execute("get_order_info", {"order_id": "1"}, "t",
                         caller_credentials={"crm_user_token": "sk-secret"})

        assert received_ctx.get("caller_credentials", {}).get("crm_user_token") == "sk-secret"

    @pytest.mark.asyncio
    async def test_unknown_token_with_policies_raises(self):
        from runtime.action_executor import CallerNotAllowedError
        ex = self._make_executor(policies=[
            CallerPolicy(token="tk-known", allowed_actions=["get_order_info"]),
        ])
        with pytest.raises(CallerNotAllowedError):
            await ex.execute("get_order_info", {"order_id": "1"}, "t",
                             caller_token="tk-unknown", caller_credentials={})


# ---------------------------------------------------------------------------
# 4. NodeRegistry v5.11
# ---------------------------------------------------------------------------

class TestNodeRegistryV511:

    @pytest.mark.asyncio
    async def test_register_stores_action_specs(self):
        from runtime.node_registry import NodeRegistry
        reg = NodeRegistry()
        spec_dict = {
            "get_order_info": {
                "description": "Retrieve order details",
                "params_schema": {"order_id": {"type": "string"}},
                "caller_credentials": {
                    "crm_user_token": {
                        "description": "CRM API key",
                        "required": True,
                        "hint": "",
                    }
                },
                "async_action": False,
            }
        }
        await reg.register(
            "node-1a",
            actions=["get_order_info"],
            action_specs=spec_dict,
        )
        tree = reg.build_capability_tree(own_actions=[])
        assert "node-1a" in tree
        cap = tree["node-1a"]
        assert "get_order_info" in cap.action_specs
        assert cap.action_specs["get_order_info"].description == "Retrieve order details"
        assert "crm_user_token" in cap.action_specs["get_order_info"].caller_credentials

    @pytest.mark.asyncio
    async def test_capability_tree_empty_specs_when_not_provided(self):
        from runtime.node_registry import NodeRegistry
        reg = NodeRegistry()
        await reg.register("node-1", actions=["execute_command"])
        tree = reg.build_capability_tree([])
        assert tree["node-1"].action_specs == {}


# ---------------------------------------------------------------------------
# 5. GET /capabilities includes action_specs
# ---------------------------------------------------------------------------

try:
    from httpx import AsyncClient, ASGITransport
    from runtime.action_loader import load_actions
    from runtime.config import load_config
    from runtime.server import create_app
    INTEGRATION_AVAILABLE = True
except ImportError:
    INTEGRATION_AVAILABLE = False


def _make_app(tmpdir: str):
    yaml_path = os.path.join(tmpdir, "node.yaml")
    with open(yaml_path, "w") as f:
        f.write("node_id: node-0\nlisten: 0.0.0.0:8080\n")
    config = load_config(yaml_path)
    seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
    from runtime.schema_validator import load_schemas
    registry = load_actions(seed_dir)
    schema_reg = load_schemas(seed_dir)
    return create_app(config, registry, schema_registry=schema_reg)


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestCapabilityEndpointV511:

    async def test_capabilities_includes_action_spec_description(self):
        with tempfile.TemporaryDirectory() as d:
            app = _make_app(d)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                # Register node with action_specs
                spec_payload = {
                    "node_id": "node-1a",
                    "actions": ["get_order_info"],
                    "action_specs": {
                        "get_order_info": {
                            "description": "Retrieve order details",
                            "params_schema": {"order_id": {"type": "string"}},
                            "caller_credentials": {
                                "crm_user_token": {
                                    "description": "CRM API key",
                                    "required": True,
                                    "hint": "From admin panel",
                                }
                            },
                            "async_action": False,
                        }
                    },
                }
                resp = await c.post("/nodes/register", json=spec_payload)
                assert resp.status_code == 200

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                caps = await c.get("/capabilities")
            data = caps.json()
            assert "node-1a" in data["reachable"]
            node_cap = data["reachable"]["node-1a"]
            assert "action_specs" in node_cap
            assert "get_order_info" in node_cap["action_specs"]
            spec = node_cap["action_specs"]["get_order_info"]
            assert spec["description"] == "Retrieve order details"
            assert "crm_user_token" in spec["caller_credentials"]

    async def test_own_actions_have_specs_from_schema_files(self):
        """Gateway node exposes its own action specs from loaded schema files."""
        with tempfile.TemporaryDirectory() as d:
            app = _make_app(d)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                caps = await c.get("/capabilities")
            data = caps.json()
            # execute_command has a schema file → should appear in own actions
            assert "execute_command" in data["actions"]


# ---------------------------------------------------------------------------
# 6. POST /action — policy + credential enforcement via HTTP
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not installed")
@pytest.mark.asyncio
class TestEndToEndV511:

    def _make_app_with_policies(self, tmpdir: str, policies_yaml: str = ""):
        yaml_path = os.path.join(tmpdir, "node.yaml")
        yaml_content = "node_id: node-0\nlisten: 0.0.0.0:8080\nauth_token: sk-test\n"
        if policies_yaml:
            yaml_content += policies_yaml
        with open(yaml_path, "w") as f:
            f.write(yaml_content)
        config = load_config(yaml_path)
        seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
        from runtime.schema_validator import load_schemas
        registry = load_actions(seed_dir)
        schema_reg = load_schemas(seed_dir)
        return create_app(config, registry, schema_registry=schema_reg)

    async def test_no_policies_any_token_allowed(self):
        """Level 1: no caller_policies → any valid Bearer token can call any action."""
        with tempfile.TemporaryDirectory() as d:
            app = self._make_app_with_policies(d)  # no policies
            payload = {
                "target_node_id": "node-0",
                "payload": {"action": "execute_command", "params": {"command": "echo hi"}},
            }
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/action", json=payload,
                    headers={"Authorization": "Bearer sk-test"},
                )
            # Should succeed (policy open) — may be sync or async response
            assert resp.status_code in (200, 202)

    async def test_policy_blocks_disallowed_action(self):
        """Level 2: token only allowed certain actions → blocked action returns 400."""
        policies_yaml = (
            "caller_policies:\n"
            "  - token: sk-test\n"
            "    allowed_actions:\n"
            "      - read_file\n"
        )
        with tempfile.TemporaryDirectory() as d:
            app = self._make_app_with_policies(d, policies_yaml)
            payload = {
                "target_node_id": "node-0",
                "payload": {"action": "execute_command", "params": {"command": "echo hi"}},
            }
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/action", json=payload,
                    headers={"Authorization": "Bearer sk-test"},
                )
            assert resp.status_code == 400
            assert "CALLER_NOT_ALLOWED" in resp.json().get("error", "")

    async def test_missing_caller_credential_returns_400(self):
        """Action requires crm_user_token but caller didn't supply it → 400."""
        with tempfile.TemporaryDirectory() as d:
            app = self._make_app_with_policies(d)
            payload = {
                "target_node_id": "node-0",
                "payload": {
                    "action": "get_order_info",
                    "params": {"order_id": "123"},
                },
                # no caller_credentials
            }
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/action", json=payload,
                    headers={"Authorization": "Bearer sk-test"},
                )
            assert resp.status_code == 400
            assert "MISSING_CALLER_CREDENTIAL" in resp.json().get("error", "")

    async def test_with_caller_credential_succeeds(self):
        """Action with crm_user_token supplied succeeds."""
        with tempfile.TemporaryDirectory() as d:
            app = self._make_app_with_policies(d)
            payload = {
                "target_node_id": "node-0",
                "payload": {
                    "action": "get_order_info",
                    "params": {"order_id": "ORD-999"},
                },
                "caller_credentials": {"crm_user_token": "sk-user-abc"},
            }
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/action", json=payload,
                    headers={"Authorization": "Bearer sk-test"},
                )
            assert resp.status_code == 200
            assert resp.json()["output"]["order_id"] == "ORD-999"

    async def test_caller_credentials_not_in_response(self):
        """caller_credentials must NOT appear in the action response output."""
        with tempfile.TemporaryDirectory() as d:
            app = self._make_app_with_policies(d)
            payload = {
                "target_node_id": "node-0",
                "payload": {"action": "get_order_info", "params": {"order_id": "1"}},
                "caller_credentials": {"crm_user_token": "sk-super-secret"},
            }
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/action", json=payload,
                    headers={"Authorization": "Bearer sk-test"},
                )
            response_text = resp.text
            assert "sk-super-secret" not in response_text


# ---------------------------------------------------------------------------
# 7. Config — caller_policies from node.yaml
# ---------------------------------------------------------------------------

class TestConfigV511:

    def test_load_config_with_caller_policies(self):
        yaml = (
            "node_id: node-0\n"
            "listen: 0.0.0.0:8080\n"
            "caller_policies:\n"
            "  - token: sk-sales\n"
            "    allowed_actions:\n"
            "      - get_order_info\n"
            "  - token: sk-admin\n"
            "    allowed_actions: \"*\"\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "node.yaml")
            open(p, "w").write(yaml)
            config = load_config(p)

        assert len(config.caller_policies) == 2
        sales = config.caller_policies[0]
        assert sales.token == "sk-sales"
        assert sales.allowed_actions == ["get_order_info"]
        admin = config.caller_policies[1]
        assert admin.allowed_actions == "*"
        assert admin.allows("anything")

    def test_load_config_no_policies_is_empty(self):
        yaml = "node_id: node-0\nlisten: 0.0.0.0:8080\n"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "node.yaml")
            open(p, "w").write(yaml)
            config = load_config(p)
        assert len(config.caller_policies) == 0


# ---------------------------------------------------------------------------
# 8. IntentHandler — caller_credentials propagated through agent loop
# ---------------------------------------------------------------------------

class TestIntentHandlerV511:

    def _make_handler(self):
        from runtime.intent_handler import IntentHandler
        from runtime.conversation_store import ConversationStore

        config = MagicMock()
        config.node_id = "node-0"
        config.intent_max_turns = 5
        config.pull_job_timeout_seconds = 60
        config.intent_system_prompt = None
        config.llm_enabled = True

        node_registry = MagicMock()
        node_registry.build_capability_tree.return_value = {}

        sv = ActionSchemaValidator({"get_order_info": SCHEMA_WITH_CREDS})
        action_registry = {"get_order_info": MagicMock()}
        router = MagicMock()
        store = ConversationStore()

        return IntentHandler(
            config=config,
            llm_client=MagicMock(),
            gateway_router=router,
            node_registry=node_registry,
            action_registry=action_registry,
            conversation_store=store,
            schema_validator=sv,
        )

    @pytest.mark.asyncio
    async def test_caller_credentials_forwarded_to_tool_call(self):
        """caller_credentials from IntentRequest reach ActionRequest."""
        from runtime.models import IntentRequest, SyncActionResponse

        handler = self._make_handler()
        captured_requests: list[ActionRequest] = []

        from runtime.llm_client import LLMResponse, ToolCall
        fake_tc = ToolCall(
            id="tc1",
            name="mesh_action",
            arguments={
                "target_node_id": "node-1a",
                "action": "get_order_info",
                "params": {"order_id": "123"},
            },
        )

        call_count = 0
        async def fake_llm_chat(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return LLMResponse(content=None, tool_calls=[fake_tc],
                                   usage={"total_tokens": 10}, model="gpt-4o")
            return LLMResponse(content="Done", tool_calls=[], usage={"total_tokens": 5}, model="gpt-4o")

        handler._llm.chat = fake_llm_chat

        async def fake_route(req: ActionRequest):
            captured_requests.append(req)
            return SyncActionResponse(task_id=req.task_id, output={"order_id": "123"})

        handler._router.route = fake_route

        req = IntentRequest(
            prompt="Get order 123",
            caller_credentials={"crm_user_token": "sk-user-xyz"},
        )
        await handler.handle(req)

        assert len(captured_requests) == 1
        ar = captured_requests[0]
        assert ar.caller_credentials.get("crm_user_token") == "sk-user-xyz"

    def test_system_prompt_shows_caller_credential_requirement(self):
        """System prompt includes ⚠ caller_credential marker for actions that need it."""
        handler = self._make_handler()
        prompt = handler._build_system_prompt()
        assert "crm_user_token" in prompt
        assert "caller_credential" in prompt

    def test_system_prompt_shows_action_description(self):
        handler = self._make_handler()
        prompt = handler._build_system_prompt()
        assert "get_order_info" in prompt
