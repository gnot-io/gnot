"""Action executor — sync/async dispatch with context injection, schema validation, and LLM.

Determines whether an action should run synchronously or asynchronously,
validates params against JSON Schema (if available), builds the runtime
context (including shared LLM client), and returns the appropriate response.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from runtime.action_loader import ActionRegistry
from runtime.config import CallerPolicy, check_caller_policy
from runtime.job_manager import JobManager, JobStatus
from runtime.llm_client import LLMClient
from runtime.models import (
    AsyncActionResponse,
    SyncActionResponse,
)
from runtime.schema_validator import ActionSchemaValidator, SchemaValidationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# v5.11 — Authorization exceptions
# ---------------------------------------------------------------------------

class CallerNotAllowedError(Exception):
    """Raised when caller's token does not permit the requested action."""
    def __init__(self, action: str) -> None:
        super().__init__(f"CALLER_NOT_ALLOWED: action '{action}' is not permitted for this caller")
        self.action = action


class MissingCallerCredentialError(Exception):
    """Raised when a required caller credential is absent from the request."""
    def __init__(self, action: str, missing: list[str]) -> None:
        detail = ", ".join(missing)
        super().__init__(f"MISSING_CALLER_CREDENTIAL: action '{action}' requires: {detail}")
        self.action = action
        self.missing = missing


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEMP_BASE_DIR: str = "/tmp/mesh"
DEFAULT_ESTIMATED_SECONDS: int = 30


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class ActionExecutor:
    """Dispatches action calls through the plugin registry.

    Handles schema validation, context injection (including LLM client),
    sync/async detection, and background task management.
    """

    def __init__(
        self,
        registry: ActionRegistry,
        job_manager: JobManager,
        node_id: str,
        config: dict[str, Any] | None = None,
        schema_validator: ActionSchemaValidator | None = None,
        llm_client: LLMClient | None = None,
        caller_policies: list[CallerPolicy] | None = None,
    ) -> None:
        self._registry = registry
        self._job_manager = job_manager
        self._node_id = node_id
        self._config = config or {}
        self._schema_validator = schema_validator
        self._llm_client = llm_client
        self._caller_policies: list[CallerPolicy] = caller_policies or []

    # -- public API ---------------------------------------------------------

    async def execute(
        self,
        action_name: str,
        params: dict[str, Any],
        task_id: str,
        caller_token: str | None = None,
        caller_credentials: dict[str, str] | None = None,
    ) -> SyncActionResponse | AsyncActionResponse:
        """Execute an action by name.

        Args:
            action_name: Key in the action registry.
            params: Parameters forwarded to the action's ``run()`` function.
            task_id: Global workflow task ID.

        Returns:
            A SyncActionResponse for immediate results or
            an AsyncActionResponse for background jobs.

        Raises:
            KeyError: If the action is not found in the registry.
            SchemaValidationError: If params fail schema validation.
        """
        module = self._registry.get(action_name)
        if module is None:
            raise KeyError(f"Action not found: {action_name}")

        # -- v5.11: Caller policy check -------------------------------------
        if not check_caller_policy(self._caller_policies, caller_token, action_name):
            raise CallerNotAllowedError(action_name)

        # -- v5.11: Caller credential check ---------------------------------
        if self._schema_validator:
            missing = self._schema_validator.check_caller_credentials(
                action_name, caller_credentials or {}
            )
            if missing:
                raise MissingCallerCredentialError(action_name, missing)

        # -- Schema validation (params only) --------------------------------
        if self._schema_validator:
            self._schema_validator.validate(action_name, params)

        is_async = getattr(module, "ASYNC", False)
        creds = caller_credentials or {}

        if is_async:
            return await self._execute_async(module, action_name, params, task_id, creds)
        return await self._execute_sync(module, action_name, params, task_id, creds)

    # -- internal -----------------------------------------------------------

    async def _execute_sync(
        self,
        module: Any,
        action_name: str,
        params: dict[str, Any],
        task_id: str,
        caller_credentials: dict[str, str] | None = None,
    ) -> SyncActionResponse:
        """Run an action synchronously (wrapping in thread for blocking I/O)."""
        context = self._build_context(task_id=task_id, job_id=None, action_name=action_name, caller_credentials=caller_credentials)
        logger.info("Executing sync action: %s (task=%s)", action_name, task_id)

        try:
            output = await asyncio.to_thread(module.run, params, context)
        except Exception as exc:
            logger.exception("Sync action %s failed", action_name)
            output = {"error": str(exc)}

        return SyncActionResponse(task_id=task_id, output=output)

    async def _execute_async(
        self,
        module: Any,
        action_name: str,
        params: dict[str, Any],
        task_id: str,
        caller_credentials: dict[str, str] | None = None,
    ) -> AsyncActionResponse:
        """Spawn an action as a background asyncio task."""
        job = await self._job_manager.create_job(
            node_id=self._node_id,
            task_id=task_id,
            estimated_completion_seconds=DEFAULT_ESTIMATED_SECONDS,
        )

        context = self._build_context(
            task_id=task_id,
            job_id=job.job_id,
            action_name=action_name,
            caller_credentials=caller_credentials,
        )

        logger.info(
            "Spawning async action: %s (task=%s, job=%s)",
            action_name,
            task_id,
            job.job_id,
        )

        asyncio.create_task(
            self._run_async_job(module, params, context, job.job_id)
        )

        return AsyncActionResponse(
            task_id=task_id,
            job_id=job.job_id,
            estimated_completion_seconds=job.estimated_completion_seconds,
        )

    async def _run_async_job(
        self,
        module: Any,
        params: dict[str, Any],
        context: dict[str, Any],
        job_id: str,
    ) -> None:
        """Background coroutine that runs an async action and updates job state."""
        await self._job_manager.update_job(job_id, status=JobStatus.RUNNING, progress=0)

        try:
            run_fn = module.run
            if asyncio.iscoroutinefunction(run_fn):
                output = await run_fn(params, context)
            else:
                output = await asyncio.to_thread(run_fn, params, context)

            await self._job_manager.update_job(
                job_id,
                status=JobStatus.COMPLETED,
                progress=100,
                output=output,
            )
            logger.info("Async job completed: %s", job_id)

        except Exception as exc:
            logger.exception("Async job failed: %s", job_id)
            await self._job_manager.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=str(exc),
            )

    def _build_context(
        self,
        task_id: str,
        job_id: str | None,
        action_name: str,
        caller_credentials: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Build the context dict injected into every action call."""
        temp_dir = Path(TEMP_BASE_DIR) / task_id
        temp_dir.mkdir(parents=True, exist_ok=True)

        ctx: dict[str, Any] = {
            "task_id": task_id,
            "job_id": job_id,
            "node_id": self._node_id,
            "config": self._config,
            "logger": logging.getLogger(f"action.{action_name}"),
            "temp_dir": str(temp_dir),
            # v5.11: caller credentials injected for action use
            # (service credentials are managed by the action itself via env vars)
            "caller_credentials": caller_credentials or {},
        }

        # Inject LLM client if configured
        if self._llm_client:
            ctx["llm"] = self._llm_client

        return ctx
