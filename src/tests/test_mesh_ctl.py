"""Tests for mesh_ctl.py — gateway control tool."""
import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mesh_ctl import cmd_run, cmd_result, cmd_nodes, cmd_health


def mock_post(response: dict):
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=None)
    resp = MagicMock()
    resp.json.return_value = response
    m.post = MagicMock(return_value=resp)
    return m


def mock_get(response: dict):
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=None)
    resp = MagicMock()
    resp.json.return_value = response
    m.get = MagicMock(return_value=resp)
    return m


# ── cmd_run ──────────────────────────────────────────────────────────────────

def test_run_sync_action_returns_output():
    sync_resp = {"status": "completed", "output": {"exit_code": 0, "stdout": "ok"}}
    with patch("mesh_ctl.httpx.Client", return_value=mock_post(sync_resp)):
        result = cmd_run("http://gw", "tok", "node-1", "execute_command",
                         '{"command":"echo hi"}', wait=False)
    assert result["output"]["exit_code"] == 0


def test_run_gateway_error_returned():
    with patch("mesh_ctl.httpx.Client", return_value=mock_post({"error": "UNTRUSTED_NODE"})):
        result = cmd_run("http://gw", "tok", "node-99", "echo", "{}", wait=False)
    assert "error" in result
    assert "UNTRUSTED_NODE" in result["error"]


def test_run_no_wait_returns_job_id():
    async_resp = {"job_id": "node1-job-abc", "status": "accepted",
                  "task_id": "t1", "estimated_completion_seconds": 30}
    with patch("mesh_ctl.httpx.Client", return_value=mock_post(async_resp)):
        result = cmd_run("http://gw", "tok", "node-1", "execute_command",
                         '{"command":"long task"}', wait=False)
    assert result["job_id"] == "node1-job-abc"
    assert "hint" in result


def test_run_invalid_params_json():
    result = cmd_run("http://gw", "tok", "node-1", "echo", "NOT_JSON", wait=False)
    assert "error" in result
    assert "Invalid JSON" in result["error"]


# ── cmd_result ───────────────────────────────────────────────────────────────

def test_result_completed():
    poll_resp = {"status": "completed", "job_id": "j1", "task_id": "t1",
                 "output": {"exit_code": 0}}
    with patch("mesh_ctl.httpx.Client", return_value=mock_get(poll_resp)):
        result = cmd_result("http://gw", "tok", "j1")
    assert result["status"] == "completed"
    assert result["output"]["exit_code"] == 0


def test_result_failed():
    fail_resp = {"status": "failed", "job_id": "j1", "task_id": "t1",
                 "error": "command not found"}
    with patch("mesh_ctl.httpx.Client", return_value=mock_get(fail_resp)):
        result = cmd_result("http://gw", "tok", "j1")
    assert result["status"] == "failed"
    assert "command not found" in result["error"]


def test_result_timeout(monkeypatch):
    import mesh_ctl
    monkeypatch.setattr(mesh_ctl, "POLL_TIMEOUT", 0.01)
    monkeypatch.setattr(mesh_ctl, "POLL_INTERVAL", 0.001)
    running = {"status": "running", "job_id": "j1", "task_id": "t1"}
    with patch("mesh_ctl.httpx.Client", return_value=mock_get(running)):
        result = cmd_result("http://gw", "tok", "j1")
    assert "Timeout" in result["error"]


# ── cmd_nodes ────────────────────────────────────────────────────────────────

def test_nodes_returns_list():
    nodes_resp = {"nodes": [
        {"node_id": "node-0", "status": "online", "address": None, "last_heartbeat": None},
        {"node_id": "node-1", "status": "online", "address": "http://10.0.0.1:8080", "last_heartbeat": 1234567890.0},
    ]}
    with patch("mesh_ctl.httpx.Client", return_value=mock_get(nodes_resp)):
        result = cmd_nodes("http://gw", "tok")
    assert len(result["nodes"]) == 2
    assert result["nodes"][1]["node_id"] == "node-1"


# ── cmd_health ───────────────────────────────────────────────────────────────

def test_health_returns_node_info():
    health_resp = {"node_id": "node-0", "status": "healthy", "uptime_seconds": 3600.0,
                   "actions_loaded": 5, "jobs_active": 0}
    with patch("mesh_ctl.httpx.Client", return_value=mock_get(health_resp)):
        result = cmd_health("http://gw", "tok")
    assert result["node_id"] == "node-0"
    assert result["status"] == "healthy"
