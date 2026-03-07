"""Tests for runtime.job_manager."""

from __future__ import annotations

import pytest
import pytest_asyncio

from runtime.job_manager import JobManager, JobStatus


@pytest.fixture
def manager() -> JobManager:
    return JobManager()


@pytest.mark.asyncio
async def test_create_job(manager: JobManager) -> None:
    job = await manager.create_job("node-0", "task-001")
    assert job.job_id.startswith("node-0-job-")
    assert job.task_id == "task-001"
    assert job.status == JobStatus.ACCEPTED


@pytest.mark.asyncio
async def test_get_job(manager: JobManager) -> None:
    job = await manager.create_job("node-0", "task-002")
    fetched = await manager.get_job(job.job_id)
    assert fetched is not None
    assert fetched.job_id == job.job_id


@pytest.mark.asyncio
async def test_get_job_not_found(manager: JobManager) -> None:
    result = await manager.get_job("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_update_job_status(manager: JobManager) -> None:
    job = await manager.create_job("node-0", "task-003")
    updated = await manager.update_job(job.job_id, status=JobStatus.RUNNING, progress=50)
    assert updated is not None
    assert updated.status == JobStatus.RUNNING
    assert updated.progress == 50


@pytest.mark.asyncio
async def test_update_job_completed(manager: JobManager) -> None:
    job = await manager.create_job("node-0", "task-004")
    output = {"exit_code": 0, "stdout": "ok"}
    await manager.update_job(job.job_id, status=JobStatus.COMPLETED, output=output)
    fetched = await manager.get_job(job.job_id)
    assert fetched is not None
    assert fetched.status == JobStatus.COMPLETED
    assert fetched.output == output


@pytest.mark.asyncio
async def test_update_nonexistent_job(manager: JobManager) -> None:
    result = await manager.update_job("fake-id", status=JobStatus.FAILED)
    assert result is None


@pytest.mark.asyncio
async def test_active_count(manager: JobManager) -> None:
    await manager.create_job("node-0", "t1")
    await manager.create_job("node-0", "t2")
    j3 = await manager.create_job("node-0", "t3")
    await manager.update_job(j3.job_id, status=JobStatus.COMPLETED)
    count = await manager.active_count()
    assert count == 2


def test_job_id_format() -> None:
    jid = JobManager.generate_job_id("node-5")
    assert jid.startswith("node-5-job-")
    assert len(jid.split("-")) >= 4
