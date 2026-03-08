"""ChannelLog — persistent interaction log per cluster channel.

v6.0 Phase 6 — External Participant Interaction.

Maintains the thread-based interaction history between agents and
external participants.  Each InteractionThread tracks one question from
an agent and all participant replies, plus metadata about resolution.

Storage:
    {path}/{cluster_id}-channel.jsonl
    Append-only JSONL; each line is either a thread record or a reply record.
    On startup, lines are replayed to reconstruct in-memory state.

Thread lifecycle:
    open  →  answered   (first "answer" reply accepted)
         ↘  timed_out  (via external call when checkpoint expires)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from runtime.models import InteractionReply, InteractionThread

logger = logging.getLogger(__name__)


class ThreadNotFoundError(Exception):
    """Raised when a question_id is not found in the log."""


class ThreadAlreadyAnsweredError(Exception):
    """Raised when trying to resolve an already-answered thread."""


class ChannelLog:
    """Interaction log for one or more cluster channels.

    Supports multiple cluster_ids in a single instance (keyed by cluster_id).
    """

    def __init__(self, path: str, node_id: str) -> None:
        self._path = Path(path)
        self._node_id = node_id
        # cluster_id → list[InteractionThread]
        self._threads: dict[str, dict[str, InteractionThread]] = {}  # cluster_id → {question_id → thread}
        # open file handles per cluster_id for append
        self._files: dict[str, Path] = {}
        self._lock = asyncio.Lock()

    # ── startup ────────────────────────────────────────────────────────────

    async def startup_load(self) -> int:
        """Scan storage dir and load all existing channel logs.  Returns thread count."""
        self._path.mkdir(parents=True, exist_ok=True)
        total = 0
        for f in self._path.glob("*-channel.jsonl"):
            cluster_id = f.stem.replace("-channel", "")
            count = await self._load_cluster(cluster_id, f)
            total += count
        logger.info("ChannelLog: loaded %d thread(s) across all clusters", total)
        return total

    async def _load_cluster(self, cluster_id: str, f: Path) -> int:
        """Load one cluster's JSONL file into memory."""
        self._files[cluster_id] = f
        self._threads.setdefault(cluster_id, {})
        count = 0
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    record_type = data.pop("_record_type", "thread")
                    if record_type == "thread":
                        thread = InteractionThread(**data)
                        self._threads[cluster_id][thread.question_id] = thread
                        count += 1
                    elif record_type == "reply":
                        reply = InteractionReply(**data)
                        t = self._threads[cluster_id].get(reply.question_id)
                        if t and reply not in t.replies:
                            t.replies.append(reply)
                except Exception as exc:
                    logger.warning("ChannelLog: skip corrupt record in %s — %s", f, exc)
        return count

    def _ensure_cluster(self, cluster_id: str) -> None:
        """Create in-memory bucket + file handle for a cluster if absent."""
        if cluster_id not in self._threads:
            self._threads[cluster_id] = {}
        if cluster_id not in self._files:
            self._path.mkdir(parents=True, exist_ok=True)
            self._files[cluster_id] = self._path / f"{cluster_id}-channel.jsonl"

    # ── write helpers ──────────────────────────────────────────────────────

    async def _append(self, cluster_id: str, record_type: str, data: dict) -> None:
        """Append a record to the cluster's JSONL file."""
        f = self._files.get(cluster_id)
        if f is None:
            return
        try:
            row = {"_record_type": record_type, **data}
            with f.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        except Exception as exc:
            logger.error("ChannelLog: write error for cluster %s — %s", cluster_id, exc)

    # ── public API ─────────────────────────────────────────────────────────

    async def open_thread(self, thread: InteractionThread) -> InteractionThread:
        """Create a new interaction thread.  Returns saved thread."""
        async with self._lock:
            self._ensure_cluster(thread.cluster_id)
            self._threads[thread.cluster_id][thread.question_id] = thread
            await self._append(
                thread.cluster_id,
                "thread",
                thread.model_dump(),
            )
        logger.info(
            "ChannelLog: opened thread %s for role=%s in cluster=%s",
            thread.question_id, thread.required_role, thread.cluster_id,
        )
        return thread

    async def get_thread(self, cluster_id: str, question_id: str) -> InteractionThread:
        """Get a thread by cluster + question_id.  Raises ThreadNotFoundError if absent."""
        bucket = self._threads.get(cluster_id, {})
        t = bucket.get(question_id)
        if t is None:
            raise ThreadNotFoundError(f"Thread {question_id} not found in cluster {cluster_id}")
        return t

    async def add_reply(self, cluster_id: str, reply: InteractionReply) -> InteractionThread:
        """Add a reply to an existing thread.  Returns updated thread."""
        async with self._lock:
            bucket = self._threads.get(cluster_id, {})
            t = bucket.get(reply.question_id)
            if t is None:
                raise ThreadNotFoundError(reply.question_id)
            t.replies.append(reply)
            await self._append(cluster_id, "reply", reply.model_dump())
        return t

    async def resolve_thread(
        self,
        cluster_id: str,
        question_id: str,
        resolution: InteractionReply,
    ) -> InteractionThread:
        """Mark a thread as answered with the given reply as resolution.

        Raises ThreadAlreadyAnsweredError if already resolved.
        """
        async with self._lock:
            bucket = self._threads.get(cluster_id, {})
            t = bucket.get(question_id)
            if t is None:
                raise ThreadNotFoundError(question_id)
            if t.status == "answered":
                raise ThreadAlreadyAnsweredError(question_id)
            t.replies.append(resolution)
            t.resolution = resolution
            t.status = "answered"
            t.answered_at = time.time()
            # Persist the reply and the updated thread state
            await self._append(cluster_id, "reply", resolution.model_dump())
            await self._append(cluster_id, "thread", t.model_dump())
        logger.info(
            "ChannelLog: thread %s resolved by participant=%s",
            question_id, resolution.participant_id,
        )
        return t

    async def timeout_thread(self, cluster_id: str, question_id: str) -> InteractionThread:
        """Mark a thread as timed_out."""
        async with self._lock:
            bucket = self._threads.get(cluster_id, {})
            t = bucket.get(question_id)
            if t is None:
                raise ThreadNotFoundError(question_id)
            t.status = "timed_out"
            await self._append(cluster_id, "thread", t.model_dump())
        return t

    async def list_threads(
        self,
        cluster_id: str,
        status: str | None = None,
    ) -> list[InteractionThread]:
        """List threads for a cluster, optionally filtered by status."""
        bucket = self._threads.get(cluster_id, {})
        result = list(bucket.values())
        if status is not None:
            result = [t for t in result if t.status == status]
        return sorted(result, key=lambda t: t.created_at)

    async def list_pending(
        self,
        cluster_id: str,
        required_role: str | None = None,
    ) -> list[InteractionThread]:
        """Return open threads, optionally filtered by required_role.

        Used by polling participants to check for pending questions.
        """
        threads = await self.list_threads(cluster_id, status="open")
        if required_role is not None:
            threads = [t for t in threads if t.required_role == required_role]
        return threads

    def cluster_ids(self) -> list[str]:
        return list(self._threads.keys())

    def thread_count(self, cluster_id: str | None = None) -> int:
        if cluster_id:
            return len(self._threads.get(cluster_id, {}))
        return sum(len(v) for v in self._threads.values())
