"""Tests for JobQueue (v5.3)."""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.job_queue import JobQueue
from runtime.models import JobMode


@pytest.fixture
def queue():
    return JobQueue()


@pytest.mark.asyncio
async def test_enqueue_and_poll(queue):
    job = await queue.enqueue("node-1", "task-001", "read_file", {"path": "/tmp/x"})
    jobs = await queue.poll("node-1")
    assert len(jobs) == 1
    assert jobs[0].job_id == job.job_id
    assert jobs[0].action == "read_file"


@pytest.mark.asyncio
async def test_poll_empty_returns_empty(queue):
    jobs = await queue.poll("node-99")
    assert jobs == []


@pytest.mark.asyncio
async def test_claim_succeeds(queue):
    job = await queue.enqueue("node-1", "task-001", "write_file", {"path": "/tmp/x", "content": "hi"})
    claimed = await queue.claim(job.job_id, "node-1")
    assert claimed is not None
    assert claimed.job_id == job.job_id
    assert claimed.claimed is True


@pytest.mark.asyncio
async def test_claim_twice_returns_none(queue):
    job = await queue.enqueue("node-1", "task-001", "write_file", {})
    await queue.claim(job.job_id, "node-1")
    result = await queue.claim(job.job_id, "node-1")
    assert result is None


@pytest.mark.asyncio
async def test_claimed_job_not_in_poll(queue):
    job = await queue.enqueue("node-1", "task-001", "write_file", {})
    await queue.claim(job.job_id, "node-1")
    pending = await queue.poll("node-1")
    assert len(pending) == 0


@pytest.mark.asyncio
async def test_claim_wrong_node_returns_none(queue):
    job = await queue.enqueue("node-1", "task-001", "write_file", {})
    result = await queue.claim(job.job_id, "node-2")
    assert result is None


@pytest.mark.asyncio
async def test_register_push_job(queue):
    await queue.register_push_job("ext-job-001", "task-001", "node-1", "http://10.0.0.1:8080")
    route = await queue.get_route("ext-job-001")
    assert route is not None
    assert route.mode == JobMode.PUSH
    assert route.worker_address == "http://10.0.0.1:8080"


@pytest.mark.asyncio
async def test_get_route_pull_job(queue):
    job = await queue.enqueue("node-1", "task-001", "read_file", {})
    route = await queue.get_route(job.job_id)
    assert route is not None
    assert route.mode == JobMode.PULL


@pytest.mark.asyncio
async def test_queue_depth(queue):
    await queue.enqueue("node-1", "task-001", "read_file", {})
    await queue.enqueue("node-1", "task-002", "write_file", {})
    assert await queue.queue_depth("node-1") == 2


@pytest.mark.asyncio
async def test_remove_from_queue(queue):
    job = await queue.enqueue("node-1", "task-001", "read_file", {})
    await queue.remove_from_queue(job.job_id, "node-1")
    jobs = await queue.poll("node-1")
    assert len(jobs) == 0
