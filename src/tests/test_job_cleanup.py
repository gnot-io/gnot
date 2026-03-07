"""Tests for job TTL cleanup in runtime.job_manager."""

from __future__ import annotations

import asyncio
import time

import pytest

from runtime.job_manager import JobManager, JobStatus


@pytest.fixture
def short_ttl_manager() -> JobManager:
    """Manager with a 1-second TTL for fast testing."""
    return JobManager(job_ttl_seconds=1, cleanup_interval_seconds=60)


@pytest.mark.asyncio
async def test_cleanup_removes_expired_completed(short_ttl_manager: JobManager) -> None:
    """Completed jobs older than TTL are cleaned up."""
    job = await short_ttl_manager.create_job("node-0", "task-001")
    await short_ttl_manager.update_job(
        job.job_id, status=JobStatus.COMPLETED, output={"ok": True}
    )
    # Force the completed_time to be old
    stored = await short_ttl_manager.get_job(job.job_id)
    assert stored is not None
    stored.completed_time = time.time() - 10  # 10 seconds ago

    removed = await short_ttl_manager.cleanup_expired()
    assert removed == 1
    assert await short_ttl_manager.get_job(job.job_id) is None


@pytest.mark.asyncio
async def test_cleanup_removes_expired_failed(short_ttl_manager: JobManager) -> None:
    """Failed jobs older than TTL are cleaned up."""
    job = await short_ttl_manager.create_job("node-0", "task-002")
    await short_ttl_manager.update_job(
        job.job_id, status=JobStatus.FAILED, error="boom"
    )
    stored = await short_ttl_manager.get_job(job.job_id)
    assert stored is not None
    stored.completed_time = time.time() - 10

    removed = await short_ttl_manager.cleanup_expired()
    assert removed == 1


@pytest.mark.asyncio
async def test_cleanup_keeps_running_jobs(short_ttl_manager: JobManager) -> None:
    """Running jobs are never cleaned up regardless of age."""
    job = await short_ttl_manager.create_job("node-0", "task-003")
    await short_ttl_manager.update_job(job.job_id, status=JobStatus.RUNNING)

    removed = await short_ttl_manager.cleanup_expired()
    assert removed == 0
    assert await short_ttl_manager.get_job(job.job_id) is not None


@pytest.mark.asyncio
async def test_cleanup_keeps_recent_completed(short_ttl_manager: JobManager) -> None:
    """Recently completed jobs within TTL are kept."""
    # Override TTL to large value
    manager = JobManager(job_ttl_seconds=3600)
    job = await manager.create_job("node-0", "task-004")
    await manager.update_job(
        job.job_id, status=JobStatus.COMPLETED, output={"ok": True}
    )

    removed = await manager.cleanup_expired()
    assert removed == 0
    assert await manager.get_job(job.job_id) is not None


@pytest.mark.asyncio
async def test_cleanup_mixed_jobs() -> None:
    """Mixed batch: only expired terminal jobs are removed."""
    manager = JobManager(job_ttl_seconds=1)

    # Job 1: completed, old → should be removed
    j1 = await manager.create_job("node-0", "t1")
    await manager.update_job(j1.job_id, status=JobStatus.COMPLETED, output={})
    s1 = await manager.get_job(j1.job_id)
    s1.completed_time = time.time() - 10  # type: ignore[union-attr]

    # Job 2: running → should be kept
    j2 = await manager.create_job("node-0", "t2")
    await manager.update_job(j2.job_id, status=JobStatus.RUNNING)

    # Job 3: accepted → should be kept
    j3 = await manager.create_job("node-0", "t3")

    removed = await manager.cleanup_expired()
    assert removed == 1
    assert await manager.get_job(j1.job_id) is None
    assert await manager.get_job(j2.job_id) is not None
    assert await manager.get_job(j3.job_id) is not None


@pytest.mark.asyncio
async def test_total_count() -> None:
    """total_count returns all tracked jobs."""
    manager = JobManager()
    await manager.create_job("n", "t1")
    await manager.create_job("n", "t2")
    assert await manager.total_count() == 2


@pytest.mark.asyncio
async def test_cleanup_loop_starts_and_stops() -> None:
    """Cleanup loop can be started and stopped without errors."""
    manager = JobManager(cleanup_interval_seconds=1)
    manager.start_cleanup_loop()
    await asyncio.sleep(0.1)
    manager.stop_cleanup_loop()
    # No exception = success
