"""Tests for Phase 6 — External Participant Interaction.

Covers:
  1. ExternalParticipantRegistry — register, list, role lookup, update, deactivate
  2. ChannelLog — open_thread, add_reply, resolve_thread, list_pending
  3. InteractionRouter — role routing, event emission, webhook (mocked)
  4. HTTP endpoints — POST /participants/register, GET /participants,
     PATCH/DELETE, GET /channels/.../log, GET pending, POST respond
  5. suspend_and_ask target_role → participant.input_required event
  6. Acceptance criteria: register participant, agent asks, human answers → task resumes
"""

from __future__ import annotations

import asyncio
import time
import uuid
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from runtime.models import (
    ExternalParticipant,
    InteractionReply,
    InteractionThread,
    InteractionRespondRequest,
    ParticipantRegisterRequest,
    ParticipantUpdateRequest,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def tmp_path_str(tmp_path):
    return str(tmp_path)


@pytest.fixture
async def participant_registry(tmp_path_str):
    from runtime.external_participant_registry import ExternalParticipantRegistry
    reg = ExternalParticipantRegistry(path=tmp_path_str, node_id="test-node")
    await reg.startup_load()
    return reg


@pytest.fixture
async def channel_log(tmp_path_str):
    from runtime.channel_log import ChannelLog
    log = ChannelLog(path=tmp_path_str, node_id="test-node")
    await log.startup_load()
    return log


@pytest.fixture
def mock_event_bus():
    bus = MagicMock()
    bus.emit = AsyncMock(return_value=1)
    return bus


@pytest.fixture
async def interaction_router(participant_registry, channel_log, mock_event_bus):
    from runtime.interaction_router import InteractionRouter
    return InteractionRouter(
        participant_registry=participant_registry,
        channel_log=channel_log,
        event_bus=mock_event_bus,
        node_id="test-node",
    )


# Helper: build a participant
def make_participant(**kwargs) -> ExternalParticipant:
    return ExternalParticipant(
        name=kwargs.get("name", "Alice"),
        roles=kwargs.get("roles", ["pm"]),
        transport=kwargs.get("transport", "polling"),
        transport_target=kwargs.get("transport_target", ""),
        auth_token=kwargs.get("auth_token", "secret-alice"),
        cluster_id=kwargs.get("cluster_id", "cluster-test"),
    )


# =============================================================================
# 1. ExternalParticipantRegistry
# =============================================================================

class TestExternalParticipantRegistry:
    @pytest.mark.asyncio
    async def test_register_returns_participant_with_id(self, participant_registry):
        """Registering a participant assigns participant_id and persists."""
        p = make_participant()
        saved = await participant_registry.register(p)
        assert saved.participant_id.startswith("participant-")
        assert saved.name == "Alice"
        assert "pm" in saved.roles

    @pytest.mark.asyncio
    async def test_get_by_id_returns_correct_participant(self, participant_registry):
        p = make_participant(name="Bob", roles=["developer"])
        saved = await participant_registry.register(p)
        fetched = await participant_registry.get(saved.participant_id)
        assert fetched.name == "Bob"
        assert fetched.roles == ["developer"]

    @pytest.mark.asyncio
    async def test_get_unknown_raises_not_found(self, participant_registry):
        from runtime.external_participant_registry import ParticipantNotFoundError
        with pytest.raises(ParticipantNotFoundError):
            await participant_registry.get("nonexistent-id")

    @pytest.mark.asyncio
    async def test_list_by_role_returns_matching(self, participant_registry):
        pm = make_participant(name="PM Alice", roles=["pm", "product-owner"])
        dev = make_participant(name="Dev Bob", roles=["developer"])
        await participant_registry.register(pm)
        await participant_registry.register(dev)

        pms = await participant_registry.list_by_role("pm", cluster_id="cluster-test")
        assert len(pms) == 1
        assert pms[0].name == "PM Alice"

    @pytest.mark.asyncio
    async def test_list_by_role_returns_multiple(self, participant_registry):
        """Multiple participants with same role are all returned."""
        pm1 = make_participant(name="PM1", roles=["pm"])
        pm2 = make_participant(name="PM2", roles=["pm"])
        await participant_registry.register(pm1)
        await participant_registry.register(pm2)

        pms = await participant_registry.list_by_role("pm", cluster_id="cluster-test")
        assert len(pms) == 2

    @pytest.mark.asyncio
    async def test_deactivate_excludes_from_active_list(self, participant_registry):
        p = make_participant(name="Ex-PM")
        saved = await participant_registry.register(p)
        await participant_registry.deactivate(saved.participant_id)

        active = await participant_registry.list_by_role("pm", active_only=True)
        ids = [x.participant_id for x in active]
        assert saved.participant_id not in ids

    @pytest.mark.asyncio
    async def test_update_roles(self, participant_registry):
        p = make_participant(name="Flexible", roles=["pm"])
        saved = await participant_registry.register(p)
        updated = await participant_registry.update(
            saved.participant_id, {"roles": ["pm", "product-owner"]}
        )
        assert "product-owner" in updated.roles

    @pytest.mark.asyncio
    async def test_verify_auth_correct_token(self, participant_registry):
        p = make_participant(auth_token="tok-123")
        saved = await participant_registry.register(p)
        # Should not raise
        participant_registry.verify_auth(saved, "tok-123")

    @pytest.mark.asyncio
    async def test_verify_auth_wrong_token_raises(self, participant_registry):
        from runtime.external_participant_registry import ParticipantAuthError
        p = make_participant(auth_token="tok-correct")
        saved = await participant_registry.register(p)
        with pytest.raises(ParticipantAuthError):
            participant_registry.verify_auth(saved, "tok-wrong")

    @pytest.mark.asyncio
    async def test_persistence_survives_reload(self, tmp_path_str):
        """Participants registered in one instance are loaded by a new instance."""
        from runtime.external_participant_registry import ExternalParticipantRegistry
        reg1 = ExternalParticipantRegistry(path=tmp_path_str, node_id="node-x")
        await reg1.startup_load()
        p = make_participant(name="Persisted")
        saved = await reg1.register(p)

        reg2 = ExternalParticipantRegistry(path=tmp_path_str, node_id="node-x")
        n = await reg2.startup_load()
        assert n == 1
        fetched = await reg2.get(saved.participant_id)
        assert fetched.name == "Persisted"


# =============================================================================
# 2. ChannelLog
# =============================================================================

class TestChannelLog:
    def _make_thread(self, **kwargs) -> InteractionThread:
        return InteractionThread(
            question_id=kwargs.get("question_id", f"q-{uuid.uuid4().hex[:8]}"),
            source_agent=kwargs.get("source_agent", "dev-A"),
            required_role=kwargs.get("required_role", "pm"),
            question_text=kwargs.get("question", "What priority is this?"),
            cluster_id=kwargs.get("cluster_id", "cluster-test"),
        )

    @pytest.mark.asyncio
    async def test_open_thread_creates_entry(self, channel_log):
        thread = self._make_thread(question_id="q-open-1")
        saved = await channel_log.open_thread(thread)
        assert saved.question_id == "q-open-1"
        assert saved.status == "open"

    @pytest.mark.asyncio
    async def test_get_thread_returns_correct(self, channel_log):
        thread = self._make_thread(question_id="q-get-1")
        await channel_log.open_thread(thread)
        fetched = await channel_log.get_thread("cluster-test", "q-get-1")
        assert fetched.question_text == "What priority is this?"

    @pytest.mark.asyncio
    async def test_get_unknown_thread_raises(self, channel_log):
        from runtime.channel_log import ThreadNotFoundError
        with pytest.raises(ThreadNotFoundError):
            await channel_log.get_thread("cluster-test", "q-unknown-999")

    @pytest.mark.asyncio
    async def test_add_reply_appends_to_thread(self, channel_log):
        thread = self._make_thread(question_id="q-reply-1")
        await channel_log.open_thread(thread)
        reply = InteractionReply(
            question_id="q-reply-1",
            participant_id="p-alice",
            content="High priority",
            reply_type="comment",
        )
        updated = await channel_log.add_reply("cluster-test", reply)
        assert len(updated.replies) == 1
        assert updated.replies[0].content == "High priority"

    @pytest.mark.asyncio
    async def test_resolve_thread_marks_answered(self, channel_log):
        thread = self._make_thread(question_id="q-resolve-1")
        await channel_log.open_thread(thread)
        resolution = InteractionReply(
            question_id="q-resolve-1",
            participant_id="p-alice",
            content="P1 — critical",
            reply_type="answer",
        )
        resolved = await channel_log.resolve_thread("cluster-test", "q-resolve-1", resolution)
        assert resolved.status == "answered"
        assert resolved.resolution is not None
        assert resolved.resolution.content == "P1 — critical"
        assert resolved.answered_at is not None

    @pytest.mark.asyncio
    async def test_resolve_twice_raises_already_answered(self, channel_log):
        from runtime.channel_log import ThreadAlreadyAnsweredError
        thread = self._make_thread(question_id="q-twice-1")
        await channel_log.open_thread(thread)
        resolution = InteractionReply(
            question_id="q-twice-1", participant_id="p-alice",
            content="answer 1", reply_type="answer",
        )
        await channel_log.resolve_thread("cluster-test", "q-twice-1", resolution)
        resolution2 = InteractionReply(
            question_id="q-twice-1", participant_id="p-bob",
            content="answer 2", reply_type="answer",
        )
        with pytest.raises(ThreadAlreadyAnsweredError):
            await channel_log.resolve_thread("cluster-test", "q-twice-1", resolution2)

    @pytest.mark.asyncio
    async def test_list_pending_returns_open_threads(self, channel_log):
        t1 = self._make_thread(question_id="q-pending-1", required_role="pm")
        t2 = self._make_thread(question_id="q-pending-2", required_role="devops")
        await channel_log.open_thread(t1)
        await channel_log.open_thread(t2)

        pending = await channel_log.list_pending("cluster-test")
        ids = [t.question_id for t in pending]
        assert "q-pending-1" in ids
        assert "q-pending-2" in ids

    @pytest.mark.asyncio
    async def test_list_pending_filters_by_role(self, channel_log):
        t1 = self._make_thread(question_id="q-role-1", required_role="pm")
        t2 = self._make_thread(question_id="q-role-2", required_role="devops")
        await channel_log.open_thread(t1)
        await channel_log.open_thread(t2)

        pm_pending = await channel_log.list_pending("cluster-test", required_role="pm")
        assert len(pm_pending) == 1
        assert pm_pending[0].question_id == "q-role-1"

    @pytest.mark.asyncio
    async def test_answered_thread_excluded_from_pending(self, channel_log):
        thread = self._make_thread(question_id="q-done-1")
        await channel_log.open_thread(thread)
        resolution = InteractionReply(
            question_id="q-done-1", participant_id="p-alice",
            content="done", reply_type="answer",
        )
        await channel_log.resolve_thread("cluster-test", "q-done-1", resolution)

        pending = await channel_log.list_pending("cluster-test")
        ids = [t.question_id for t in pending]
        assert "q-done-1" not in ids


# =============================================================================
# 3. InteractionRouter
# =============================================================================

class TestInteractionRouter:
    @pytest.mark.asyncio
    async def test_route_creates_thread_in_channel_log(
        self, interaction_router, channel_log
    ):
        """Routing a question creates an InteractionThread."""
        result = await interaction_router.route(
            question_id="q-router-1",
            question_text="Should we use Python or Go?",
            required_role="pm",
            source_agent="dev-A",
            cluster_id="cluster-test",
        )
        assert result["question_id"] == "q-router-1"
        thread = await channel_log.get_thread("cluster-test", "q-router-1")
        assert thread.status == "open"
        assert thread.required_role == "pm"

    @pytest.mark.asyncio
    async def test_route_notifies_matching_participant(
        self, interaction_router, participant_registry, mock_event_bus
    ):
        """Routing notifies participants with matching role."""
        pm = make_participant(name="PM Alice", roles=["pm"])
        await participant_registry.register(pm)

        result = await interaction_router.route(
            question_id="q-router-2",
            question_text="Which framework?",
            required_role="pm",
            source_agent="dev-A",
            cluster_id="cluster-test",
        )
        assert len(result["notified"]) == 1
        assert result["participants_found"] == 1

    @pytest.mark.asyncio
    async def test_route_emits_participant_input_required(
        self, interaction_router, participant_registry, mock_event_bus
    ):
        """Route emits participant.input_required event."""
        await interaction_router.route(
            question_id="q-event-1",
            question_text="Priority?",
            required_role="pm",
            source_agent="dev-A",
            cluster_id="cluster-test",
        )
        calls = [str(c) for c in mock_event_bus.emit.call_args_list]
        assert any("participant.input_required" in c for c in calls)

    @pytest.mark.asyncio
    async def test_route_no_matching_participants_returns_zero_notified(
        self, interaction_router
    ):
        """Route with no participants found still creates thread."""
        result = await interaction_router.route(
            question_id="q-empty-1",
            question_text="Budget?",
            required_role="cfo",  # no CFO registered
            source_agent="dev-A",
            cluster_id="cluster-test",
        )
        assert result["participants_found"] == 0
        assert result["notified"] == []

    @pytest.mark.asyncio
    async def test_handle_answer_emits_clarification_answered(
        self, interaction_router, mock_event_bus
    ):
        """handle_answer emits both participant.answered and clarification.answered."""
        await interaction_router.handle_answer(
            cluster_id="cluster-test",
            question_id="q-answer-1",
            participant_id="p-alice",
            content="Use Python",
        )
        emitted_types = []
        for call in mock_event_bus.emit.call_args_list:
            event = call[0][0] if call[0] else call[1].get("event")
            if hasattr(event, "event_type"):
                emitted_types.append(event.event_type)
        assert "participant.answered" in emitted_types
        assert "clarification.answered" in emitted_types


# =============================================================================
# 4. HTTP Endpoints
# =============================================================================

@pytest.fixture
def test_app(tmp_path):
    from runtime.config import NodeConfig
    from runtime.action_loader import ActionRegistry
    config = NodeConfig(
        node_id="test-node",
        listen="0.0.0.0:9000",
        participants_dir=str(tmp_path / "participants"),
        channel_log_dir=str(tmp_path / "channels"),
    )
    registry = ActionRegistry(base_dir=".", actions_dir=None)
    app = __import__("runtime.server", fromlist=["create_app"]).create_app(config, registry)
    return app


@pytest.fixture
def client(test_app):
    from httpx import AsyncClient, ASGITransport
    return AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test")


class TestParticipantHTTPEndpoints:
    @pytest.mark.asyncio
    async def test_register_participant_returns_201(self, client):
        async with client as c:
            resp = await c.post("/participants/register", json={
                "name": "Alice PM",
                "roles": ["pm", "product-owner"],
                "transport": "polling",
                "transport_target": "",
                "auth_token": "tok-alice",
                "cluster_id": "cluster-001",
            })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Alice PM"
        assert "pm" in data["roles"]
        assert data["participant_id"].startswith("participant-")

    @pytest.mark.asyncio
    async def test_list_participants_returns_registered(self, client):
        async with client as c:
            await c.post("/participants/register", json={
                "name": "Bob Dev",
                "roles": ["developer"],
                "transport": "polling",
                "transport_target": "",
                "auth_token": "tok-bob",
                "cluster_id": "cluster-001",
            })
            resp = await c.get("/participants", params={"cluster_id": "cluster-001"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        names = [p["name"] for p in data["participants"]]
        assert "Bob Dev" in names

    @pytest.mark.asyncio
    async def test_list_participants_filter_by_role(self, client):
        async with client as c:
            await c.post("/participants/register", json={
                "name": "PM Carol",
                "roles": ["pm"],
                "transport": "polling",
                "transport_target": "",
                "auth_token": "tok-carol",
                "cluster_id": "cluster-002",
            })
            await c.post("/participants/register", json={
                "name": "Dev Dave",
                "roles": ["developer"],
                "transport": "polling",
                "transport_target": "",
                "auth_token": "tok-dave",
                "cluster_id": "cluster-002",
            })
            resp = await c.get("/participants", params={"cluster_id": "cluster-002", "role": "pm"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["participants"][0]["name"] == "PM Carol"

    @pytest.mark.asyncio
    async def test_patch_participant_updates_roles(self, client):
        async with client as c:
            reg = await c.post("/participants/register", json={
                "name": "Multi-Role Eve",
                "roles": ["pm"],
                "transport": "polling",
                "transport_target": "",
                "auth_token": "tok-eve",
                "cluster_id": "cluster-003",
            })
            pid = reg.json()["participant_id"]
            resp = await c.patch(f"/participants/{pid}", json={"roles": ["pm", "product-owner"]})
        assert resp.status_code == 200
        assert "product-owner" in resp.json()["roles"]

    @pytest.mark.asyncio
    async def test_delete_participant_deactivates(self, client):
        async with client as c:
            reg = await c.post("/participants/register", json={
                "name": "To Deactivate",
                "roles": ["pm"],
                "transport": "polling",
                "transport_target": "",
                "auth_token": "tok-x",
                "cluster_id": "cluster-004",
            })
            pid = reg.json()["participant_id"]
            resp = await c.delete(f"/participants/{pid}")
        assert resp.status_code == 200
        assert resp.json()["deactivated"] is True


class TestChannelLogHTTPEndpoints:
    @pytest.mark.asyncio
    async def test_get_channel_log_empty(self, client):
        async with client as c:
            resp = await c.get("/channels/cluster-empty/log")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_get_pending_empty(self, client):
        async with client as c:
            resp = await c.get("/channels/cluster-empty/pending")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_respond_requires_valid_participant(self, client):
        """Responding with unknown participant_id returns 404."""
        async with client as c:
            resp = await c.post(
                "/channels/cluster-x/interactions/q-fake/respond",
                json={
                    "participant_id": "p-nonexistent",
                    "auth_token": "tok-x",
                    "content": "answer",
                    "reply_type": "answer",
                },
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_full_flow_register_ask_answer(self, client):
        """
        Acceptance criterion:
        1. Human registers with roles: pm, product-owner
        2. (Simulated) question opens a thread
        3. Human submits answer → ChannelLog shows answered thread
        """
        async with client as c:
            # 1. Register participant
            reg_resp = await c.post("/participants/register", json={
                "name": "PM Frank",
                "roles": ["pm", "product-owner"],
                "transport": "polling",
                "transport_target": "",
                "auth_token": "tok-frank",
                "cluster_id": "cluster-acceptance",
            })
            assert reg_resp.status_code == 201
            participant_id = reg_resp.json()["participant_id"]

            # 2. Open a thread (would normally be done by InteractionRouter
            #    when handling participant.input_required event)
            # Use the channel_log directly via app.state
            from runtime.channel_log import ChannelLog
            from runtime.models import InteractionThread
            cl = client._transport.app.state.channel_log
            thread = InteractionThread(
                question_id="q-accept-1",
                source_agent="dev-A",
                required_role="pm",
                question_text="Which database should we use?",
                cluster_id="cluster-acceptance",
            )
            await cl.open_thread(thread)

            # 3. Check pending
            pending_resp = await c.get(
                "/channels/cluster-acceptance/pending",
                params={"role": "pm"},
            )
            assert pending_resp.status_code == 200
            assert pending_resp.json()["total"] == 1

            # 4. Human submits answer
            answer_resp = await c.post(
                "/channels/cluster-acceptance/interactions/q-accept-1/respond",
                json={
                    "participant_id": participant_id,
                    "auth_token": "tok-frank",
                    "content": "PostgreSQL — it fits our needs",
                    "reply_type": "answer",
                },
            )
            assert answer_resp.status_code == 200
            data = answer_resp.json()
            assert data["accepted"] is True
            assert data["thread_status"] == "answered"

            # 5. ChannelLog shows full thread history
            log_resp = await c.get("/channels/cluster-acceptance/log")
            assert log_resp.status_code == 200
            threads = log_resp.json()["threads"]
            assert len(threads) == 1
            t = threads[0]
            assert t["status"] == "answered"
            assert t["resolution"]["content"] == "PostgreSQL — it fits our needs"

            # 6. Thread is no longer in pending
            pending_after = await c.get("/channels/cluster-acceptance/pending")
            assert pending_after.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_wrong_auth_token_returns_403(self, client):
        async with client as c:
            reg = await c.post("/participants/register", json={
                "name": "Authenticated Gary",
                "roles": ["pm"],
                "transport": "polling",
                "transport_target": "",
                "auth_token": "tok-correct",
                "cluster_id": "cluster-auth",
            })
            pid = reg.json()["participant_id"]

            # Open thread
            cl = client._transport.app.state.channel_log
            from runtime.models import InteractionThread
            await cl.open_thread(InteractionThread(
                question_id="q-auth-1",
                source_agent="dev-A",
                required_role="pm",
                question_text="Question?",
                cluster_id="cluster-auth",
            ))

            resp = await c.post(
                "/channels/cluster-auth/interactions/q-auth-1/respond",
                json={
                    "participant_id": pid,
                    "auth_token": "tok-WRONG",
                    "content": "answer",
                    "reply_type": "answer",
                },
            )
        assert resp.status_code == 403


# =============================================================================
# 5. TaskPool emits participant.input_required for target_role
# =============================================================================

class TestTaskPoolTargetRoleEmission:
    @pytest.mark.asyncio
    async def test_suspend_with_target_role_emits_participant_event(self, tmp_path):
        """TaskPool.suspend_task emits participant.input_required when target_role is set."""
        from runtime.task_pool import TaskPool
        from runtime.checkpoint_store import CheckpointStore, TaskSuspendedException
        from runtime.job_manager import JobManager

        event_bus = MagicMock()
        emitted_events = []

        async def record_emit(event):
            emitted_events.append(event)
            return 1

        event_bus.emit = record_emit

        cp_store = CheckpointStore(
            path=str(tmp_path),
            node_id="node-test",
            event_bus=None,
        )
        await cp_store.startup_load()

        job_manager = JobManager(job_ttl_seconds=300, cleanup_interval_seconds=60)

        pool = TaskPool(
            checkpoint_store=cp_store,
            job_manager=job_manager,
            node_id="node-test",
            max_active_tasks=3,
            event_bus=event_bus,
        )

        exc = TaskSuspendedException(
            question="What should we use?",
            question_id="q-target-1",
            ask_node="",
            target_role="pm",
            timeout_seconds=3600,
            timeout_action="use_assumption",
            assumption="use default",
            suspend_tool_call_id="tc-123",
        )

        checkpoint = await pool.handle_suspension(
            exc=exc,
            task_id="task-target-1",
            session_id="sess-1",
            original_prompt="Build the API",
            messages=[],
            turn_count=3,
        )

        types = [e.event_type for e in emitted_events]
        assert "clarification.needed" in types
        assert "participant.input_required" in types

        pi_event = next(e for e in emitted_events if e.event_type == "participant.input_required")
        assert pi_event.payload["required_role"] == "pm"
        assert pi_event.payload["question_id"] == "q-target-1"
