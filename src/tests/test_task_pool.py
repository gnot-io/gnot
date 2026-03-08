"""Tests for v6.0 Phase 4 — Task Suspension & Resumption.

Covers:
  - TaskCheckpoint model validation
  - CheckpointStore: save / load / update / delete / timeout sweep
  - TaskPool: suspension handling, resumption, status
  - IntentHandler: suspend_and_ask interception, TaskSuspendedException
  - IntentHandler.handle_resume(): checkpoint continuation
  - New JobStatus values: SUSPENDED, RESUMING, TIMED_OUT
  - Server endpoints: GET /tasks, POST /tasks/{id}/answer, DELETE /tasks/{id}
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from runtime.checkpoint_store import CheckpointStore, TaskSuspendedException
from runtime.models import (
    JobStatus,
    TaskCheckpoint,
    TaskAnswerRequest,
    TaskSuspendedResponse,
)
from runtime.task_pool import TaskPool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_checkpoint_dir(tmp_path: Path) -> Path:
    return tmp_path / "checkpoints"


@pytest.fixture
def checkpoint_store(tmp_checkpoint_dir: Path) -> CheckpointStore:
    return CheckpointStore(
        path=str(tmp_checkpoint_dir),
        node_id="test-node",
        sweep_interval_seconds=9999,  # disable auto-sweep
    )


@pytest.fixture
def sample_checkpoint() -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id="task-abc123",
        session_id="session-xyz",
        node_id="test-node",
        original_prompt="Implement the login feature",
        messages=[
            {"role": "user", "content": "Implement the login feature"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-001", "type": "function",
                 "function": {"name": "mesh_action",
                              "arguments": json.dumps({
                                  "target_node_id": "self",
                                  "action": "suspend_and_ask",
                                  "params": {
                                      "question": "Should login use JWT or sessions?",
                                      "ask_node": "architect-A",
                                  }
                              })}}
            ]},
            {"role": "tool", "tool_call_id": "call-001", "name": "mesh_action",
             "content": json.dumps({"status": "suspended", "question_id": "q-test001"})},
        ],
        suspend_tool_call_id="call-001",
        turn_count=1,
        pending_question="Should login use JWT or sessions?",
        pending_question_id="q-test001",
        asked_node="architect-A",
        timeout_seconds=3600,
        assumption="Use JWT",
    )


@pytest.fixture
def mock_job_manager():
    jm = AsyncMock()
    jm.update_job = AsyncMock(return_value=None)
    return jm


@pytest.fixture
def task_pool(checkpoint_store: CheckpointStore, mock_job_manager) -> TaskPool:
    return TaskPool(
        checkpoint_store=checkpoint_store,
        job_manager=mock_job_manager,
        node_id="test-node",
        max_active_tasks=3,
        event_bus=None,
    )


# ---------------------------------------------------------------------------
# JobStatus — new values
# ---------------------------------------------------------------------------

class TestJobStatusExtensions:
    def test_suspended_status_exists(self):
        assert JobStatus.SUSPENDED == "suspended"

    def test_resuming_status_exists(self):
        assert JobStatus.RESUMING == "resuming"

    def test_timed_out_status_exists(self):
        assert JobStatus.TIMED_OUT == "timed_out"

    def test_all_statuses(self):
        statuses = {s.value for s in JobStatus}
        assert "suspended" in statuses
        assert "resuming" in statuses
        assert "timed_out" in statuses
        # Original statuses still present
        assert "accepted" in statuses
        assert "completed" in statuses
        assert "failed" in statuses


# ---------------------------------------------------------------------------
# TaskSuspendedException
# ---------------------------------------------------------------------------

class TestTaskSuspendedException:
    def test_creation(self):
        exc = TaskSuspendedException(
            question="Which DB should I use?",
            question_id="q-001",
            ask_node="architect-A",
            target_role="",
            timeout_seconds=7200,
            timeout_action="use_assumption",
            assumption="PostgreSQL",
            suspend_tool_call_id="call-xyz",
        )
        assert exc.question == "Which DB should I use?"
        assert exc.question_id == "q-001"
        assert exc.ask_node == "architect-A"
        assert exc.timeout_seconds == 7200
        assert exc.assumption == "PostgreSQL"
        assert exc.suspend_tool_call_id == "call-xyz"

    def test_is_exception(self):
        exc = TaskSuspendedException(
            question="?", question_id="q-1", ask_node="", target_role="",
            timeout_seconds=100, timeout_action="use_assumption", assumption="",
            suspend_tool_call_id="call-1",
        )
        assert isinstance(exc, Exception)


# ---------------------------------------------------------------------------
# TaskCheckpoint model
# ---------------------------------------------------------------------------

class TestTaskCheckpoint:
    def test_default_checkpoint_id_generated(self):
        cp = TaskCheckpoint(
            task_id="t1", session_id="s1", node_id="n1",
            original_prompt="hello", messages=[],
            suspend_tool_call_id="call-1",
            turn_count=2,
            pending_question="q?", pending_question_id="q-1",
        )
        assert cp.checkpoint_id.startswith("cp-")
        assert len(cp.checkpoint_id) > 5

    def test_default_status_is_suspended(self, sample_checkpoint):
        assert sample_checkpoint.status == "suspended"

    def test_suspended_at_auto_set(self, sample_checkpoint):
        assert sample_checkpoint.suspended_at > 0
        assert sample_checkpoint.suspended_at <= time.time()

    def test_model_dump_json(self, sample_checkpoint):
        json_str = sample_checkpoint.model_dump_json()
        data = json.loads(json_str)
        assert data["task_id"] == "task-abc123"
        assert data["pending_question_id"] == "q-test001"
        assert len(data["messages"]) == 3

    def test_roundtrip(self, sample_checkpoint):
        data = json.loads(sample_checkpoint.model_dump_json())
        cp2 = TaskCheckpoint(**data)
        assert cp2.checkpoint_id == sample_checkpoint.checkpoint_id
        assert cp2.pending_question == sample_checkpoint.pending_question


# ---------------------------------------------------------------------------
# CheckpointStore
# ---------------------------------------------------------------------------

class TestCheckpointStore:
    @pytest.mark.asyncio
    async def test_save_and_get_by_task_id(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: TaskCheckpoint
    ):
        await checkpoint_store.startup_load()
        await checkpoint_store.save(sample_checkpoint)
        result = await checkpoint_store.get_by_task_id(sample_checkpoint.task_id)
        assert result is not None
        assert result.checkpoint_id == sample_checkpoint.checkpoint_id
        assert result.pending_question == sample_checkpoint.pending_question

    @pytest.mark.asyncio
    async def test_save_and_get_by_question_id(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: TaskCheckpoint
    ):
        await checkpoint_store.startup_load()
        await checkpoint_store.save(sample_checkpoint)
        result = await checkpoint_store.get_by_question_id(sample_checkpoint.pending_question_id)
        assert result is not None
        assert result.task_id == sample_checkpoint.task_id

    @pytest.mark.asyncio
    async def test_update_status(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: TaskCheckpoint
    ):
        await checkpoint_store.startup_load()
        await checkpoint_store.save(sample_checkpoint)
        updated = await checkpoint_store.update_status(
            sample_checkpoint.checkpoint_id, "answered", answer="Use JWT tokens"
        )
        assert updated is not None
        assert updated.status == "answered"
        assert updated.answer == "Use JWT tokens"
        assert updated.answered_at is not None

    @pytest.mark.asyncio
    async def test_list_active_returns_only_suspended(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: TaskCheckpoint
    ):
        await checkpoint_store.startup_load()
        await checkpoint_store.save(sample_checkpoint)
        # Create a second, already-answered checkpoint
        cp2 = sample_checkpoint.model_copy(update={
            "checkpoint_id": f"cp-{uuid.uuid4().hex[:12]}",
            "pending_question_id": "q-other",
            "status": "answered",
        })
        await checkpoint_store.save(cp2)

        active = await checkpoint_store.list_active()
        assert len(active) == 1
        assert active[0].checkpoint_id == sample_checkpoint.checkpoint_id

    @pytest.mark.asyncio
    async def test_delete(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: TaskCheckpoint
    ):
        await checkpoint_store.startup_load()
        await checkpoint_store.save(sample_checkpoint)
        deleted = await checkpoint_store.delete(sample_checkpoint.checkpoint_id)
        assert deleted is True
        result = await checkpoint_store.get_by_task_id(sample_checkpoint.task_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_persistence_survives_reload(
        self, tmp_checkpoint_dir: Path, sample_checkpoint: TaskCheckpoint
    ):
        """Checkpoint saved to disk should be loadable by a fresh store instance."""
        store1 = CheckpointStore(str(tmp_checkpoint_dir), "test-node")
        await store1.startup_load()
        await store1.save(sample_checkpoint)

        # Create fresh store instance — simulate restart
        store2 = CheckpointStore(str(tmp_checkpoint_dir), "test-node")
        count = await store2.startup_load()
        assert count == 1  # 1 active checkpoint loaded

        result = await store2.get_by_task_id(sample_checkpoint.task_id)
        assert result is not None
        assert result.pending_question == sample_checkpoint.pending_question

    @pytest.mark.asyncio
    async def test_startup_load_empty(self, checkpoint_store: CheckpointStore):
        count = await checkpoint_store.startup_load()
        assert count == 0

    @pytest.mark.asyncio
    async def test_timeout_sweep_marks_expired(
        self, tmp_checkpoint_dir: Path, sample_checkpoint: TaskCheckpoint
    ):
        """Sweep should expire checkpoints past their timeout."""
        # Create checkpoint with 1-second timeout, already expired
        expired = sample_checkpoint.model_copy(update={
            "timeout_seconds": 1,
            "suspended_at": time.time() - 10,  # 10s ago, timeout=1s
            "status": "suspended",
        })
        store = CheckpointStore(str(tmp_checkpoint_dir), "test-node")
        await store.startup_load()
        await store.save(expired)

        count = await store._sweep_expired()
        assert count == 1

        result = await store.get_by_checkpoint_id(expired.checkpoint_id)
        assert result is not None
        assert result.status == "timed_out"

    @pytest.mark.asyncio
    async def test_non_expired_not_swept(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: TaskCheckpoint
    ):
        await checkpoint_store.startup_load()
        await checkpoint_store.save(sample_checkpoint)
        count = await checkpoint_store._sweep_expired()
        assert count == 0


# ---------------------------------------------------------------------------
# TaskPool
# ---------------------------------------------------------------------------

class TestTaskPool:
    @pytest.mark.asyncio
    async def test_handle_suspension_saves_checkpoint(
        self,
        task_pool: TaskPool,
        checkpoint_store: CheckpointStore,
    ):
        await checkpoint_store.startup_load()
        exc = TaskSuspendedException(
            question="Which framework?",
            question_id="q-frame-1",
            ask_node="architect-A",
            target_role="",
            timeout_seconds=3600,
            timeout_action="use_assumption",
            assumption="FastAPI",
            suspend_tool_call_id="call-frame-1",
        )
        cp = await task_pool.handle_suspension(
            exc=exc,
            task_id="task-frame-1",
            session_id="sess-frame",
            original_prompt="Build the API",
            messages=[{"role": "user", "content": "Build the API"}],
            turn_count=2,
        )
        assert cp.task_id == "task-frame-1"
        assert cp.pending_question_id == "q-frame-1"
        assert cp.pending_question == "Which framework?"
        assert cp.status == "suspended"

        # Verify persisted
        stored = await checkpoint_store.get_by_task_id("task-frame-1")
        assert stored is not None

    @pytest.mark.asyncio
    async def test_handle_suspension_decrements_active(
        self,
        task_pool: TaskPool,
        checkpoint_store: CheckpointStore,
    ):
        await checkpoint_store.startup_load()
        async with task_pool._lock:
            task_pool._active_count = 2  # simulate 2 active

        exc = TaskSuspendedException(
            question="?", question_id="q-dec", ask_node="", target_role="",
            timeout_seconds=100, timeout_action="use_assumption", assumption="",
            suspend_tool_call_id="call-dec",
        )
        await task_pool.handle_suspension(
            exc=exc, task_id="task-dec", session_id="s",
            original_prompt="p", messages=[], turn_count=1,
        )
        async with task_pool._lock:
            assert task_pool._active_count == 1

    @pytest.mark.asyncio
    async def test_resume_task_not_found(self, task_pool: TaskPool, checkpoint_store):
        await checkpoint_store.startup_load()
        success, msg = await task_pool.resume_task("q-nonexistent", "answer")
        assert success is False
        assert "No suspended task" in msg

    @pytest.mark.asyncio
    async def test_resume_task_wrong_status(
        self,
        task_pool: TaskPool,
        checkpoint_store: CheckpointStore,
        sample_checkpoint: TaskCheckpoint,
    ):
        await checkpoint_store.startup_load()
        answered = sample_checkpoint.model_copy(update={"status": "answered"})
        await checkpoint_store.save(answered)

        success, msg = await task_pool.resume_task(
            sample_checkpoint.pending_question_id, "answer"
        )
        assert success is False
        assert "answered" in msg

    @pytest.mark.asyncio
    async def test_resume_task_no_handler_records_checkpoint(
        self,
        task_pool: TaskPool,
        checkpoint_store: CheckpointStore,
        sample_checkpoint: TaskCheckpoint,
    ):
        """resume_task without intent_handler should still update checkpoint to 'answered'."""
        await checkpoint_store.startup_load()
        await checkpoint_store.save(sample_checkpoint)

        success, msg = await task_pool.resume_task(
            sample_checkpoint.pending_question_id,
            answer="Use JWT",
            intent_handler=None,
        )
        assert success is True
        cp = await checkpoint_store.get_by_question_id(sample_checkpoint.pending_question_id)
        assert cp.status == "answered"
        assert cp.answer == "Use JWT"

    @pytest.mark.asyncio
    async def test_cancel_task(
        self,
        task_pool: TaskPool,
        checkpoint_store: CheckpointStore,
        sample_checkpoint: TaskCheckpoint,
    ):
        await checkpoint_store.startup_load()
        await checkpoint_store.save(sample_checkpoint)

        success, msg = await task_pool.cancel_task(sample_checkpoint.task_id)
        assert success is True

        cp = await checkpoint_store.get_by_task_id(sample_checkpoint.task_id)
        assert cp.status == "timed_out"

    @pytest.mark.asyncio
    async def test_get_status(self, task_pool: TaskPool, checkpoint_store):
        await checkpoint_store.startup_load()
        status = await task_pool.get_status()
        assert "active_tasks" in status
        assert "suspended_tasks" in status
        assert "max_active_tasks" in status
        assert status["max_active_tasks"] == 3

    @pytest.mark.asyncio
    async def test_list_tasks_empty(self, task_pool: TaskPool, checkpoint_store):
        await checkpoint_store.startup_load()
        tasks = await task_pool.list_tasks()
        assert tasks == []

    @pytest.mark.asyncio
    async def test_list_tasks_with_entries(
        self,
        task_pool: TaskPool,
        checkpoint_store: CheckpointStore,
        sample_checkpoint: TaskCheckpoint,
    ):
        await checkpoint_store.startup_load()
        await checkpoint_store.save(sample_checkpoint)
        tasks = await task_pool.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == sample_checkpoint.task_id
        assert tasks[0]["question"] == sample_checkpoint.pending_question
        assert tasks[0]["status"] == "suspended"

    @pytest.mark.asyncio
    async def test_increment_decrement_active(self, task_pool: TaskPool):
        ok = await task_pool.increment_active()
        assert ok is True
        ok = await task_pool.increment_active()
        assert ok is True
        ok = await task_pool.increment_active()
        assert ok is True
        # Now at max (3)
        ok = await task_pool.increment_active()
        assert ok is False
        # Decrement
        await task_pool.decrement_active()
        ok = await task_pool.increment_active()
        assert ok is True


# ---------------------------------------------------------------------------
# IntentHandler suspend_and_ask interception
# ---------------------------------------------------------------------------

class TestIntentHandlerSuspension:
    """Tests for suspend_and_ask detection in IntentHandler._execute_tool_call."""

    def _make_handler(self, task_pool=None, config_override=None):
        from runtime.intent_handler import IntentHandler
        from runtime.config import NodeConfig, TaskPoolConfig, CheckpointStoreConfig, SessionConfig, MemoryConfig, EventBusConfig, SchedulerConfig

        config = NodeConfig(
            node_id="test-gw",
            listen="0.0.0.0:8000",
            intent_max_turns=5,
            task_pool=TaskPoolConfig(enabled=True),
            checkpoint_store=CheckpointStoreConfig(),
            session=SessionConfig(),
            memory=MemoryConfig(),
            event_bus=EventBusConfig(),
            scheduler=SchedulerConfig(),
        )

        mock_router = AsyncMock()
        mock_llm = AsyncMock()
        mock_registry = AsyncMock()
        mock_registry.keys = MagicMock(return_value=[])
        mock_node_registry = MagicMock()
        mock_node_registry.build_capability_tree = MagicMock(return_value={})
        mock_store = AsyncMock()

        # Mock session
        from runtime.conversation_store import Session
        session = Session(
            session_id="sess-test",
            created_at=time.time(),
            last_active=time.time(),
        )
        mock_store.get_or_create = AsyncMock(return_value=session)

        handler = IntentHandler(
            config=config,
            llm_client=mock_llm,
            gateway_router=mock_router,
            node_registry=mock_node_registry,
            action_registry=mock_registry,
            conversation_store=mock_store,
            task_pool=task_pool,
        )
        return handler, session, mock_llm

    @pytest.mark.asyncio
    async def test_suspend_and_ask_raises_exception(self):
        from runtime.intent_handler import IntentHandler
        from runtime.llm_client import ToolCall

        handler, session, mock_llm = self._make_handler()

        tc = ToolCall(
            id="call-suspend-1",
            name="mesh_action",
            arguments={
                "target_node_id": "self",
                "action": "suspend_and_ask",
                "params": {
                    "question": "JWT or sessions?",
                    "ask_node": "architect-A",
                    "timeout_seconds": 3600,
                    "assumption": "JWT",
                }
            }
        )

        with pytest.raises(TaskSuspendedException) as exc_info:
            await handler._execute_tool_call(tc, {})

        exc = exc_info.value
        assert exc.question == "JWT or sessions?"
        assert exc.ask_node == "architect-A"
        assert exc.timeout_seconds == 3600
        assert exc.assumption == "JWT"
        assert exc.suspend_tool_call_id == "call-suspend-1"

    @pytest.mark.asyncio
    async def test_suspend_and_ask_with_node_id_as_target(self):
        """Test that target_node_id = own node_id also triggers suspension."""
        from runtime.llm_client import ToolCall

        handler, _, _ = self._make_handler()

        tc = ToolCall(
            id="call-suspend-2",
            name="mesh_action",
            arguments={
                "target_node_id": "test-gw",   # own node_id
                "action": "suspend_and_ask",
                "params": {"question": "Q?", "ask_node": "", "timeout_seconds": 100}
            }
        )

        with pytest.raises(TaskSuspendedException):
            await handler._execute_tool_call(tc, {})

    @pytest.mark.asyncio
    async def test_handle_returns_suspended_response(
        self, tmp_path: Path
    ):
        """IntentHandler.handle() should return TaskSuspendedResponse on suspension."""
        from runtime.intent_handler import IntentHandler
        from runtime.models import IntentRequest
        from runtime.llm_client import LLMResponse, ToolCall

        # Setup TaskPool + CheckpointStore
        cp_store = CheckpointStore(str(tmp_path / "cp"), "test-gw")
        await cp_store.startup_load()

        mock_jm = AsyncMock()
        mock_jm.update_job = AsyncMock()
        tp = TaskPool(cp_store, mock_jm, "test-gw", event_bus=None)

        handler, session, mock_llm = self._make_handler(task_pool=tp)

        # LLM returns a suspend_and_ask tool call
        suspend_call = ToolCall(
            id="call-s1",
            name="mesh_action",
            arguments={
                "target_node_id": "self",
                "action": "suspend_and_ask",
                "params": {
                    "question": "Which auth strategy?",
                    "ask_node": "architect-A",
                    "timeout_seconds": 3600,
                    "assumption": "JWT",
                }
            }
        )
        mock_llm.chat = AsyncMock(return_value=LLMResponse(content="", model="test-model", tool_calls=[suspend_call], usage={"total_tokens": 100}))

        req = IntentRequest(prompt="Implement auth", session_id="sess-test")
        result = await handler.handle(req, task_id="task-auth-1")

        assert isinstance(result, TaskSuspendedResponse)
        assert result.suspended is True
        assert result.task_id == "task-auth-1"
        assert result.question == "Which auth strategy?"
        assert result.question_id is not None
        assert result.asked_node == "architect-A"

    @pytest.mark.asyncio
    async def test_handle_resume_injects_answer(self, tmp_path: Path):
        """handle_resume() should inject answer and continue the loop."""
        from runtime.llm_client import LLMResponse

        cp_store = CheckpointStore(str(tmp_path / "cp"), "test-gw")
        await cp_store.startup_load()

        mock_jm = AsyncMock()
        tp = TaskPool(cp_store, mock_jm, "test-gw", event_bus=None)
        handler, session, mock_llm = self._make_handler(task_pool=tp)

        # Build a checkpoint with a suspend_and_ask tool call
        checkpoint = TaskCheckpoint(
            task_id="task-resume-1",
            session_id="sess-test",
            node_id="test-gw",
            original_prompt="Build login",
            messages=[
                {"role": "user", "content": "Build login"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-r1",
                        "type": "function",
                        "function": {
                            "name": "mesh_action",
                            "arguments": json.dumps({
                                "target_node_id": "self",
                                "action": "suspend_and_ask",
                                "params": {"question": "JWT or sessions?"},
                            })
                        }
                    }]
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-r1",
                    "name": "mesh_action",
                    "content": json.dumps({"status": "suspended", "question_id": "q-r1"})
                },
            ],
            suspend_tool_call_id="call-r1",
            turn_count=1,
            pending_question="JWT or sessions?",
            pending_question_id="q-r1",
        )

        # LLM returns final answer after seeing the injected answer
        mock_llm.chat = AsyncMock(return_value=LLMResponse(content="I will use JWT as specified.", model="test-model", tool_calls=[], usage={"total_tokens": 50}))

        result = await handler.handle_resume(checkpoint, answer="Use JWT tokens")

        assert result.reply == "I will use JWT as specified."
        assert result.turns >= 1

        # Verify the injected message contains the answer
        injected_msgs = session.messages
        tool_results = [m for m in injected_msgs if m.get("role") == "tool" and m.get("tool_call_id") == "call-r1"]
        assert len(tool_results) == 1
        content = json.loads(tool_results[0]["content"])
        assert content["status"] == "answered"
        assert "JWT" in content["answer"]


# ---------------------------------------------------------------------------
# Server endpoint tests
# ---------------------------------------------------------------------------

class TestTaskEndpoints:
    """Integration tests for /tasks endpoints via TestClient."""

    @pytest.fixture
    def client(self, tmp_path: Path):
        from fastapi.testclient import TestClient
        from runtime.config import NodeConfig, TaskPoolConfig, CheckpointStoreConfig, SessionConfig, MemoryConfig, EventBusConfig, SchedulerConfig
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app

        config = NodeConfig(
            node_id="test-gw",
            listen="0.0.0.0:9000",
            task_pool=TaskPoolConfig(enabled=True, max_active_tasks=3),
            checkpoint_store=CheckpointStoreConfig(
                enabled=True,
                path=str(tmp_path / "checkpoints"),
            ),
            session=SessionConfig(),
            memory=MemoryConfig(),
            event_bus=EventBusConfig(enabled=False),
            scheduler=SchedulerConfig(enabled=False),
        )
        registry = ActionRegistry()
        app = create_app(config=config, registry=registry)
        return TestClient(app)

    def test_get_tasks_returns_empty(self, client):
        resp = client.get("/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data
        assert data["total"] == 0

    def test_get_tasks_has_pool_status(self, client):
        resp = client.get("/tasks")
        data = resp.json()
        assert "active_tasks" in data
        assert "suspended_tasks" in data
        assert "max_active_tasks" in data

    def test_answer_task_not_found(self, client):
        resp = client.post(
            "/tasks/nonexistent-task/answer",
            json={"answer": "Use JWT", "answered_by": "test"},
        )
        assert resp.status_code == 404
        assert "RESUME_FAILED" in resp.json()["error"]

    def test_cancel_task_not_found(self, client):
        resp = client.delete("/tasks/nonexistent-task")
        assert resp.status_code == 404

    def test_get_task_checkpoint_not_found(self, client):
        resp = client.get("/tasks/nonexistent-task/checkpoint")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------

class TestPhase4Config:
    def test_task_pool_config_defaults(self):
        from runtime.config import TaskPoolConfig
        cfg = TaskPoolConfig()
        assert cfg.enabled is True
        assert cfg.max_active_tasks == 3

    def test_checkpoint_store_config_defaults(self):
        from runtime.config import CheckpointStoreConfig
        cfg = CheckpointStoreConfig()
        assert cfg.enabled is True
        assert cfg.default_timeout_seconds == 86400
        assert cfg.sweep_interval_seconds == 3600

    def test_parse_task_pool_from_yaml(self, tmp_path: Path):
        import yaml
        from runtime.config import load_config

        node_yaml = tmp_path / "node.yaml"
        node_yaml.write_text(yaml.dump({
            "node_id": "test-node",
            "listen": "0.0.0.0:8000",
            "task_pool": {
                "enabled": True,
                "max_active_tasks": 5,
            },
            "checkpoint_store": {
                "enabled": True,
                "path": "/tmp/test-checkpoints",
                "default_timeout_seconds": 7200,
                "sweep_interval_seconds": 1800,
            },
        }))
        config = load_config(str(node_yaml))
        assert config.task_pool.enabled is True
        assert config.task_pool.max_active_tasks == 5
        assert config.checkpoint_store.path == "/tmp/test-checkpoints"
        assert config.checkpoint_store.default_timeout_seconds == 7200
        assert config.checkpoint_store.sweep_interval_seconds == 1800

    def test_task_pool_disabled(self, tmp_path: Path):
        import yaml
        from runtime.config import load_config

        node_yaml = tmp_path / "node.yaml"
        node_yaml.write_text(yaml.dump({
            "node_id": "test-node",
            "listen": "0.0.0.0:8000",
            "task_pool": {"enabled": False},
        }))
        config = load_config(str(node_yaml))
        assert config.task_pool.enabled is False


# ---------------------------------------------------------------------------
# TaskSuspendedResponse model
# ---------------------------------------------------------------------------

class TestTaskSuspendedResponse:
    def test_model_fields(self):
        resp = TaskSuspendedResponse(
            session_id="sess-1",
            task_id="task-1",
            question="Which DB?",
            question_id="q-1",
            asked_node="architect-A",
            timeout_seconds=3600,
            assumption="PostgreSQL",
        )
        assert resp.suspended is True
        assert resp.session_id == "sess-1"
        assert resp.task_id == "task-1"
        assert resp.question == "Which DB?"
        assert "message" in resp.model_dump()

    def test_model_dump(self):
        resp = TaskSuspendedResponse(
            session_id="s", task_id="t",
            question="?", question_id="q",
            timeout_seconds=100, assumption="",
        )
        data = resp.model_dump()
        assert data["suspended"] is True
        assert data["task_id"] == "t"


# ---------------------------------------------------------------------------
# Full integration: suspend → answer → resume cycle
# ---------------------------------------------------------------------------

class TestSuspendResumeIntegration:
    @pytest.mark.asyncio
    async def test_full_suspend_resume_cycle(self, tmp_path: Path):
        """End-to-end: task suspends, answer injected, task resumes."""
        from runtime.llm_client import LLMResponse, ToolCall

        cp_store = CheckpointStore(str(tmp_path / "cp"), "dev-node")
        await cp_store.startup_load()

        mock_jm = AsyncMock()
        tp = TaskPool(cp_store, mock_jm, "dev-node", event_bus=None)

        from runtime.config import NodeConfig, TaskPoolConfig, CheckpointStoreConfig, SessionConfig, MemoryConfig, EventBusConfig, SchedulerConfig
        from runtime.intent_handler import IntentHandler
        from runtime.models import IntentRequest
        from runtime.conversation_store import Session

        config = NodeConfig(
            node_id="dev-node",
            listen="0.0.0.0:8001",
            intent_max_turns=10,
            task_pool=TaskPoolConfig(),
            checkpoint_store=CheckpointStoreConfig(),
            session=SessionConfig(),
            memory=MemoryConfig(),
            event_bus=EventBusConfig(),
            scheduler=SchedulerConfig(),
        )

        mock_router = AsyncMock()
        mock_llm = AsyncMock()
        mock_registry = AsyncMock()
        mock_registry.keys = MagicMock(return_value=[])
        mock_node_registry = MagicMock()
        mock_node_registry.build_capability_tree = MagicMock(return_value={})
        mock_store = AsyncMock()

        session = Session("sess-e2e", time.time(), time.time())
        mock_store.get_or_create = AsyncMock(return_value=session)
        mock_store.get = AsyncMock(return_value=session)

        handler = IntentHandler(
            config=config, llm_client=mock_llm, gateway_router=mock_router,
            node_registry=mock_node_registry, action_registry=mock_registry,
            conversation_store=mock_store, task_pool=tp,
        )

        # Phase 1: LLM suspends the task
        suspend_tc = ToolCall(
            id="call-e2e",
            name="mesh_action",
            arguments={
                "target_node_id": "self",
                "action": "suspend_and_ask",
                "params": {
                    "question": "Should we use PostgreSQL or MySQL?",
                    "ask_node": "architect-A",
                    "timeout_seconds": 3600,
                    "assumption": "PostgreSQL",
                }
            }
        )
        mock_llm.chat = AsyncMock(return_value=LLMResponse(content="", model="test-model", tool_calls=[suspend_tc], usage={"total_tokens": 50}))

        req = IntentRequest(prompt="Set up the database", session_id="sess-e2e")
        result = await handler.handle(req, task_id="task-e2e")

        assert isinstance(result, TaskSuspendedResponse)
        question_id = result.question_id

        # Verify checkpoint saved
        cp = await cp_store.get_by_question_id(question_id)
        assert cp is not None
        assert cp.status == "suspended"

        # Phase 2: Answer arrives
        mock_llm.chat = AsyncMock(return_value=LLMResponse(content="Database setup complete using PostgreSQL as specified.", model="test-model", tool_calls=[], usage={"total_tokens": 75}))

        success, msg = await tp.resume_task(
            question_id=question_id,
            answer="Use PostgreSQL",
            intent_handler=handler,
        )
        assert success is True

        # Wait briefly for background task
        await asyncio.sleep(0.1)

        # Verify checkpoint updated
        cp_final = await cp_store.get_by_question_id(question_id)
        assert cp_final.status in ("answered", "resumed")
        assert cp_final.answer == "Use PostgreSQL"
