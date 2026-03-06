"""Node bootstrap workflow with automatic rollback.

Provides a high-level API for the Cloud AI Planner to create new nodes
in the mesh. Each bootstrap operation is wrapped in a transactional
context that rolls back all changes on failure.

Workflow steps:
    1. Create node directory structure
    2. Write node.yaml configuration
    3. Write skills.md
    4. Write action modules
    5. Install dependencies (optional)
    6. Start the node runtime process
    7. Health-check the new node
    8. Register the new node in the seed's config

If any step fails, all prior steps are automatically reversed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEALTH_CHECK_TIMEOUT_SECONDS: float = 10.0
HEALTH_CHECK_RETRIES: int = 5
HEALTH_CHECK_INTERVAL_SECONDS: float = 2.0
PROCESS_START_WAIT_SECONDS: float = 3.0
PROCESS_KILL_TIMEOUT_SECONDS: float = 5.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class BootstrapStatus(str, Enum):
    """Status of a bootstrap operation."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class ActionFile:
    """An action module to be deployed to the new node."""

    filename: str
    content: str
    schema_content: str | None = None


@dataclass
class BootstrapRequest:
    """All inputs needed to bootstrap a new node."""

    node_id: str
    listen: str
    base_dir: str = "."
    actions: list[ActionFile] = field(default_factory=list)
    skills_md: str = ""
    extra_nodes: dict[str, str] = field(default_factory=dict)
    default_resolver: str = "node-0"
    max_hop: int = 10
    cache_ttl_seconds: int = 300
    pip_packages: list[str] = field(default_factory=list)
    runtime_entry: str = "node_runtime.py"
    auth_token: str | None = None


@dataclass
class BootstrapResult:
    """Result of a bootstrap operation."""

    node_id: str
    status: BootstrapStatus
    address: str | None = None
    pid: int | None = None
    error: str | None = None
    steps_completed: list[str] = field(default_factory=list)
    steps_rolled_back: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rollback tracker
# ---------------------------------------------------------------------------

class _RollbackTracker:
    """Tracks actions that need to be reversed on failure.

    Each registered undo operation is a coroutine that will be called
    in reverse order during rollback.
    """

    def __init__(self) -> None:
        self._undos: list[tuple[str, Any]] = []

    def register(self, step_name: str, undo_coro: Any) -> None:
        """Register an undo operation for a completed step."""
        self._undos.append((step_name, undo_coro))

    async def rollback(self) -> list[str]:
        """Execute all undo operations in reverse order.

        Returns:
            List of step names that were rolled back.
        """
        rolled_back: list[str] = []
        for step_name, undo_coro in reversed(self._undos):
            try:
                logger.info("Rolling back step: %s", step_name)
                await undo_coro
                rolled_back.append(step_name)
            except Exception:
                logger.exception("Rollback failed for step: %s", step_name)
                rolled_back.append(f"{step_name} (partial)")
        return rolled_back


# ---------------------------------------------------------------------------
# Bootstrap Engine
# ---------------------------------------------------------------------------

class BootstrapEngine:
    """Orchestrates the creation of new mesh nodes with auto-rollback.

    This engine is intended to be invoked by the Cloud AI Planner or
    directly via an admin action on the seed node.
    """

    def __init__(self, seed_config_path: str | None = None) -> None:
        self._seed_config_path = seed_config_path

    async def bootstrap(self, request: BootstrapRequest) -> BootstrapResult:
        """Execute the full bootstrap workflow for a new node.

        Args:
            request: Complete specification of the node to create.

        Returns:
            A BootstrapResult with status and details.
        """
        result = BootstrapResult(node_id=request.node_id, status=BootstrapStatus.IN_PROGRESS)
        tracker = _RollbackTracker()
        pid: int | None = None

        try:
            # -- Step 1: Create directory structure ----------------------------
            node_dir = Path(request.base_dir) / request.node_id
            await self._step_create_dirs(node_dir, tracker, result)

            # -- Step 2: Write node.yaml --------------------------------------
            await self._step_write_config(node_dir, request, tracker, result)

            # -- Step 3: Write skills.md --------------------------------------
            await self._step_write_skills(node_dir, request, tracker, result)

            # -- Step 4: Write action modules ----------------------------------
            await self._step_write_actions(node_dir, request, tracker, result)

            # -- Step 5: Install pip packages (optional) ----------------------
            if request.pip_packages:
                await self._step_install_deps(request, tracker, result)

            # -- Step 6: Start the node process --------------------------------
            pid = await self._step_start_node(node_dir, request, tracker, result)

            # -- Step 7: Health-check ------------------------------------------
            host, port = request.listen.rsplit(":", 1)
            bind_host = "127.0.0.1" if host == "0.0.0.0" else host
            address = f"http://{bind_host}:{port}"
            await self._step_health_check(address, tracker, result)

            # -- Step 8: Register in seed config (optional) --------------------
            if self._seed_config_path:
                await self._step_register_node(
                    request.node_id, address, tracker, result
                )

            result.status = BootstrapStatus.COMPLETED
            result.address = address
            result.pid = pid
            logger.info(
                "Bootstrap completed for %s — address=%s, pid=%s",
                request.node_id,
                address,
                pid,
            )

        except Exception as exc:
            logger.error("Bootstrap failed for %s: %s", request.node_id, exc)
            result.status = BootstrapStatus.FAILED
            result.error = str(exc)

            # -- Auto rollback ------------------------------------------------
            logger.info("Starting auto-rollback for %s...", request.node_id)
            rolled_back = await tracker.rollback()
            result.steps_rolled_back = rolled_back
            result.status = BootstrapStatus.ROLLED_BACK
            logger.info(
                "Rollback completed — %d step(s) reversed: %s",
                len(rolled_back),
                rolled_back,
            )

        return result

    # -- Individual steps ---------------------------------------------------

    async def _step_create_dirs(
        self,
        node_dir: Path,
        tracker: _RollbackTracker,
        result: BootstrapResult,
    ) -> None:
        """Create the node directory and actions subdirectory."""
        actions_dir = node_dir / "actions"

        def _do() -> None:
            node_dir.mkdir(parents=True, exist_ok=True)
            actions_dir.mkdir(parents=True, exist_ok=True)

        await asyncio.to_thread(_do)

        async def _undo() -> None:
            await asyncio.to_thread(shutil.rmtree, str(node_dir), True)

        tracker.register("create_dirs", _undo())
        result.steps_completed.append("create_dirs")
        logger.info("Created directory: %s", node_dir)

    async def _step_write_config(
        self,
        node_dir: Path,
        request: BootstrapRequest,
        tracker: _RollbackTracker,
        result: BootstrapResult,
    ) -> None:
        """Write node.yaml for the new node."""
        config_data: dict[str, Any] = {
            "node_id": request.node_id,
            "listen": request.listen,
            "nodes": {request.node_id: f"http://127.0.0.1:{request.listen.rsplit(':', 1)[1]}"},
            "default_resolver": request.default_resolver,
            "max_hop": request.max_hop,
            "cache_ttl_seconds": request.cache_ttl_seconds,
        }
        if request.extra_nodes:
            config_data["nodes"].update(request.extra_nodes)
        if request.auth_token:
            config_data["auth_token"] = request.auth_token

        config_path = node_dir / "node.yaml"

        def _do() -> None:
            with open(config_path, "w", encoding="utf-8") as fh:
                yaml.dump(config_data, fh, default_flow_style=False)

        await asyncio.to_thread(_do)
        # Undo covered by directory removal
        result.steps_completed.append("write_config")
        logger.info("Wrote config: %s", config_path)

    async def _step_write_skills(
        self,
        node_dir: Path,
        request: BootstrapRequest,
        tracker: _RollbackTracker,
        result: BootstrapResult,
    ) -> None:
        """Write skills.md for the new node."""
        skills_path = node_dir / "skills.md"
        content = request.skills_md or f"# Node: {request.node_id}\n\nNo skills defined yet.\n"

        def _do() -> None:
            skills_path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_do)
        result.steps_completed.append("write_skills")
        logger.info("Wrote skills: %s", skills_path)

    async def _step_write_actions(
        self,
        node_dir: Path,
        request: BootstrapRequest,
        tracker: _RollbackTracker,
        result: BootstrapResult,
    ) -> None:
        """Write action Python modules and optional schema files."""
        actions_dir = node_dir / "actions"
        for action_file in request.actions:
            py_path = actions_dir / action_file.filename
            py_path.write_text(action_file.content, encoding="utf-8")
            logger.info("Wrote action: %s", py_path)

            if action_file.schema_content:
                schema_name = action_file.filename.replace(".py", ".schema.json")
                schema_path = actions_dir / schema_name
                schema_path.write_text(action_file.schema_content, encoding="utf-8")
                logger.info("Wrote schema: %s", schema_path)

        result.steps_completed.append("write_actions")

    async def _step_install_deps(
        self,
        request: BootstrapRequest,
        tracker: _RollbackTracker,
        result: BootstrapResult,
    ) -> None:
        """Install pip packages for the new node."""
        packages = " ".join(request.pip_packages)
        logger.info("Installing packages: %s", packages)

        proc = await asyncio.create_subprocess_shell(
            f"pip install {packages}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            raise RuntimeError(
                f"pip install failed (exit={proc.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')}"
            )

        result.steps_completed.append("install_deps")
        logger.info("Dependencies installed successfully")

    async def _step_start_node(
        self,
        node_dir: Path,
        request: BootstrapRequest,
        tracker: _RollbackTracker,
        result: BootstrapResult,
    ) -> int:
        """Start the node runtime as a background process."""
        config_path = node_dir / "node.yaml"
        cmd = f"python {request.runtime_entry} --config {config_path}"
        logger.info("Starting node process: %s", cmd)

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        await asyncio.sleep(PROCESS_START_WAIT_SECONDS)

        if proc.returncode is not None:
            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
            raise RuntimeError(f"Node process exited immediately (code={proc.returncode}): {stderr}")

        pid = proc.pid

        async def _undo() -> None:
            await _kill_process(pid)

        tracker.register("start_node", _undo())
        result.steps_completed.append("start_node")
        logger.info("Node process started: pid=%d", pid)
        return pid

    async def _step_health_check(
        self,
        address: str,
        tracker: _RollbackTracker,
        result: BootstrapResult,
    ) -> None:
        """Verify the new node is healthy."""
        url = f"{address}/health"
        logger.info("Health-checking %s ...", url)

        for attempt in range(1, HEALTH_CHECK_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "healthy":
                            result.steps_completed.append("health_check")
                            logger.info("Health check passed (attempt %d)", attempt)
                            return
            except httpx.HTTPError:
                pass

            if attempt < HEALTH_CHECK_RETRIES:
                logger.warning("Health check attempt %d failed, retrying...", attempt)
                await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

        raise RuntimeError(f"Health check failed after {HEALTH_CHECK_RETRIES} attempts: {url}")

    async def _step_register_node(
        self,
        node_id: str,
        address: str,
        tracker: _RollbackTracker,
        result: BootstrapResult,
    ) -> None:
        """Add the new node to the seed's node.yaml config."""
        if not self._seed_config_path:
            return

        config_path = Path(self._seed_config_path)

        def _do() -> dict[str, Any]:
            with open(config_path, "r", encoding="utf-8") as fh:
                config = yaml.safe_load(fh) or {}
            original = dict(config.get("nodes", {}))
            config.setdefault("nodes", {})[node_id] = address
            with open(config_path, "w", encoding="utf-8") as fh:
                yaml.dump(config, fh, default_flow_style=False)
            return original

        original_nodes = await asyncio.to_thread(_do)

        async def _undo() -> None:
            def _revert() -> None:
                with open(config_path, "r", encoding="utf-8") as fh:
                    config = yaml.safe_load(fh) or {}
                config["nodes"] = original_nodes
                with open(config_path, "w", encoding="utf-8") as fh:
                    yaml.dump(config, fh, default_flow_style=False)
            await asyncio.to_thread(_revert)

        tracker.register("register_node", _undo())
        result.steps_completed.append("register_node")
        logger.info("Registered %s in seed config at %s", node_id, address)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _kill_process(pid: int) -> None:
    """Kill a process by PID, with escalation to SIGKILL."""
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait for graceful shutdown
        for _ in range(int(PROCESS_KILL_TIMEOUT_SECONDS)):
            await asyncio.sleep(1)
            try:
                os.kill(pid, 0)  # Check if still running
            except ProcessLookupError:
                logger.info("Process %d terminated gracefully", pid)
                return
        # Force kill
        os.kill(pid, signal.SIGKILL)
        logger.info("Process %d force-killed", pid)
    except ProcessLookupError:
        logger.info("Process %d already gone", pid)
    except Exception:
        logger.exception("Failed to kill process %d", pid)
