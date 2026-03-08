"""In-memory job state management with TTL-based cleanup.

Tracks async job lifecycle from acceptance through completion or failure.
All access is protected by an asyncio.Lock for safe concurrent use.
A background task periodically purges expired terminal jobs.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from runtime.models import JobStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_JOB_TTL_SECONDS: int = 3600
DEFAULT_CLEANUP_INTERVAL_SECONDS: int = 60


# ---------------------------------------------------------------------------
# Job state dataclass
# ---------------------------------------------------------------------------

@dataclass
class JobState:
    """Mutable state for a single async job."""

    job_id: str
    task_id: str
    status: JobStatus = JobStatus.ACCEPTED
    progress: int = 0
    start_time: float = field(default_factory=time.time)
    completed_time: float | None = None
    estimated_completion_seconds: int = 60
    output: dict[str, Any] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Job Manager
# ---------------------------------------------------------------------------

class JobManager:
    """Thread-safe in-memory store for async job state.

    Each node maintains its own JobManager instance.
    Includes TTL-based cleanup for completed/failed jobs.
    """

    def __init__(
        self,
        job_ttl_seconds: int = DEFAULT_JOB_TTL_SECONDS,
        cleanup_interval_seconds: int = DEFAULT_CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._job_ttl_seconds = job_ttl_seconds
        self._cleanup_interval_seconds = cleanup_interval_seconds
        self._cleanup_task: asyncio.Task[None] | None = None

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def generate_job_id(node_id: str) -> str:
        """Generate a unique job ID scoped to a node.

        Format: ``{node_id}-job-{short_uuid}``
        """
        short = uuid.uuid4().hex[:8]
        return f"{node_id}-job-{short}"

    # -- lifecycle ----------------------------------------------------------

    def start_cleanup_loop(self) -> None:
        """Start the background cleanup task.

        Should be called once after the event loop is running
        (e.g., in a FastAPI startup event).
        """
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info(
                "Job cleanup loop started — ttl=%ds, interval=%ds",
                self._job_ttl_seconds,
                self._cleanup_interval_seconds,
            )

    def stop_cleanup_loop(self) -> None:
        """Cancel the background cleanup task."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            logger.info("Job cleanup loop stopped")

    async def _cleanup_loop(self) -> None:
        """Periodically remove expired terminal jobs."""
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval_seconds)
                removed = await self.cleanup_expired()
                if removed > 0:
                    logger.info("Cleaned up %d expired job(s)", removed)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in cleanup loop")

    async def cleanup_expired(self) -> int:
        """Remove terminal jobs older than the TTL.

        Returns:
            Number of jobs removed.
        """
        now = time.time()
        to_remove: list[str] = []

        async with self._lock:
            for job_id, job in self._jobs.items():
                # v6.0 Phase 4: TIMED_OUT is also a terminal state
                if job.status not in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.TIMED_OUT):
                    continue
                completed_at = job.completed_time or job.start_time
                if now - completed_at > self._job_ttl_seconds:
                    to_remove.append(job_id)

            for job_id in to_remove:
                del self._jobs[job_id]

        return len(to_remove)

    # -- public API ---------------------------------------------------------

    async def create_job(
        self,
        node_id: str,
        task_id: str,
        estimated_completion_seconds: int = 60,
    ) -> JobState:
        """Create and register a new job.

        Args:
            node_id: The node that owns this job.
            task_id: The global workflow task ID.
            estimated_completion_seconds: Hint for the poller.

        Returns:
            The newly created JobState.
        """
        job_id = self.generate_job_id(node_id)
        job = JobState(
            job_id=job_id,
            task_id=task_id,
            estimated_completion_seconds=estimated_completion_seconds,
        )
        async with self._lock:
            self._jobs[job_id] = job
        logger.info("Job created: %s (task=%s)", job_id, task_id)
        return job

    async def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        progress: int | None = None,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> JobState | None:
        """Update fields on an existing job.

        Args:
            job_id: The job to update.
            status: New status (if changing).
            progress: New progress percentage.
            output: Result payload on completion.
            error: Error message on failure.

        Returns:
            The updated JobState, or None if not found.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                logger.warning("Attempted to update unknown job: %s", job_id)
                return None
            if status is not None:
                job.status = status
                # Track completion time for TTL cleanup
                if status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.TIMED_OUT):
                    job.completed_time = time.time()
            if progress is not None:
                job.progress = progress
            if output is not None:
                job.output = output
            if error is not None:
                job.error = error
        logger.debug("Job updated: %s → status=%s", job_id, job.status)
        return job

    async def get_job(self, job_id: str) -> JobState | None:
        """Retrieve the current state of a job.

        Args:
            job_id: The job to look up.

        Returns:
            The JobState or None if not found.
        """
        async with self._lock:
            return self._jobs.get(job_id)

    async def active_count(self) -> int:
        """Return the number of non-terminal jobs."""
        async with self._lock:
            return sum(
                1
                for j in self._jobs.values()
                if j.status in (JobStatus.ACCEPTED, JobStatus.RUNNING)
            )

    async def total_count(self) -> int:
        """Return the total number of tracked jobs."""
        async with self._lock:
            return len(self._jobs)
