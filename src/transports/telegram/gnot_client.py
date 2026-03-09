"""GNOTClient — HTTP client for calling GNOT node endpoints.

Used by TelegramBotInstance to forward messages and query state.
Zero knowledge of Telegram — pure GNOT API wrapper.

Self-call pattern: if bot instance runs in-process with gateway,
base_url = "http://localhost:{port}". No network overhead.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class GNOTAPIError(Exception):
    """Raised when GNOT API returns an unexpected error."""
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"GNOT API error {status}: {body}")
        self.status = status
        self.body = body


class GNOTClient:
    """HTTP client for calling GNOT node endpoints.

    Provides typed wrappers around the most commonly used GNOT API endpoints.
    Handles auth token injection and error responses uniformly.
    """

    def __init__(self, base_url: str, auth_token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._session: Any = None  # aiohttp.ClientSession — lazy init

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize aiohttp session."""
        try:
            import aiohttp
            headers: dict[str, str] = {}
            if self._auth_token:
                headers["Authorization"] = f"Bearer {self._auth_token}"
            self._session = aiohttp.ClientSession(headers=headers)
        except ImportError:
            raise ImportError("aiohttp is required for GNOTClient. Install with: pip install aiohttp")

    async def close(self) -> None:
        """Close aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ── core request ───────────────────────────────────────────────────────

    async def _get(self, path: str) -> tuple[int, Any]:
        """GET request. Returns (status_code, parsed_json_or_None)."""
        if self._session is None:
            await self.start()
        import aiohttp
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                body = await resp.json() if resp.content_type == "application/json" else None
                return resp.status, body
        except Exception as exc:
            logger.error("GNOTClient GET %s failed: %s", path, exc)
            raise

    async def _post(self, path: str, payload: dict) -> tuple[int, Any]:
        """POST request with JSON body. Returns (status_code, parsed_json_or_None)."""
        if self._session is None:
            await self.start()
        import aiohttp
        url = f"{self._base_url}{path}"
        try:
            async with self._session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                body = await resp.json() if resp.content_type == "application/json" else None
                return resp.status, body
        except Exception as exc:
            logger.error("GNOTClient POST %s failed: %s", path, exc)
            raise

    # ── GNOT API wrappers ──────────────────────────────────────────────────

    async def post_intent(self, prompt: str, session_id: str) -> dict:
        """POST /intent — forward a user message to the agent loop.

        Returns the full response dict from GNOT (may include 'reply',
        'suspended', 'question', etc.).
        Raises GNOTAPIError on HTTP errors.
        """
        status, body = await self._post("/intent", {
            "prompt": prompt,
            "session_id": session_id,
        })
        if status >= 400:
            raise GNOTAPIError(status, str(body))
        return body or {}

    async def get_session(self, session_id: str) -> dict | None:
        """GET /sessions/{session_id} — check if session exists.

        Returns session dict if found, None if 404.
        """
        status, body = await self._get(f"/sessions/{session_id}")
        if status == 404:
            return None
        if status >= 400:
            raise GNOTAPIError(status, str(body))
        return body

    async def get_tasks(self) -> list[dict]:
        """GET /tasks — list all active tasks.

        Returns list of task dicts. Used to find suspended tasks.
        """
        status, body = await self._get("/tasks")
        if status >= 400:
            raise GNOTAPIError(status, str(body))
        if isinstance(body, dict):
            return body.get("tasks", [])
        return []

    async def post_channels_respond(
        self,
        cluster_id: str,
        question_id: str,
        participant_id: str,
        auth_token: str,
        content: str,
    ) -> dict:
        """POST /channels/{cluster_id}/interactions/{question_id}/respond — answer a question.

        Used by Pattern 1 (transactional Q&A).
        """
        status, body = await self._post(
            f"/channels/{cluster_id}/interactions/{question_id}/respond",
            {
                "participant_id": participant_id,
                "auth_token": auth_token,
                "content": content,
            },
        )
        if status >= 400:
            raise GNOTAPIError(status, str(body))
        return body or {}

    async def get_channel_pending(self, cluster_id: str) -> list[dict]:
        """GET /channels/{cluster_id}/pending — list open (unanswered) threads.

        Returns list of thread dicts.
        """
        status, body = await self._get(f"/channels/{cluster_id}/pending")
        if status >= 400:
            raise GNOTAPIError(status, str(body))
        if isinstance(body, dict):
            return body.get("threads", [])
        return []

    async def register_participant(self, payload: dict) -> dict:
        """POST /participants/register — register an external participant.

        Used by /register command (Pattern 1 setup).
        """
        status, body = await self._post("/participants/register", payload)
        if status >= 400:
            raise GNOTAPIError(status, str(body))
        return body or {}
