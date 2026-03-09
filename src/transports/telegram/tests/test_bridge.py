"""Tests for TelegramBridge.notify() and BotInstanceRegistry.

Covers:
  1. TelegramBridge.notify() — routes to correct bot instance
  2. TelegramBridge.notify() — no matching instance → warning logged
  3. TelegramBridge.notify() — invalid transport_target → warning, no crash
  4. TelegramBridge.notify() — exception in send_question_notification → caught
  5. BotInstanceRegistry.startup_load() — empty file
  6. BotInstanceRegistry.startup_load() — loads records
  7. BotInstanceRegistry.register() — persists record
  8. BotInstanceRegistry.deactivate() — marks inactive
  9. BotInstanceRegistry.find_by_cluster() — filters correctly
  10. BotInstanceRegistry.list_active() — only active records
"""

from __future__ import annotations

import json
import asyncio
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from runtime.models import ExternalParticipant, InteractionThread
from transports.telegram.bot_registry import BotInstanceRegistry, BotRegistryError
from transports.telegram.config import TelegramBotRecord


# ── Helpers ────────────────────────────────────────────────────────────────

def make_record(
    bot_id: str = "bot-alice-abc",
    cluster_id: str = "cluster-A",
    active: bool = True,
    mode: str = "polling",
) -> TelegramBotRecord:
    return TelegramBotRecord(
        bot_id=bot_id,
        bot_token="tok-test",
        bot_username="@test_bot",
        owner_user_id="alice",
        cluster_id=cluster_id,
        mode=mode,
        active=active,
    )


def make_participant(transport_target: str = "987654321") -> ExternalParticipant:
    return ExternalParticipant(
        participant_id="p-alice",
        name="Alice PM",
        roles=["pm"],
        transport="telegram",
        transport_target=transport_target,
        cluster_id="cluster-A",
        auth_token="tok-alice",
    )


def make_thread(cluster_id: str = "cluster-A") -> InteractionThread:
    return InteractionThread(
        question_id="q-001",
        source_agent="dev-A",
        required_role="pm",
        question_text="Use PostgreSQL or MySQL?",
        cluster_id=cluster_id,
        status="open",
    )


# ── BotInstanceRegistry tests ──────────────────────────────────────────────

class TestBotInstanceRegistry:

    @pytest.mark.asyncio
    async def test_startup_load_empty(self):
        """startup_load on nonexistent file returns 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = BotInstanceRegistry(storage_path=f"{tmpdir}/bots.jsonl")
            loaded = await reg.startup_load()
            assert loaded == 0
            assert reg.list_all() == []

    @pytest.mark.asyncio
    async def test_startup_load_existing_records(self):
        """startup_load reads records from JSONL file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bots.jsonl"
            record = make_record()
            path.write_text(json.dumps(record.to_dict()) + "\n")

            reg = BotInstanceRegistry(storage_path=str(path))
            loaded = await reg.startup_load()

            assert loaded == 1
            assert reg.get("bot-alice-abc") is not None

    @pytest.mark.asyncio
    async def test_startup_load_skips_malformed_lines(self):
        """startup_load skips lines that can't be parsed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bots.jsonl"
            good = make_record(bot_id="bot-good")
            path.write_text("not-json\n" + json.dumps(good.to_dict()) + "\n")

            reg = BotInstanceRegistry(storage_path=str(path))
            loaded = await reg.startup_load()

            assert loaded == 1
            assert reg.get("bot-good") is not None

    @pytest.mark.asyncio
    async def test_register_persists(self):
        """register() stores record and writes to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bots.jsonl"
            reg = BotInstanceRegistry(storage_path=str(path))

            record = make_record()
            await reg.register(record)

            assert reg.get("bot-alice-abc") is not None
            assert path.exists()
            lines = [l for l in path.read_text().strip().split("\n") if l]
            assert len(lines) == 1

    @pytest.mark.asyncio
    async def test_register_duplicate_active_raises(self):
        """register() raises BotRegistryError for duplicate active bot."""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = BotInstanceRegistry(storage_path=f"{tmpdir}/bots.jsonl")
            record = make_record()
            await reg.register(record)

            with pytest.raises(BotRegistryError):
                await reg.register(record)

    @pytest.mark.asyncio
    async def test_deactivate_marks_inactive(self):
        """deactivate() marks record as inactive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = BotInstanceRegistry(storage_path=f"{tmpdir}/bots.jsonl")
            await reg.register(make_record())

            result = await reg.deactivate("bot-alice-abc")
            assert result.active is False
            assert reg.list_active() == []

    @pytest.mark.asyncio
    async def test_deactivate_nonexistent_raises(self):
        """deactivate() raises KeyError for unknown bot_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = BotInstanceRegistry(storage_path=f"{tmpdir}/bots.jsonl")
            with pytest.raises(KeyError):
                await reg.deactivate("nonexistent")

    @pytest.mark.asyncio
    async def test_list_active_filters_inactive(self):
        """list_active() returns only active records."""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = BotInstanceRegistry(storage_path=f"{tmpdir}/bots.jsonl")
            await reg.register(make_record(bot_id="bot-a"))
            await reg.register(make_record(bot_id="bot-b"))
            await reg.deactivate("bot-a")

            active = reg.list_active()
            assert len(active) == 1
            assert active[0].bot_id == "bot-b"

    @pytest.mark.asyncio
    async def test_find_by_cluster(self):
        """find_by_cluster() returns bots for the given cluster."""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = BotInstanceRegistry(storage_path=f"{tmpdir}/bots.jsonl")
            await reg.register(make_record(bot_id="bot-a", cluster_id="cluster-A"))
            await reg.register(make_record(bot_id="bot-b", cluster_id="cluster-B"))

            result = reg.find_by_cluster("cluster-A")
            assert len(result) == 1
            assert result[0].bot_id == "bot-a"

    @pytest.mark.asyncio
    async def test_persistence_survives_reload(self):
        """Records persisted to disk are loadable after new registry instance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/bots.jsonl"
            reg1 = BotInstanceRegistry(storage_path=path)
            await reg1.register(make_record(bot_id="bot-persist"))

            reg2 = BotInstanceRegistry(storage_path=path)
            loaded = await reg2.startup_load()
            assert loaded == 1
            assert reg2.get("bot-persist") is not None

    def test_generate_bot_id_format(self):
        """generate_bot_id produces expected prefix format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = BotInstanceRegistry(storage_path=f"{tmpdir}/bots.jsonl")
            bot_id = reg.generate_bot_id("alice")
            assert bot_id.startswith("bot-alice-")
            assert len(bot_id) > len("bot-alice-")


# ── TelegramBridge.notify() tests ──────────────────────────────────────────

class TestTelegramBridgeNotify:

    def _make_bridge_with_mock_instance(self, cluster_id: str = "cluster-A"):
        """Build a minimal TelegramBridge with a mocked instance."""
        from transports.telegram.bridge import TelegramBridge

        config = MagicMock()
        config.telegram.bots_storage_path = "/tmp/test-bots.jsonl"
        config.telegram.default_message_format = "full"
        config.telegram.default_language = "en"
        config.telegram.poll_interval_seconds = 1
        config.telegram.poll_timeout_seconds = 30
        config.telegram.api_timeout_seconds = 10
        config.telegram.gnot_base_url = None
        config.port = 8080
        config.auth_token = "tok"

        bridge = TelegramBridge.__new__(TelegramBridge)
        bridge._config = config
        bridge._instances = {}
        bridge._registry = MagicMock()
        bridge._message_format = "full"
        bridge._language = "en"
        bridge._ready = True

        # Add a mock instance for cluster-A
        mock_instance = MagicMock()
        mock_instance.record = make_record(cluster_id=cluster_id)
        mock_instance.send_question_notification = AsyncMock()
        bridge._instances["bot-alice-abc"] = mock_instance

        return bridge, mock_instance

    @pytest.mark.asyncio
    async def test_notify_routes_to_correct_instance(self):
        """notify() finds bot for cluster and calls send_question_notification."""
        bridge, mock_instance = self._make_bridge_with_mock_instance("cluster-A")

        participant = make_participant(transport_target="987654321")
        thread = make_thread(cluster_id="cluster-A")

        await bridge.notify(participant, thread)

        mock_instance.send_question_notification.assert_called_once()
        call_kwargs = mock_instance.send_question_notification.call_args[1]
        assert call_kwargs["chat_id"] == 987654321
        assert call_kwargs["question_id"] == "q-001"

    @pytest.mark.asyncio
    async def test_notify_no_matching_instance(self, caplog):
        """notify() logs warning when no bot exists for cluster."""
        bridge, _ = self._make_bridge_with_mock_instance("cluster-A")

        participant = make_participant()
        thread = make_thread(cluster_id="cluster-B")  # different cluster

        await bridge.notify(participant, thread)  # must not raise

        # The mock_instance should NOT have been called
        bridge._instances["bot-alice-abc"].send_question_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_invalid_transport_target(self):
        """notify() logs error for non-integer transport_target, does not raise."""
        bridge, mock_instance = self._make_bridge_with_mock_instance("cluster-A")

        participant = make_participant(transport_target="not-an-int")
        thread = make_thread()

        await bridge.notify(participant, thread)  # must not raise

        mock_instance.send_question_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_empty_transport_target(self):
        """notify() handles empty transport_target gracefully."""
        bridge, mock_instance = self._make_bridge_with_mock_instance("cluster-A")

        participant = make_participant(transport_target="")
        thread = make_thread()

        await bridge.notify(participant, thread)  # must not raise

        mock_instance.send_question_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_exception_caught(self):
        """notify() catches exceptions from send_question_notification."""
        bridge, mock_instance = self._make_bridge_with_mock_instance("cluster-A")
        mock_instance.send_question_notification = AsyncMock(
            side_effect=RuntimeError("Telegram API down")
        )

        participant = make_participant()
        thread = make_thread()

        # Must not raise
        await bridge.notify(participant, thread)
