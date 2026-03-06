"""Authentication middleware for the mesh node runtime.

Provides Bearer-token based authentication for all protected endpoints.
Tokens are configured in node.yaml under the ``auth`` section.

Exempt paths (configurable):
    - GET /health
    - GET /skills
    - GET /setup.sh
    - GET /runtime-bundle
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTH_HEADER: str = "Authorization"
BEARER_PREFIX: str = "Bearer "

# Paths exempt from authentication by default
DEFAULT_EXEMPT_PATHS: frozenset[str] = frozenset({
    "/health",
    "/skills",
    "/docs",
    "/openapi.json",
    "/setup.sh",
    "/runtime-bundle",
    # v5.3 — always accessible so gateway can probe workers without token
    "/ping",
})


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class AuthMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that enforces Bearer token authentication.

    Supports two modes:
      - Single token (auth_token): all callers share one token.
      - Multi-token (allowed_tokens): each caller has a unique token.
        Used when deploying with per-node tokens (v5.13+).

    If neither is configured, all requests are allowed (open mode).
    """

    def __init__(
        self,
        app: Any,
        auth_token: str | None = None,
        allowed_tokens: list[str] | None = None,  # v5.13: per-node tokens
        exempt_paths: frozenset[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._auth_token = auth_token
        # Build a set of all valid tokens (single + list)
        self._allowed_tokens: frozenset[str] = frozenset(
            ([auth_token] if auth_token else []) +
            (allowed_tokens or [])
        )
        self._exempt_paths = exempt_paths or DEFAULT_EXEMPT_PATHS
        if self._allowed_tokens:
            logger.info(
                "Auth enabled — %d token(s), %d exempt paths",
                len(self._allowed_tokens), len(self._exempt_paths),
            )
        else:
            logger.info("Auth disabled — all requests allowed (no token configured)")

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Check authorization before forwarding to the endpoint."""
        # No token configured → open mode
        if not self._allowed_tokens:
            return await call_next(request)

        # Exempt paths skip auth
        if request.url.path in self._exempt_paths:
            return await call_next(request)

        # Extract token from header
        auth_header = request.headers.get(AUTH_HEADER, "")
        if not auth_header.startswith(BEARER_PREFIX):
            logger.warning("Missing or malformed Authorization header from %s", request.client)
            return JSONResponse(
                content={"error": "UNAUTHORIZED", "detail": "Missing Bearer token"},
                status_code=401,
            )

        provided_token = auth_header[len(BEARER_PREFIX):]

        # v5.13: check against all allowed tokens (constant-time per token)
        if not any(
            secrets.compare_digest(provided_token, t)
            for t in self._allowed_tokens
        ):
            logger.warning("Invalid token from %s", request.client)
            return JSONResponse(
                content={"error": "FORBIDDEN", "detail": "Invalid token"},
                status_code=403,
            )

        return await call_next(request)
