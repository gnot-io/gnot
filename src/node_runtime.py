"""Entry point for the Mesh Node Runtime v5.3.

v5.3 change: if the node config declares gateway_node_id + gateway_address,
a WorkerAgent is attached to the app lifecycle — it registers with the gateway
on startup and runs heartbeat + poll loops in the background.

Usage:
    python node_runtime.py --config node-0/node.yaml   # gateway
    python node_runtime.py --config node-1/node.yaml   # worker behind NAT
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from runtime.action_loader import load_actions
from runtime.config import load_config
from runtime.schema_validator import load_schemas
from runtime.server import create_app

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def register_signal_handlers() -> None:
    def _handle_signal(signum: int, frame: object) -> None:
        logging.getLogger(__name__).info(
            "Received signal %d — shutting down gracefully", signum
        )
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

SEED_NODE_ID: str = "node-0"
DEFAULT_SEED_ACTIONS_DIR: str = "seed/actions"


def resolve_actions_dir(config_path: str, node_id: str, actions_dir_override: str | None) -> Path:
    if actions_dir_override:
        return Path(actions_dir_override)
    if node_id == SEED_NODE_ID:
        return Path(DEFAULT_SEED_ACTIONS_DIR)
    return Path(config_path).parent / "actions"


def resolve_skills_path(config_path: str, skills_file_override: str | None) -> str | None:
    if skills_file_override:
        p = Path(skills_file_override)
        return str(p) if p.exists() else None
    default = Path(config_path).parent / "skills.md"
    return str(default) if default.exists() else None


# ---------------------------------------------------------------------------
# Worker agent lifecycle hooks
# ---------------------------------------------------------------------------

def attach_worker_agent(app: FastAPI, config: object, registry: object, schema_registry: object | None = None) -> None:
    """Attach WorkerAgent startup/shutdown hooks to the FastAPI app.

    Only called when config.is_worker is True.
    """
    from runtime.action_executor import ActionExecutor
    from runtime.job_manager import JobManager
    from runtime.schema_validator import ActionSchemaValidator
    from runtime.worker_agent import WorkerAgent

    logger = logging.getLogger(__name__)

    # WorkerAgent needs its own executor so it can run pulled jobs locally
    worker_job_manager = JobManager()
    worker_executor = ActionExecutor(
        registry,           # type: ignore[arg-type]
        worker_job_manager,
        config.node_id,     # type: ignore[attr-defined]
        schema_validator=ActionSchemaValidator({}),
    )
    agent = WorkerAgent(
        config=config,
        executor=worker_executor,
        action_registry=dict(registry),      # type: ignore[arg-type]
        schema_registry=dict(schema_registry or {}),
    )

    @app.on_event("startup")
    async def start_worker_agent() -> None:
        logger.info(
            "Worker mode — starting agent (gateway=%s)",
            config.gateway_address,     # type: ignore[attr-defined]
        )
        # v5.10: inject agent into server's worker_agent_ref so /nodes/register
        # can call agent.add_sub_route() when sub-nodes register.
        if hasattr(app.state, "worker_agent_ref"):
            app.state.worker_agent_ref["agent"] = agent
        # v5.12: inject local node_registry so agent can filter stale sub-routes
        if hasattr(app.state, "node_registry"):
            agent._local_node_registry = app.state.node_registry
        await agent.start()

    @app.on_event("shutdown")
    async def stop_worker_agent() -> None:
        await agent.stop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Mesh Node Runtime v5.3")
    parser.add_argument("--config", required=True, help="Path to node.yaml")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    setup_logging(args.log_level)
    register_signal_handlers()

    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("Mesh Node Runtime v5.3 starting...")
    logger.info("=" * 60)

    config = load_config(args.config)
    actions_dir = resolve_actions_dir(args.config, config.node_id, config.actions_dir)
    skills_path = resolve_skills_path(args.config, config.skills_file)

    registry = load_actions(actions_dir)
    schema_registry = load_schemas(actions_dir)
    runtime_base_dir = str(Path(args.config).parent.parent)

    mode_flags = []
    if config.is_gateway:
        mode_flags.append(f"gateway (trusted_nodes={config.trusted_nodes})")
    if config.is_worker:
        mode_flags.append(f"worker (gateway={config.gateway_address})")
    if not mode_flags:
        mode_flags.append("standalone")

    logger.info(
        "Node %s — listen=%s, mode=%s, actions=%s, auth=%s, llm=%s",
        config.node_id,
        config.listen,
        "+".join(mode_flags),
        list(registry.keys()),
        "enabled" if config.auth_token else "disabled",
        f"enabled ({config.llm_default_model})" if config.llm_enabled else "disabled",
    )

    app = create_app(
        config,
        registry,
        skills_path,
        schema_registry=schema_registry,
        config_path=args.config,
        runtime_base_dir=runtime_base_dir,
    )

    # v5.3: attach worker agent if this node is configured as a worker
    if config.is_worker:
        attach_worker_agent(app, config, registry, schema_registry)

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=args.log_level.lower(),
    )


if __name__ == "__main__":
    main()
