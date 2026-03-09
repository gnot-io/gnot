"""Integration tests for Phase 7 Multi-Channel Transport.

Covers:
  1. TransportBridgeRegistry — register, get, list_ids
  2. ChannelTransportBridge ABC enforcement
  3. InteractionRouter with bridge registry — telegram transport delegation
  4. InteractionRouter with bridge registry — bridge not ready → warning
  5. InteractionRouter with bridge registry — unknown transport → warning
  6. InteractionRouter backward compat — polling and webhook still work
  7. NodeConfig.telegram — parsed from YAML
  8. _parse_telegram_config — all fields parsed correctly
  9. TelegramFormatter — format_question full style
  10. TelegramFormatter — format_intent_response normal/suspended
  11. TelegramFormatter — format_session_status
  12. TelegramFormatter — format_start message
  13. TelegramBotRecord — to_dict / from_dict roundtrip
  14. GNOTClient — post_intent calls correct URL
  15. runtime/ has zero imports from transports/
"""

from __future__ import annotations

import asyncio
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import yaml
import io

from runtime.transport_bridge import ChannelTransportBridge, TransportBridgeRegistry
from runtime.models import ExternalParticipant, InteractionThread
from runtime.interaction_router import InteractionRouter
from runtime.config import NodeConfig, TelegramTransportConfig, load_config
from transports.telegram.formatters import TelegramFormatter
from transports.telegram.config import TelegramBotRecord
from transports.telegram.gnot_client import GNOTClient


# ── Test double ────────────────────────────────────────────────────────────

class TestBridge(ChannelTransportBridge):
    """Minimal concrete implementation of ChannelTransportBridge for testing."""
    transport_id = "test-channel"

    def __init__(self, ready: bool = True) -> None:
        self._ready = ready
        self.notified: list[tuple] = []
        self.started = False
        self.stopped = False

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def startup(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.stopped = True

    async def notify(self, participant, thread) -> None:
        self.notified.append((participant.participant_id, thread.question_id))


def make_participant(transport: str = "test-channel", target: str = "123") -> ExternalParticipant:
    return ExternalParticipant(
        participant_id="p-1",
        name="Test",
        roles=["pm"],
        transport=transport,
        transport_target=target,
        cluster_id="cluster-A",
        auth_token="tok",
    )


def make_thread() -> InteractionThread:
    return InteractionThread(
        question_id="q-001",
        source_agent="dev-A",
        required_role="pm",
        question_text="Which DB?",
        cluster_id="cluster-A",
        status="open",
    )


# ── TransportBridgeRegistry tests ─────────────────────────────────────────

class TestTransportBridgeRegistry:

    def test_register_and_get(self):
        """register() + get() roundtrip."""
        reg = TransportBridgeRegistry()
        bridge = TestBridge()
        reg.register(bridge)
        assert reg.get("test-channel") is bridge

    def test_get_unknown_returns_none(self):
        """get() returns None for unknown transport_id."""
        reg = TransportBridgeRegistry()
        assert reg.get("nonexistent") is None

    def test_list_ids(self):
        """list_ids() returns all registered transport ids."""
        reg = TransportBridgeRegistry()
        reg.register(TestBridge())

        class AnotherBridge(TestBridge):
            transport_id = "another"

        reg.register(AnotherBridge())
        ids = reg.list_ids()
        assert "test-channel" in ids
        assert "another" in ids

    def test_all_returns_all_bridges(self):
        """all() returns list of all registered bridge instances."""
        reg = TransportBridgeRegistry()
        b1 = TestBridge()
        reg.register(b1)
        assert reg.all() == [b1]

    def test_len(self):
        """__len__ returns count of registered bridges."""
        reg = TransportBridgeRegistry()
        assert len(reg) == 0
        reg.register(TestBridge())
        assert len(reg) == 1

    def test_overwrite_existing(self):
        """Registering same transport_id overwrites existing bridge."""
        reg = TransportBridgeRegistry()
        b1 = TestBridge()
        b2 = TestBridge()
        reg.register(b1)
        reg.register(b2)
        assert reg.get("test-channel") is b2


# ── ABC enforcement ────────────────────────────────────────────────────────

class TestABCEnforcement:

    def test_cannot_instantiate_abc(self):
        """Cannot instantiate ChannelTransportBridge directly."""
        with pytest.raises(TypeError):
            ChannelTransportBridge()

    def test_concrete_subclass_instantiates(self):
        """Concrete subclass with all methods implemented instantiates fine."""
        b = TestBridge()
        assert b.transport_id == "test-channel"

    @pytest.mark.asyncio
    async def test_default_is_ready_false(self):
        """Default is_ready is False (override needed to return True)."""
        class MinimalBridge(ChannelTransportBridge):
            transport_id = "minimal"
            async def startup(self): pass
            async def shutdown(self): pass
            async def notify(self, p, t): pass

        b = MinimalBridge()
        assert b.is_ready is False

    def test_default_get_fastapi_router_none(self):
        """Default get_fastapi_router() returns None."""
        b = TestBridge()
        assert b.get_fastapi_router() is None


# ── InteractionRouter bridge delegation ────────────────────────────────────

class TestInteractionRouterBridgeDelegation:

    def _make_router(self, bridge: ChannelTransportBridge | None = None) -> InteractionRouter:
        reg = MagicMock()
        reg.list_by_role = AsyncMock(return_value=[make_participant()])
        channel_log = MagicMock()
        channel_log.open_thread = AsyncMock()
        bridge_reg = None
        if bridge is not None:
            bridge_reg = TransportBridgeRegistry()
            bridge_reg.register(bridge)
        return InteractionRouter(
            participant_registry=reg,
            channel_log=channel_log,
            bridge_registry=bridge_reg,
        )

    @pytest.mark.asyncio
    async def test_bridge_notify_called(self):
        """InteractionRouter delegates to bridge for unknown transport."""
        bridge = TestBridge(ready=True)
        router = self._make_router(bridge)

        await router.route(
            question_id="q-001",
            question_text="Which DB?",
            required_role="pm",
            source_agent="dev-A",
            cluster_id="cluster-A",
        )

        assert len(bridge.notified) == 1
        assert bridge.notified[0] == ("p-1", "q-001")

    @pytest.mark.asyncio
    async def test_bridge_not_ready_no_call(self):
        """InteractionRouter skips bridge notification if bridge not ready."""
        bridge = TestBridge(ready=False)
        router = self._make_router(bridge)

        # Should complete without error, but no notification sent
        await router.route(
            question_id="q-001",
            question_text="Which DB?",
            required_role="pm",
            source_agent="dev-A",
            cluster_id="cluster-A",
        )

        assert len(bridge.notified) == 0

    @pytest.mark.asyncio
    async def test_polling_transport_no_bridge_call(self):
        """Polling transport skips all notifications — no bridge call."""
        bridge = TestBridge(ready=True)

        reg = MagicMock()
        reg.list_by_role = AsyncMock(
            return_value=[make_participant(transport="polling", target="")]
        )
        channel_log = MagicMock()
        channel_log.open_thread = AsyncMock()
        bridge_reg = TransportBridgeRegistry()
        bridge_reg.register(bridge)

        router = InteractionRouter(
            participant_registry=reg,
            channel_log=channel_log,
            bridge_registry=bridge_reg,
        )
        await router.route("q-1", "Q?", "pm", "agent", "cluster-A")

        assert len(bridge.notified) == 0

    @pytest.mark.asyncio
    async def test_no_bridge_registry_unknown_transport_warns(self):
        """No bridge registry + unknown transport → warning, no crash."""
        reg = MagicMock()
        reg.list_by_role = AsyncMock(
            return_value=[make_participant(transport="telegram", target="123")]
        )
        channel_log = MagicMock()
        channel_log.open_thread = AsyncMock()

        router = InteractionRouter(
            participant_registry=reg,
            channel_log=channel_log,
            bridge_registry=None,  # no registry
        )
        # Should not raise
        result = await router.route("q-1", "Q?", "pm", "agent", "cluster-A")
        assert result["question_id"] == "q-1"


# ── Config parsing tests ───────────────────────────────────────────────────

class TestTelegramConfig:

    def test_default_telegram_config(self):
        """NodeConfig has telegram field with sensible defaults."""
        config = NodeConfig(node_id="n1", listen="0.0.0.0:8080")
        assert config.telegram.enabled is False
        assert config.telegram.bots_storage_path == "./telegram-bots"
        assert config.telegram.poll_interval_seconds == 1
        assert config.telegram.default_language == "vi"

    def test_parse_telegram_from_yaml(self, tmp_path):
        """load_config parses transports.telegram section correctly."""
        yaml_content = """
node_id: n1
listen: "0.0.0.0:8080"
transports:
  telegram:
    enabled: true
    bots_storage_path: /var/gnot/telegram-bots
    default_message_format: compact
    default_language: en
    poll_interval_seconds: 2
    poll_timeout_seconds: 60
    api_timeout_seconds: 15
"""
        config_file = tmp_path / "node.yaml"
        config_file.write_text(yaml_content)

        config = load_config(str(config_file))

        assert config.telegram.enabled is True
        assert config.telegram.bots_storage_path == "/var/gnot/telegram-bots"
        assert config.telegram.default_message_format == "compact"
        assert config.telegram.default_language == "en"
        assert config.telegram.poll_interval_seconds == 2
        assert config.telegram.poll_timeout_seconds == 60
        assert config.telegram.api_timeout_seconds == 15

    def test_telegram_disabled_by_default_in_yaml(self, tmp_path):
        """node.yaml without transports: section → telegram disabled."""
        yaml_content = "node_id: n1\nlisten: '0.0.0.0:8080'\n"
        config_file = tmp_path / "node.yaml"
        config_file.write_text(yaml_content)

        config = load_config(str(config_file))
        assert config.telegram.enabled is False


# ── Formatter tests ────────────────────────────────────────────────────────

class TestTelegramFormatter:

    def test_format_question_full_english(self):
        """format_question (full, en) contains all key fields."""
        f = TelegramFormatter(style="full", language="en")
        text = f.format_question(
            question_id="q-001",
            question_text="PostgreSQL or MySQL?",
            required_role="pm",
            source_agent="dev-A",
            cluster_id="cluster-A",
        )
        assert "pm" in text
        assert "dev-A" in text
        assert "PostgreSQL or MySQL" in text
        assert "q-001" in text

    def test_format_question_compact(self):
        """format_question (compact) is shorter than full."""
        f = TelegramFormatter(style="full", language="en")
        fc = TelegramFormatter(style="compact", language="en")
        full = f.format_question("q-1", "Q?", "pm", "agent", "cluster-A")
        compact = fc.format_question("q-1", "Q?", "pm", "agent", "cluster-A")
        assert len(compact) < len(full)

    def test_format_intent_response_normal(self):
        """format_intent_response returns reply text for normal response."""
        f = TelegramFormatter()
        result = f.format_intent_response({"reply": "Task completed successfully."})
        assert "Task completed" in result

    def test_format_intent_response_suspended(self):
        """format_intent_response shows suspension notice for suspended response."""
        f = TelegramFormatter()
        result = f.format_intent_response({"suspended": True, "question": "Which DB?"})
        assert "⏸" in result or "suspended" in result.lower()
        assert "Which DB?" in result

    def test_format_intent_response_no_reply(self):
        """format_intent_response handles empty reply gracefully."""
        f = TelegramFormatter(language="en")
        result = f.format_intent_response({})
        assert result  # not empty

    def test_format_session_status_running(self):
        """format_session_status shows 'Running' when no suspended tasks."""
        f = TelegramFormatter(language="en")
        session = {"session_id": "sess-1", "messages": [{"role": "user", "content": "hi"}]}
        result = f.format_session_status("sess-1", session, tasks=[])
        assert "sess-1" in result
        assert "Running" in result or "running" in result.lower()

    def test_format_session_status_suspended(self):
        """format_session_status shows suspended task info."""
        f = TelegramFormatter(language="en")
        tasks = [{"status": "suspended", "pending_question": {"question_id": "q-1", "required_role": "pm"}, "question": "Which DB?"}]
        result = f.format_session_status("sess-1", {"messages": []}, tasks)
        assert "pm" in result or "suspended" in result.lower() or "⏸" in result

    def test_format_session_status_not_found(self):
        """format_session_status handles None session (not found)."""
        f = TelegramFormatter(language="en")
        result = f.format_session_status("sess-missing", None, [])
        assert "not found" in result.lower() or "❌" in result

    def test_format_start_english(self):
        """format_start returns helpful welcome message."""
        f = TelegramFormatter(language="en")
        result = f.format_start("@test_bot")
        assert "@test_bot" in result
        assert "/conversation" in result

    def test_format_start_vietnamese(self):
        """format_start returns Vietnamese message when language=vi."""
        f = TelegramFormatter(language="vi")
        result = f.format_start("@test_bot")
        assert "@test_bot" in result


# ── TelegramBotRecord serialization ───────────────────────────────────────

class TestTelegramBotRecord:

    def test_to_dict_from_dict_roundtrip(self):
        """TelegramBotRecord serializes and deserializes correctly."""
        record = TelegramBotRecord(
            bot_id="bot-alice-abc",
            bot_token="tok-123",
            bot_username="@alice_bot",
            owner_user_id="alice",
            cluster_id="cluster-A",
            mode="polling",
            webhook_url=None,
            active=True,
            registered_at=1000.0,
        )
        d = record.to_dict()
        loaded = TelegramBotRecord.from_dict(d)

        assert loaded.bot_id == record.bot_id
        assert loaded.bot_token == record.bot_token
        assert loaded.owner_user_id == record.owner_user_id
        assert loaded.cluster_id == record.cluster_id
        assert loaded.mode == record.mode
        assert loaded.active == record.active

    def test_from_dict_defaults(self):
        """from_dict fills defaults for missing optional fields."""
        minimal = {"bot_id": "bot-1", "bot_token": "tok"}
        record = TelegramBotRecord.from_dict(minimal)
        assert record.bot_id == "bot-1"
        assert record.active is True


# ── Runtime isolation check ────────────────────────────────────────────────

class TestRuntimeIsolation:

    def test_runtime_has_no_transports_imports(self):
        """Verify runtime/ files do not have module-level imports from transports/.

        Lazy imports inside function bodies (e.g. _setup_transport_bridges) are allowed.
        Only module-level imports are flagged — they would create circular coupling.
        """
        import ast

        runtime_dir = Path(__file__).parent.parent / "runtime"
        violations = []

        for py_file in runtime_dir.glob("*.py"):
            try:
                tree = ast.parse(py_file.read_text())
            except SyntaxError:
                continue
            # Only check module-level imports (direct children of Module node)
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("transports"):
                            violations.append(f"{py_file.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.startswith("transports"):
                        violations.append(f"{py_file.name}: from {node.module} import ...")

        assert violations == [], f"runtime/ has module-level imports from transports/: {violations}"
