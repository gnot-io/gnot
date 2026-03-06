"""Per-node job queue — gateway-side store for pull-mode jobs.

When the gateway cannot reach a worker (pull mode), it enqueues the job here
instead of proxying. The worker then polls, claims, executes, and reports back.

Also tracks push-mode jobs so that GET /result/{job_id} can be proxied to the
correct worker node.

Thread-safety: asyncio.Lock protects all mutations.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from runtime.models import JobMode, QueuedJob

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal tracking record
# ---------------------------------------------------------------------------

@dataclass
class _JobRouteEntry:
    """Tracks where a job lives so GET /result can be answered correctly."""
    job_id: str
    task_id: str
    target_node_id: str
    mode: JobMode
    worker_address: str | None = None   # set for push-mode jobs (proxy result there)


# ---------------------------------------------------------------------------
# Job Queue
# ---------------------------------------------------------------------------

class JobQueue:
    """Gateway-side queue and routing table for worker jobs.

    Responsibilities:
      - Enqueue jobs for offline workers (pull mode)
      - Let workers poll and claim their jobs
      - Track all job→node mappings (both push and pull) so the gateway
        can answer GET /result/{job_id} for LLM callers
    """

    def __init__(self) -> None:
        # pull-mode queue: node_id → list of pending QueuedJob
        self._queues: dict[str, list[QueuedJob]] = defaultdict(list)
        # routing table: job_id → _JobRouteEntry (covers BOTH push and pull)
        self._routes: dict[str, _JobRouteEntry] = {}
        self._lock = asyncio.Lock()

    # -- pull-mode enqueue --------------------------------------------------

    async def enqueue(
        self,
        node_id: str,
        task_id: str,
        action: str,
        params: dict[str, Any],
        job_id: str | None = None,
        timeout_seconds: int = 300,
        caller_token: str | None = None,
        caller_credentials: dict[str, Any] | None = None,
    ) -> QueuedJob:
        """Create a queued job for a worker to pull.

        Args:
            node_id:          Target worker node.
            task_id:          Global workflow task ID.
            action:           Action name.
            params:           Action params.
            job_id:           If provided, use this job_id (caller already created one).
            timeout_seconds:  v5.7 — fail the job if unclaimed after this many seconds.

        Returns:
            The created QueuedJob.
        """
        jid = job_id or _generate_job_id(node_id)
        job = QueuedJob(
            job_id=jid,
            task_id=task_id,
            target_node_id=node_id,
            action=action,
            params=params,
            created_at=time.time(),
            timeout_seconds=timeout_seconds,
            caller_token=caller_token,
            caller_credentials=caller_credentials or {},
        )
        async with self._lock:
            self._queues[node_id].append(job)
            self._routes[jid] = _JobRouteEntry(
                job_id=jid,
                task_id=task_id,
                target_node_id=node_id,
                mode=JobMode.PULL,
            )
        logger.info("Enqueued pull job %s for node %s (action=%s)", jid, node_id, action)
        return job

    # -- push-mode registration --------------------------------------------

    async def register_push_job(
        self,
        job_id: str,
        task_id: str,
        target_node_id: str,
        worker_address: str,
    ) -> None:
        """Record a push-mode job so its result can be proxied.

        Called after the gateway successfully proxied an action to a reachable
        worker and received a job_id back. Subsequent GET /result/{job_id} calls
        will be forwarded to worker_address.
        """
        async with self._lock:
            self._routes[job_id] = _JobRouteEntry(
                job_id=job_id,
                task_id=task_id,
                target_node_id=target_node_id,
                mode=JobMode.PUSH,
                worker_address=worker_address,
            )
        logger.info(
            "Registered push job %s → node %s @ %s",
            job_id, target_node_id, worker_address,
        )

    # -- worker poll / claim -----------------------------------------------

    async def poll(self, node_id: str) -> list[QueuedJob]:
        """Return all unclaimed jobs waiting for node_id.

        Does NOT claim them — worker must call claim() separately.
        """
        async with self._lock:
            pending = [j for j in self._queues.get(node_id, []) if not j.claimed]
        logger.debug("Poll from %s — %d pending job(s)", node_id, len(pending))
        return pending

    async def claim(self, job_id: str, node_id: str) -> QueuedJob | None:
        """Mark a job as claimed by the calling worker.

        Returns the QueuedJob if the claim succeeded, None if:
          - job not found
          - job doesn't belong to this node
          - job already claimed
        """
        async with self._lock:
            queue = self._queues.get(node_id, [])
            for job in queue:
                if job.job_id == job_id:
                    if job.claimed:
                        logger.warning("Job %s already claimed (node=%s)", job_id, node_id)
                        return None
                    job.claimed = True
                    job.claimed_at = time.time()
                    logger.info("Job %s claimed by %s", job_id, node_id)
                    return job
        logger.warning("Claim failed — job %s not found in queue for %s", job_id, node_id)
        return None

    # -- routing lookup -----------------------------------------------------

    async def get_route(self, job_id: str) -> _JobRouteEntry | None:
        """Look up routing info for a job_id."""
        async with self._lock:
            return self._routes.get(job_id)

    async def get_queued_job(self, job_id: str) -> QueuedJob | None:
        """Return the QueuedJob for a pull-mode job_id, or None.

        Used by route_result to perform lazy timeout evaluation.
        """
        async with self._lock:
            route = self._routes.get(job_id)
            if route is None or route.mode != JobMode.PULL:
                return None
            for job in self._queues.get(route.target_node_id, []):
                if job.job_id == job_id:
                    return job
        return None

    async def remove_from_queue(self, job_id: str, node_id: str) -> None:
        """Remove a job from the pull queue after it has been completed."""
        async with self._lock:
            queue = self._queues.get(node_id, [])
            self._queues[node_id] = [j for j in queue if j.job_id != job_id]

    # -- stats --------------------------------------------------------------

    async def queue_depth(self, node_id: str) -> int:
        """Return count of unclaimed jobs for a node."""
        async with self._lock:
            return sum(1 for j in self._queues.get(node_id, []) if not j.claimed)

    async def all_queue_depths(self) -> dict[str, int]:
        """Return unclaimed job counts for all nodes."""
        async with self._lock:
            return {
                nid: sum(1 for j in jobs if not j.claimed)
                for nid, jobs in self._queues.items()
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_job_id(node_id: str) -> str:
    short = uuid.uuid4().hex[:8]
    return f"{node_id}-job-{short}"
