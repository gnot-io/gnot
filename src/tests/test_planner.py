"""Tests for the Cloud AI Planner — ReAct loop and helpers."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from planner.config import PlannerConfig, NodeEntry
from planner.mesh_client import MeshClient, MeshError, JobFailedError
from planner.planner import CloudPlanner
from planner.skills_cache import SkillsCache


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_config(**kwargs):
    defaults = dict(
        gateway_url="http://localhost:8080",
        gateway_token="test-token",
        llm_api_key="sk-test",
        llm_model="gpt-4o",
        max_iterations=10,
        poll_interval_seconds=0.01,
        poll_max_wait_seconds=5.0,
        nodes=[NodeEntry("node-0", "http://localhost:8080"),
               NodeEntry("node-1", "http://10.0.0.1:8080")],
    )
    defaults.update(kwargs)
    return PlannerConfig(**defaults)


def make_planner(llm_responses: list[str], action_outputs: list[dict] | None = None):
    """Build a CloudPlanner with mocked LLM and MeshClient."""
    config = make_config()
    mesh = MagicMock(spec=MeshClient)

    outputs = iter(action_outputs or [{}])
    mesh.execute = AsyncMock(side_effect=lambda *a, **kw: next(outputs))

    skills = MagicMock(spec=SkillsCache)
    skills.build_prompt_section.return_value = "# node-0 skills\n..."
    skills.known_node_ids.return_value = ["node-0", "node-1"]

    planner = CloudPlanner(config, mesh, skills)

    llm_iter = iter(llm_responses)
    planner._call_llm = AsyncMock(side_effect=lambda sys, msgs: next(llm_iter))

    return planner, mesh


# ── SkillsCache ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skills_cache_fetches_from_address():
    config = make_config()
    cache = SkillsCache(config)

    import httpx
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "# Node skills"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("planner.skills_cache.httpx.AsyncClient", return_value=mock_client):
        await cache.load_all()

    assert "node-0" in cache.known_node_ids()


@pytest.mark.asyncio
async def test_skills_cache_falls_back_to_manual_text():
    config = make_config(nodes=[
        NodeEntry("node-3", skills_text="# Manual skills for node-3"),
    ])
    cache = SkillsCache(config)
    await cache.load_all()
    assert "node-3" in cache.known_node_ids()
    assert "Manual skills" in cache.build_prompt_section()


@pytest.mark.asyncio
async def test_skills_cache_skips_unreachable_no_fallback():
    config = make_config(nodes=[
        NodeEntry("node-99", address="http://10.255.255.255:8080"),
    ])
    cache = SkillsCache(config)
    import httpx
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("planner.skills_cache.httpx.AsyncClient", return_value=mock_client):
        await cache.load_all()
    assert "node-99" not in cache.known_node_ids()


# ── CloudPlanner — ReAct loop ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_planner_done_on_first_response():
    """LLM immediately signals done → PlannerResult.success=True."""
    done_resp = json.dumps({
        "thought": "Nothing to do.",
        "done": True,
        "summary": "Task already complete.",
    })
    planner, mesh = make_planner([done_resp])
    result = await planner.run("check status")

    assert result.success is True
    assert result.iterations == 1
    assert "already complete" in result.summary
    mesh.execute.assert_not_called()


@pytest.mark.asyncio
async def test_planner_single_action_then_done():
    """LLM calls one action then signals done."""
    action_resp = json.dumps({
        "thought": "Run backup.",
        "action": {
            "target_node_id": "node-1",
            "action": "execute_command",
            "params": {"command": "mysqldump -u root mydb > /tmp/backup.sql"},
        },
    })
    done_resp = json.dumps({
        "thought": "Backup done.",
        "done": True,
        "summary": "Database backed up successfully.",
    })
    planner, mesh = make_planner(
        [action_resp, done_resp],
        action_outputs=[{"exit_code": 0, "stdout": "", "stderr": ""}],
    )
    result = await planner.run("backup mydb on node-1")

    assert result.success is True
    assert result.iterations == 2
    mesh.execute.assert_called_once_with(
        "node-1", "execute_command",
        {"command": "mysqldump -u root mydb > /tmp/backup.sql"},
    )


@pytest.mark.asyncio
async def test_planner_multi_step_workflow():
    """3-step: backup → transfer → restore."""
    responses = [
        json.dumps({"thought": "Step 1", "action": {"target_node_id": "node-1", "action": "execute_command", "params": {"command": "mysqldump ..."}}}),
        json.dumps({"thought": "Step 2", "action": {"target_node_id": "node-1", "action": "read_file_b64", "params": {"path": "/tmp/backup.sql"}}}),
        json.dumps({"thought": "Step 3", "action": {"target_node_id": "node-0", "action": "write_file_b64", "params": {"path": "/backups/backup.sql", "content_b64": "abc"}}}),
        json.dumps({"thought": "Done", "done": True, "summary": "Backup, transfer, restore complete."}),
    ]
    outputs = [
        {"exit_code": 0, "stdout": ""},
        {"content_b64": "abc123", "size_bytes": 100},
        {"success": True, "size_bytes": 100},
    ]
    planner, mesh = make_planner(responses, outputs)
    result = await planner.run("backup db on node-1, transfer to node-0")

    assert result.success is True
    assert result.iterations == 4
    assert mesh.execute.call_count == 3


@pytest.mark.asyncio
async def test_planner_error_signal():
    """LLM signals error → PlannerResult.success=False."""
    error_resp = json.dumps({
        "thought": "Disk full.",
        "error": True,
        "message": "No space left on device.",
    })
    planner, _ = make_planner([error_resp])
    result = await planner.run("backup db")

    assert result.success is False
    assert "No space left" in result.summary


@pytest.mark.asyncio
async def test_planner_action_failure_feeds_back():
    """Job failure is fed back to LLM as OBSERVATION, loop continues."""
    action_resp = json.dumps({
        "thought": "Try backup.",
        "action": {"target_node_id": "node-1", "action": "execute_command", "params": {"command": "bad_cmd"}},
    })
    error_resp = json.dumps({
        "thought": "Command failed, nothing I can do.",
        "error": True,
        "message": "backup command not found",
    })
    planner, mesh = make_planner([action_resp, error_resp])
    mesh.execute = AsyncMock(side_effect=JobFailedError("exit_code 127"))

    result = await planner.run("backup db")
    assert result.success is False
    # LLM was called twice: once for action, once after seeing the failure
    assert planner._call_llm.await_count == 2


@pytest.mark.asyncio
async def test_planner_invalid_json_from_llm_continues():
    """LLM returning non-JSON is handled gracefully — loop continues."""
    bad_resp = "Sorry, I cannot help with that."
    done_resp = json.dumps({"thought": "Recovered.", "done": True, "summary": "Done after recovery."})
    planner, _ = make_planner([bad_resp, done_resp])
    result = await planner.run("backup db")

    assert result.success is True
    assert result.iterations == 2


@pytest.mark.asyncio
async def test_planner_max_iterations():
    """Hitting max_iterations returns failure."""
    # Always return an action, never done
    action = json.dumps({
        "thought": "Keep trying.",
        "action": {"target_node_id": "node-0", "action": "execute_command", "params": {"command": "echo hi"}},
    })
    config = make_config(max_iterations=3)
    mesh = MagicMock(spec=MeshClient)
    mesh.execute = AsyncMock(return_value={"exit_code": 0})
    skills = MagicMock(spec=SkillsCache)
    skills.build_prompt_section.return_value = "skills"

    planner = CloudPlanner(config, mesh, skills)
    planner._call_llm = AsyncMock(return_value=action)

    result = await planner.run("infinite task")
    assert result.success is False
    assert result.iterations == 3
    assert "Max iterations" in result.summary


# ── MeshClient ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mesh_client_sync_action():
    config = make_config()
    client = MeshClient(config)

    sync_response = {
        "task_id": "t1", "status": "completed",
        "output": {"exit_code": 0, "stdout": "ok", "stderr": ""},
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = sync_response

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    mock_http.post = AsyncMock(return_value=mock_resp)

    with patch("planner.mesh_client.httpx.AsyncClient", return_value=mock_http):
        output = await client.execute("node-0", "execute_command", {"command": "echo hi"})

    assert output["exit_code"] == 0


@pytest.mark.asyncio
async def test_mesh_client_error_response_raises():
    config = make_config()
    client = MeshClient(config)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"error": "UNTRUSTED_NODE: node-99"}

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    mock_http.post = AsyncMock(return_value=mock_resp)

    with patch("planner.mesh_client.httpx.AsyncClient", return_value=mock_http):
        with pytest.raises(MeshError, match="UNTRUSTED_NODE"):
            await client.execute("node-99", "echo", {})
