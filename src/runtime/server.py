"""FastAPI HTTP server for the mesh node runtime v5.8.

v5.8 changes over v5.7:
  - POST /upload  — accept multipart file upload, return file_id + download_url.
  - GET  /download/{file_id} — stream file back (FileResponse).
  - GET  /files   — list non-expired uploads with metadata.
  - DELETE /files/{file_id} — explicit cleanup before TTL.
  - UploadManager: lazy TTL, configurable dir/max-size/ttl via node.yaml.
  - Lifespan: calls upload_manager.sweep_expired() on startup.
  - Version bumped to 5.8.0.

v5.7 changes (retained):
  - lifespan replaces deprecated on_event("startup"/"shutdown").
  - GET /health now includes queue_depths per node (observability).
  - Pull job timeout: lazy check in route_result (via GatewayRouter).
  - Lazy staleness check in NodeRegistry (no background loop needed).

v5.6 changes (retained):
  - ActionRequest.task_id and .trace are optional (server fills defaults).
  - mesh_ctl.py deprecated — Claude Web uses curl directly.

v5.3 additions (retained):
  - GatewayRouter replaces RequestRouter (push/pull dispatch)
  - NodeRegistry (trusted nodes + heartbeat tracking)
  - JobQueue (per-node pull queue + push-job routing table)
  - Worker-facing endpoints: /nodes/register, /nodes/{id}/heartbeat,
    /ping, /jobs/poll, /jobs/{id}/claim, /jobs/{id}/result
  - GET /result/{job_id} routes through GatewayRouter
  - GET /nodes — list registered trusted nodes (debug)
"""

from __future__ import annotations

import io
import logging
import tarfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

from runtime.action_executor import ActionExecutor
from runtime.action_loader import ActionRegistry
from runtime.auth import AuthMiddleware
from runtime.bootstrap import BootstrapEngine, BootstrapRequest, BootstrapStatus
from runtime.config import NodeConfig
from runtime.gateway_router import GatewayRouter
from runtime.job_manager import JobManager, JobStatus
from runtime.job_queue import JobQueue
from runtime.llm_client import LLMClient
from runtime.models import (
    ActionRequest,
    AsyncActionResponse,
    ClaimRequest,
    ErrorResponse,
    HealthResponse,
    HeartbeatRequest,
    JobResultReport,
    JobStatusResponse,
    NodeRegistrationRequest,
    ResolveResponse,
    SyncActionResponse,
    UploadResponse,
    FileInfo,
    FileListResponse,
    CapabilityNode,
    CapabilityTreeResponse,
    IntentRequest,
    IntentResponse,
    ConversationMessage,
    SessionInfo,
    Event,
    Subscription,
    EmitRequest,
    EmitResponse,
    SubscribeRequest,
    SubscribeResponse,
    ScheduleEntry,
    SchedulePatchRequest,
)
from runtime.node_registry import NodeRegistry
from runtime.resolver import NodeNotFoundError, NodeResolver
from runtime.schema_validator import ActionSchemaValidator, SchemaRegistry, SchemaValidationError
from runtime.upload_manager import FileTooLargeError, UploadManager
from runtime.conversation_store import ConversationStore
from runtime.credential_store import CredentialStore
from runtime.intent_handler import IntentHandler
from runtime.event_bus import EventBus
from runtime.channel_registry import ChannelRegistry
from runtime.scheduler import Scheduler
from runtime.persistent_session_store import PersistentSessionStore
from runtime.agent_memory_store import AgentMemoryStore
from runtime.mcp_registry import MCPRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_app(
    config: NodeConfig,
    registry: ActionRegistry,
    skills_path: str | None = None,
    schema_registry: SchemaRegistry | None = None,
    config_path: str | None = None,
    runtime_base_dir: str | None = None,
) -> FastAPI:
    """Build and return a fully configured FastAPI application (v5.7)."""

    # -- LLM client ---------------------------------------------------------
    llm_client: LLMClient | None = None
    if config.llm_enabled:
        llm_client = LLMClient(
            api_key=config.llm_api_key or "",
            base_url=config.llm_base_url,
            default_model=config.llm_default_model,
            timeout_seconds=config.llm_timeout_seconds,
            extra_headers=config.llm_extra_headers,
        )
        logger.info(
            "LLM client initialised: base_url=%s model=%s",
            config.llm_base_url, config.llm_default_model,
        )

    # -- shared state -------------------------------------------------------
    job_manager = JobManager(
        job_ttl_seconds=config.job_ttl_seconds,
        cleanup_interval_seconds=config.cleanup_interval_seconds,
    )
    resolver = NodeResolver(config)
    schema_validator = ActionSchemaValidator(schema_registry or {})

    # -- v5.9 / v6.0 Phase 3: session store (memory or persistent) ---------
    if config.session.backend == "persistent":
        conversation_store = PersistentSessionStore(
            storage_dir=config.session.storage_dir,
            default_ttl_seconds=config.session.default_ttl_seconds,
            max_messages_per_session=config.session.max_messages_per_session,
        )
        logger.info(
            "PersistentSessionStore enabled — dir=%s ttl=%d",
            config.session.storage_dir, config.session.default_ttl_seconds,
        )
    else:
        conversation_store = ConversationStore(
            ttl_seconds=config.session.default_ttl_seconds or config.session_ttl_seconds,
        )

    # -- v6.0 Phase 3: agent memory store -----------------------------------
    memory_store: AgentMemoryStore | None = None
    if config.memory.enabled:
        memory_store = AgentMemoryStore(
            storage_dir=config.memory.storage_dir,
            node_id=config.node_id,
            max_entries=config.memory.max_entries,
            inject_into_prompt=config.memory.inject_into_prompt,
        )
        logger.info(
            "AgentMemoryStore enabled — dir=%s max_entries=%d",
            config.memory.storage_dir, config.memory.max_entries,
        )

    # -- v6.0 Phase 3: MCP registry (connect at lifespan startup) -----------
    mcp_registry: MCPRegistry | None = None
    if config.mcp_servers:
        mcp_registry = MCPRegistry(list(config.mcp_servers))
        logger.info(
            "MCPRegistry created — %d server(s) configured",
            len(config.mcp_servers),
        )

    # -- ActionExecutor (after memory_store is ready) -----------------------
    executor = ActionExecutor(
        registry, job_manager, config.node_id,
        schema_validator=schema_validator,
        llm_client=llm_client,
        caller_policies=list(config.caller_policies),
        memory_store=memory_store,   # v6.0 Phase 3
    )

    # -- v5.3: gateway state ------------------------------------------------
    node_registry = NodeRegistry(
        trusted_node_ids=config.trusted_nodes,
        heartbeat_timeout_seconds=config.heartbeat_timeout_seconds,
        ping_timeout_seconds=config.ping_timeout_seconds,
        registration_policy=config.registration_policy,  # v6.0
    )
    job_queue = JobQueue()

    # GatewayRouter replaces the old RequestRouter
    gateway_router = GatewayRouter(
        config=config,
        resolver=resolver,
        executor=executor,
        job_manager=job_manager,
        node_registry=node_registry,
        job_queue=job_queue,
    )

    bootstrap_engine = BootstrapEngine(seed_config_path=config_path)

    # -- v5.8: upload manager -----------------------------------------------
    upload_manager = UploadManager(
        upload_dir=config.upload_dir,
        max_size_bytes=config.upload_max_size_bytes,
        ttl_seconds=config.upload_ttl_seconds,
    )
    # Use dedicated credential_encryption_key if set (v5.13); otherwise
    # derive from auth_token for backward-compat with v5.12 deployments.
    _cred_enc_key = config.credential_encryption_key or config.auth_token
    credential_store = CredentialStore(
        encryption_key=_cred_enc_key,
        ttl_seconds=config.session_ttl_seconds,
        store_path=config.credential_store_path,
    )
    intent_handler: IntentHandler | None = None
    if config.llm_enabled and llm_client is not None:
        intent_handler = IntentHandler(
            config=config,
            llm_client=llm_client,
            gateway_router=gateway_router,
            node_registry=node_registry,
            action_registry=registry,
            conversation_store=conversation_store,
            schema_validator=schema_validator,  # v5.11
            credential_store=credential_store,  # v5.12
            memory_store=memory_store,          # v6.0 Phase 3
            mcp_registry=mcp_registry,          # v6.0 Phase 3
        )
        logger.info("IntentHandler ready — POST /intent enabled")
    else:
        logger.info("LLM not configured — POST /intent will return 503")
    start_time = time.time()

    # -- v6.0: EventBus + ChannelRegistry -----------------------------------
    event_bus: EventBus | None = None
    channel_registry: ChannelRegistry | None = None
    if config.event_bus.enabled:
        event_bus = EventBus(
            max_log_size=config.event_bus.max_log_size,
            delivery_timeout_seconds=config.event_bus.delivery_timeout_seconds,
            delivery_retry_count=config.event_bus.delivery_retry_count,
            delivery_retry_backoff=config.event_bus.delivery_retry_backoff,
            persistence_path=config.event_bus.persistence_path,
            gateway_router=gateway_router,
            node_id=config.node_id,
            caller_token=config.auth_token,
        )
        channel_registry = ChannelRegistry(
            node_registry=node_registry,
            gateway_node_id=config.node_id,
        )
        logger.info("EventBus enabled — node=%s", config.node_id)

    # -- v6.0 Phase 2: Scheduler -------------------------------------------
    scheduler: Scheduler | None = None
    if config.scheduler.enabled:
        scheduler = Scheduler(
            gateway_router=gateway_router,
            event_bus=event_bus,
            node_id=config.node_id,
            caller_token=config.auth_token,
        )
        logger.info("Scheduler enabled — node=%s", config.node_id)

    # -- lifespan (replaces deprecated on_event) ----------------------------
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        job_manager.start_cleanup_loop()
        swept_uploads = await upload_manager.sweep_expired()
        swept_sessions = await conversation_store.sweep_expired()
        # v5.13 — load persisted credentials + start background flusher
        _cred_sessions = await credential_store.load()
        credential_store.start_flush_task()
        # v6.0 Phase 3 — load persistent sessions from disk
        _sess_loaded = 0
        if isinstance(conversation_store, PersistentSessionStore):
            _sess_loaded = await conversation_store.startup_load()
        # v6.0 Phase 3 — load agent memory from disk
        _mem_loaded = 0
        if memory_store is not None:
            _mem_loaded = await memory_store.startup_load()
        # v6.0 Phase 3 — connect MCP servers + discover tools
        _mcp_tools = 0
        if mcp_registry is not None:
            await mcp_registry.startup()
            _mcp_tools = len(mcp_registry.list_tools())
        # v6.0 — start EventBus delivery worker + load persisted events
        _eb_loaded = 0
        if event_bus is not None:
            _eb_loaded = await event_bus.load_persisted_events()
            await event_bus.start()
        # v6.0 Phase 2 — start Scheduler + load static schedule entries from config
        _sched_count = 0
        if scheduler is not None:
            # Load static entries defined in node.yaml schedule: section
            for sched_dict in (config.schedule or []):
                try:
                    entry = ScheduleEntry(**sched_dict)
                    await scheduler.add_entry(entry)
                    _sched_count += 1
                except Exception as exc:
                    logger.warning("Skipping invalid schedule entry: %s — %s", sched_dict, exc)
            await scheduler.start()
        logger.info(
            "v6.0 startup — job cleanup active, trusted_nodes=%s, "
            "swept %d orphaned upload(s), %d stale session(s), "
            "loaded %d credential session(s), llm=%s, "
            "event_bus=%s, persisted_events=%d, "
            "scheduler=%s, static_schedules=%d, "
            "sessions=%s(loaded=%d), memory=%s(entries=%d), mcp=%s(tools=%d)",
            config.trusted_nodes, swept_uploads, swept_sessions,
            _cred_sessions, "enabled" if config.llm_enabled else "disabled",
            "enabled" if event_bus is not None else "disabled", _eb_loaded,
            "enabled" if scheduler is not None else "disabled", _sched_count,
            config.session.backend, _sess_loaded,
            "enabled" if memory_store is not None else "disabled", _mem_loaded,
            "enabled" if mcp_registry is not None else "disabled", _mcp_tools,
        )
        yield
        job_manager.stop_cleanup_loop()
        # v6.0 Phase 2 — stop Scheduler
        if scheduler is not None:
            await scheduler.stop()
        # v6.0 — stop EventBus
        if event_bus is not None:
            await event_bus.stop()
        # v6.0 Phase 3 — disconnect MCP servers
        if mcp_registry is not None:
            await mcp_registry.shutdown()
        # v5.13 — final credential flush before exit
        await credential_store.stop_flush_task()
        logger.info("Shutdown complete")

    app = FastAPI(
        title=f"Mesh Node — {config.node_id}",
        version="6.0.0",
        lifespan=lifespan,
    )

    # -- v5.10: worker_agent ref (mutable dict; node_runtime injects agent after startup) --
    worker_agent_ref: dict = {"agent": None}
    app.state.worker_agent_ref = worker_agent_ref
    app.state.node_registry = node_registry  # v5.12: exposed for WorkerAgent liveness check
    app.state.event_bus = event_bus           # v6.0: exposed for testing
    app.state.channel_registry = channel_registry  # v6.0
    app.state.scheduler = scheduler           # v6.0 Phase 2
    app.state.memory_store = memory_store     # v6.0 Phase 3
    app.state.mcp_registry = mcp_registry     # v6.0 Phase 3
    app.state.conversation_store = conversation_store  # v6.0 Phase 3

    # -- Auth (v5.13: supports single auth_token or allowed_tokens list) ------
    _allowed = list(config.allowed_tokens) if config.allowed_tokens else None
    app.add_middleware(
        AuthMiddleware,
        auth_token=config.auth_token,
        allowed_tokens=_allowed,
    )

    # -- CORS ---------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- request logging middleware -----------------------------------------
    @app.middleware("http")
    async def log_requests(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:8])
        logger.info("[%s] %s %s", request_id, request.method, request.url.path)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # -- lifecycle ----------------------------------------------------------

    # =======================================================================
    # CORE ENDPOINTS (unchanged interface for LLM)
    # =======================================================================

    @app.post("/action")
    async def action_endpoint(req: ActionRequest, http_req: Request) -> JSONResponse:
        """Receive an action request — routes via push or pull automatically.

        v5.6: task_id and trace are optional — model validator fills defaults.
        v5.11: extracts caller_token from Authorization header for policy enforcement.
        """
        if req.task_id and req.task_id.startswith("task-") and len(req.task_id) == 17:
            logger.debug("Auto-generated task_id=%s for action=%s", req.task_id, req.payload.action)
        # v5.11: attach caller_token so executor can enforce caller_policies
        auth_header = http_req.headers.get("Authorization", "")
        caller_token = auth_header.removeprefix("Bearer ").strip() or None
        req = req.model_copy(update={"caller_token": caller_token})
        try:
            result = await gateway_router.route(req)
        except SchemaValidationError as exc:
            return JSONResponse(
                content=ErrorResponse(
                    error=f"SCHEMA_VALIDATION_ERROR: {exc}",
                    node_id=config.node_id,
                ).model_dump(),
                status_code=422,
            )

        if isinstance(result, ErrorResponse):
            return JSONResponse(content=result.model_dump(), status_code=400)
        if isinstance(result, AsyncActionResponse):
            return JSONResponse(content=result.model_dump(), status_code=202)
        return JSONResponse(content=result.model_dump(), status_code=200)

    @app.get("/result/{job_id}")
    async def result_endpoint(job_id: str) -> JSONResponse:
        """Poll job status — transparent for push/pull/local jobs."""
        result = await gateway_router.route_result(job_id)
        if isinstance(result, ErrorResponse):
            return JSONResponse(content=result.model_dump(), status_code=404)
        return JSONResponse(content=result.model_dump(), status_code=200)

    @app.get("/resolve/{node_id}")
    async def resolve_endpoint(node_id: str) -> JSONResponse:
        try:
            address = await resolver.resolve(node_id)
            return JSONResponse(
                content=ResolveResponse(node_id=node_id, address=address).model_dump(),
                status_code=200,
            )
        except NodeNotFoundError:
            return JSONResponse(
                content=ErrorResponse(error="NODE_NOT_FOUND", node_id=node_id).model_dump(),
                status_code=404,
            )

    @app.get("/skills")
    async def skills_endpoint() -> Response:
        path = Path(skills_path) if skills_path else None
        if path and path.exists():
            return PlainTextResponse(
                content=path.read_text(encoding="utf-8"),
                media_type="text/markdown",
            )
        return PlainTextResponse(
            content=f"# Node: {config.node_id}\n\nNo skills defined.",
            media_type="text/markdown",
        )

    @app.get("/health")
    async def health_endpoint() -> JSONResponse:
        active = await job_manager.active_count()
        queue_depths = await job_queue.all_queue_depths()
        resp = HealthResponse(
            node_id=config.node_id,
            uptime_seconds=round(time.time() - start_time, 2),
            actions_loaded=len(registry),
            jobs_active=active,
            queue_depths=queue_depths,
        )
        data = resp.model_dump()
        # v6.0: include EventBus info
        if event_bus is not None:
            subs = await event_bus.get_subscriptions()
            events = await event_bus.get_events(limit=1)  # just to get count cheaply
            all_events = await event_bus.get_events(limit=config.event_bus.max_log_size)
            data["event_bus"] = {
                "enabled": True,
                "subscriptions": len(subs),
                "events_in_log": len(all_events),
            }
            if channel_registry is not None:
                data["channel"] = {
                    "channel_id": channel_registry.get_channel_id(),
                    "members": channel_registry.member_count(),
                }
        else:
            data["event_bus"] = {"enabled": False}
        return JSONResponse(content=data, status_code=200)

    @app.get("/ping")
    async def ping_endpoint() -> JSONResponse:
        """Reachability check — called by gateway before deciding push vs pull.
        Always exempt from auth so gateway can probe workers freely.
        """
        return JSONResponse(
            content={"node_id": config.node_id, "pong": True},
            status_code=200,
        )

    # =======================================================================
    # v5.3 — NODE REGISTRY ENDPOINTS (worker → gateway)
    # =======================================================================

    @app.post("/nodes/register")
    async def register_node_endpoint(req: NodeRegistrationRequest) -> JSONResponse:
        """Worker registers itself with this gateway.

        v5.10: accepts BGP-style route advertisement in the request body.
        If this node is itself a worker (has gateway_address configured), it
        re-advertises the new sub-routes upward to its own gateway.
        """
        await node_registry.register(
            node_id=req.node_id,
            address=req.address,
            actions=req.actions,
            advertise_routes=req.advertise_routes,
            capabilities=req.capabilities,
            action_specs=getattr(req, 'action_specs', None),
            sub_route_specs=getattr(req, 'sub_route_specs', None) or None,  # v5.13
        )
        # v5.12 — BGP WITHDRAW: remove routes that are no longer advertised
        if req.advertise_routes is not None:
            withdrawn = await node_registry.withdraw_routes(
                via_node_id=req.node_id,
                keep_routes=req.advertise_routes,
            )
            if withdrawn:
                logger.info(
                    "Route withdrawal from %s: removed %s",
                    req.node_id, withdrawn,
                )

        # v5.10: if the registering node advertised sub-routes, propagate upward
        # by calling add_sub_route() on the local WorkerAgent (if any).
        agent = worker_agent_ref.get("agent")
        if agent is not None and (req.actions or req.advertise_routes):
            # Build full capability map for this registering node:
            # its own actions + whatever sub-routes it advertised
            all_reachable: dict[str, list[str]] = {}
            all_reachable[req.node_id] = list(req.actions)
            for sub_id in req.advertise_routes:
                all_reachable[sub_id] = (req.capabilities or {}).get(sub_id, [])

            # Advertise the registering node (+ its sub-tree) upward
            for advertised_id, advertised_actions in all_reachable.items():
                try:
                    # v5.13: pass action_specs for this sub-node if available
                    _sub_node_specs = (req.sub_route_specs or {}).get(advertised_id)
                    # For the registering node itself, its own action_specs apply
                    if advertised_id == req.node_id:
                        _sub_node_specs = _sub_node_specs or (
                            {k: v for k, v in req.action_specs.items()}
                            if req.action_specs else None
                        )
                    await agent.add_sub_route(
                        advertised_id,
                        advertised_actions,
                        action_specs=_sub_node_specs,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to propagate route %s upward: %s", advertised_id, exc
                    )

        return JSONResponse(
            content={
                "node_id": req.node_id,
                "registered": True,
                "routes_acknowledged": req.advertise_routes,
                "message": f"Node {req.node_id} registered successfully",
            },
            status_code=200,
        )

    @app.get("/capabilities")
    async def capabilities_endpoint() -> JSONResponse:
        """Return full capability tree rooted at this node.

        Includes this node's own actions plus all reachable nodes with their
        actions and the next-hop required to reach them. Used by:
          - Claude Web (via GET /capabilities on node-0) to discover the full mesh
          - IntentHandler to build a rich system prompt
          - Operators for topology debugging

        v5.10: capabilities propagate via BGP-style advertisement at registration.
        """
        own_actions = list(registry.keys())
        reachable = node_registry.build_capability_tree(own_actions)
        resp = CapabilityTreeResponse(
            node_id=config.node_id,
            actions=own_actions,
            reachable=reachable,
        )
        return JSONResponse(content=resp.model_dump(), status_code=200)

    @app.post("/nodes/{node_id}/heartbeat")
    async def heartbeat_endpoint(node_id: str, req: HeartbeatRequest) -> JSONResponse:
        """Worker sends periodic heartbeat to this gateway."""
        accepted = await node_registry.heartbeat(node_id)
        if not accepted:
            return JSONResponse(
                content={"node_id": node_id, "acknowledged": False,
                         "error": "Node not trusted"},
                status_code=403,
            )
        return JSONResponse(
            content={"node_id": node_id, "acknowledged": True},
            status_code=200,
        )

    @app.get("/nodes")
    async def list_nodes_endpoint() -> JSONResponse:
        """List all registered trusted nodes and their status."""
        nodes = await node_registry.list_nodes()
        return JSONResponse(
            content={"nodes": [n.model_dump() for n in nodes]},
            status_code=200,
        )

    # =======================================================================
    # v5.3 — JOB QUEUE ENDPOINTS (worker → gateway)
    # =======================================================================

    @app.get("/jobs/poll")
    async def poll_jobs_endpoint(
        node_id: str = Query(..., description="Calling worker's node ID"),
    ) -> JSONResponse:
        """Worker polls for queued jobs addressed to it."""
        if not node_registry.is_trusted(node_id):
            return JSONResponse(
                content={"error": f"UNTRUSTED_NODE: {node_id}"},
                status_code=403,
            )
        jobs = await job_queue.poll(node_id)
        return JSONResponse(
            content={"node_id": node_id, "jobs": [j.model_dump() for j in jobs]},
            status_code=200,
        )

    @app.post("/jobs/{job_id}/claim")
    async def claim_job_endpoint(job_id: str, req: ClaimRequest) -> JSONResponse:
        """Worker claims a job before executing it (prevents double-execution)."""
        if not node_registry.is_trusted(req.node_id):
            return JSONResponse(
                content={"job_id": job_id, "claimed": False,
                         "reason": f"UNTRUSTED_NODE: {req.node_id}"},
                status_code=403,
            )
        claimed_job = await job_queue.claim(job_id, req.node_id)
        if claimed_job is None:
            return JSONResponse(
                content={"job_id": job_id, "claimed": False,
                         "reason": "Not found or already claimed"},
                status_code=409,
            )
        # Transition gateway job state: QUEUED → RUNNING
        await job_manager.update_job(job_id, status=JobStatus.RUNNING, progress=0)
        return JSONResponse(
            content={"job_id": job_id, "claimed": True},
            status_code=200,
        )

    @app.post("/jobs/{job_id}/result")
    async def report_result_endpoint(job_id: str, req: JobResultReport) -> JSONResponse:
        """Worker reports execution result back to the gateway."""
        if not node_registry.is_trusted(req.node_id):
            return JSONResponse(
                content={"error": f"UNTRUSTED_NODE: {req.node_id}"},
                status_code=403,
            )
        new_status = JobStatus.COMPLETED if req.status == "completed" else JobStatus.FAILED
        job = await job_manager.update_job(
            job_id,
            status=new_status,
            progress=100 if new_status == JobStatus.COMPLETED else None,
            output=req.output,
            error=req.error,
        )
        if job is None:
            return JSONResponse(
                content={"error": f"Job not found: {job_id}"},
                status_code=404,
            )
        # Clean up from pull queue
        await job_queue.remove_from_queue(job_id, req.node_id)
        logger.info(
            "Job %s completed by worker %s → %s",
            job_id, req.node_id, new_status.value,
        )
        return JSONResponse(
            content={"job_id": job_id, "acknowledged": True},
            status_code=200,
        )


    # =======================================================================
    # v5.8 — FILE UPLOAD / DOWNLOAD ENDPOINTS
    # =======================================================================

    @app.post("/upload")
    async def upload_endpoint(request: Request, file: UploadFile) -> JSONResponse:
        """Accept a file upload and store it on this node.

        Workers call this from execute_command via curl:
            curl -X POST $GATEWAY/upload \
              -H "Authorization: Bearer $TOKEN" \
              -F "file=@/path/to/backup.sql.gz"

        Returns:
            201 — UploadResponse with file_id and download_url
            413 — FILE_TOO_LARGE
            400 — bad request (no file)
        """
        # Early size check from Content-Length if available
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > config.upload_max_size_bytes:
                    return JSONResponse(
                        content={
                            "error": "FILE_TOO_LARGE",
                            "max_size_mb": config.upload_max_size_mb,
                        },
                        status_code=413,
                    )
            except ValueError:
                pass

        data = await file.read()
        if not data:
            return JSONResponse(
                content={"error": "EMPTY_FILE"},
                status_code=400,
            )

        # Identify uploader from X-Node-ID header (set by workers) or fall back
        uploader = request.headers.get("x-node-id") or request.headers.get("x-request-id")

        try:
            entry = await upload_manager.store(
                filename=file.filename or "upload",
                data=data,
                uploader=uploader,
            )
        except FileTooLargeError as exc:
            return JSONResponse(
                content={"error": "FILE_TOO_LARGE", "detail": str(exc)},
                status_code=413,
            )

        resp = UploadResponse(
            file_id=entry.file_id,
            filename=entry.filename,
            size_bytes=entry.size_bytes,
            ttl_seconds=config.upload_ttl_seconds,
            download_url=f"/download/{entry.file_id}",
        )
        return JSONResponse(content=resp.model_dump(), status_code=201)

    @app.get("/download/{file_id}")
    async def download_endpoint(file_id: str) -> Response:
        """Stream a previously uploaded file back to the caller.

        Workers call this from execute_command via curl:
            curl -O -J $GATEWAY/download/{file_id} \
              -H "Authorization: Bearer $TOKEN"

        Returns:
            200 — file stream with Content-Disposition: attachment
            404 — FILE_NOT_FOUND or expired
        """
        result = await upload_manager.get(file_id)
        if result is None:
            return JSONResponse(
                content={"error": "FILE_NOT_FOUND", "file_id": file_id},
                status_code=404,
            )
        path, entry = result
        return FileResponse(
            path=str(path),
            filename=entry.filename,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{entry.filename}"'},
        )

    @app.get("/files")
    async def list_files_endpoint() -> JSONResponse:
        """List all non-expired uploads on this node.

        Useful for Claude to inspect what files are staged on the gateway
        before routing a download to a worker.
        """
        now = time.time()
        entries = await upload_manager.list_files()
        file_infos = [
            FileInfo(
                file_id=e.file_id,
                filename=e.filename,
                size_bytes=e.size_bytes,
                created_at=e.created_at,
                age_seconds=round(now - e.created_at, 1),
                uploader=e.uploader,
            )
            for e in entries
        ]
        resp = FileListResponse(files=file_infos, total=len(file_infos))
        return JSONResponse(content=resp.model_dump(), status_code=200)

    @app.delete("/files/{file_id}")
    async def delete_file_endpoint(file_id: str) -> JSONResponse:
        """Delete an uploaded file before its TTL expires.

        Claude calls this after a successful restore to free disk space.
        """
        deleted = await upload_manager.delete(file_id)
        if not deleted:
            return JSONResponse(
                content={"error": "FILE_NOT_FOUND", "file_id": file_id},
                status_code=404,
            )
        return JSONResponse(
            content={"file_id": file_id, "deleted": True},
            status_code=200,
        )



    # =======================================================================
    # v5.9 — INTENT / AGENT LOOP ENDPOINTS
    # =======================================================================

    @app.post("/intent")
    async def intent_endpoint(req: IntentRequest) -> JSONResponse:
        """Accept a natural-language prompt and autonomously execute it.

        The agent loop runs internally:
          1. LLM decides which mesh action to call
          2. Action executes on the target node
          3. Result fed back to LLM
          4. Repeat until LLM returns a plain reply

        Use session_id (e.g. Telegram chat_id) to maintain conversation context
        across multiple turns.

        Returns:
            200 — IntentResponse with reply, actions_taken, turns, tokens_used
            503 — LLM not configured on this node
        """
        if intent_handler is None:
            return JSONResponse(
                content={
                    "error": "LLM_NOT_CONFIGURED",
                    "detail": (
                        "This node has no LLM configured. "
                        "Set llm_api_key and (optionally) llm_base_url in node.yaml "
                        "to enable POST /intent."
                    ),
                },
                status_code=503,
            )

        resp: IntentResponse = await intent_handler.handle(req)
        return JSONResponse(content=resp.model_dump(), status_code=200)

    @app.get("/sessions/{session_id}")
    async def get_session_endpoint(session_id: str) -> JSONResponse:
        """Retrieve conversation history for a session.

        Returns:
            200 — SessionInfo with full message history
            404 — session not found or expired
        """
        session = await conversation_store.get(session_id)
        if session is None:
            return JSONResponse(
                content={"error": "SESSION_NOT_FOUND", "session_id": session_id},
                status_code=404,
            )

        now = time.time()
        messages = [
            ConversationMessage(
                role=m.get("role", ""),
                content=m.get("content") or "",
                tool_call_id=m.get("tool_call_id"),
                tool_name=m.get("name"),
                timestamp=0.0,
            )
            for m in session.messages
            if m.get("role") in ("user", "assistant", "tool")
        ]

        info = SessionInfo(
            session_id=session.session_id,
            created_at=session.created_at,
            last_active=session.last_active,
            age_seconds=round(now - session.created_at, 1),
            turn_count=session.turn_count,
            messages=messages,
        )
        return JSONResponse(content=info.model_dump(), status_code=200)

    @app.delete("/sessions/{session_id}")
    async def delete_session_endpoint(session_id: str) -> JSONResponse:
        """Delete a session and its conversation history.

        Call this to reset context (e.g. user sends /reset in Telegram).

        Returns:
            200 — deleted successfully
            404 — session not found
        """
        deleted = await conversation_store.delete(session_id)
        if not deleted:
            return JSONResponse(
                content={"error": "SESSION_NOT_FOUND", "session_id": session_id},
                status_code=404,
            )
        return JSONResponse(
            content={"session_id": session_id, "deleted": True},
            status_code=200,
        )

    @app.get("/sessions")
    async def list_sessions_endpoint() -> JSONResponse:
        """List all active (non-expired) sessions."""
        now = time.time()
        sessions = await conversation_store.list_sessions()
        return JSONResponse(
            content={
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "created_at": s.created_at,
                        "last_active": s.last_active,
                        "age_seconds": round(now - s.created_at, 1),
                        "turn_count": s.turn_count,
                    }
                    for s in sessions
                ],
                "total": len(sessions),
            },
            status_code=200,
        )

    # =======================================================================
    # v6.0 Phase 3 — SESSION / MEMORY / MCP ENDPOINTS
    # =======================================================================

    @app.post("/sessions/{session_id}/clear")
    async def clear_session_messages_endpoint(session_id: str) -> JSONResponse:
        """Clear conversation history for a session (keep session + memory).

        Useful for resetting the agent context without losing the session ID
        or associated memories.

        Returns:
            200 — messages cleared
            404 — session not found
        """
        if isinstance(conversation_store, PersistentSessionStore):
            cleared = await conversation_store.clear_messages(session_id)
        else:
            # In-memory store: get session, clear messages
            session = await conversation_store.get(session_id)
            if session is not None:
                session.messages.clear()
                cleared = True
            else:
                cleared = False

        if not cleared:
            return JSONResponse(
                content={"error": "SESSION_NOT_FOUND", "session_id": session_id},
                status_code=404,
            )
        return JSONResponse(
            content={"session_id": session_id, "cleared": True},
            status_code=200,
        )

    @app.get("/memory")
    async def list_memory_endpoint(
        query: str | None = None,
        scope: str | None = None,
        entry_type: str | None = None,
        limit: int = 100,
    ) -> JSONResponse:
        """List agent memory entries.

        Query params:
            query:      Substring search (key + value)
            scope:      Filter by scope ("global", "session:{id}")
            entry_type: "fact" | "narrative"
            limit:      Max results (default 100)

        Returns 503 if memory not enabled.
        """
        if memory_store is None:
            return JSONResponse(
                content={"error": "MEMORY_NOT_ENABLED"},
                status_code=503,
            )
        entries = await memory_store.recall(
            query=query, scope=scope, entry_type=entry_type, limit=limit
        )
        return JSONResponse(
            content={
                "entries": [e.to_dict() for e in entries],
                "total": len(entries),
                "memory_count": memory_store.count,
            },
            status_code=200,
        )

    @app.post("/memory")
    async def write_memory_endpoint(request: Request) -> JSONResponse:
        """Store or update a memory entry.

        Body: {key, value, entry_type?, scope?, session_id?}

        Returns 503 if memory not enabled.
        """
        if memory_store is None:
            return JSONResponse(
                content={"error": "MEMORY_NOT_ENABLED"},
                status_code=503,
            )
        body = await request.json()
        key = body.get("key")
        value = body.get("value")
        if not key or value is None:
            return JSONResponse(
                content={"error": "MISSING_FIELDS", "detail": "key and value are required"},
                status_code=400,
            )
        entry = await memory_store.remember(
            key=str(key),
            value=str(value),
            entry_type=body.get("entry_type", "fact"),
            scope=body.get("scope", "global"),
            session_id=body.get("session_id"),
            source=body.get("source", "api"),
        )
        return JSONResponse(content=entry.to_dict(), status_code=201)

    @app.delete("/memory")
    async def delete_memory_endpoint(request: Request) -> JSONResponse:
        """Remove memory entries.

        Body: {key?, scope?, session_id?}
        At least one field required.

        Returns 503 if memory not enabled.
        """
        if memory_store is None:
            return JSONResponse(
                content={"error": "MEMORY_NOT_ENABLED"},
                status_code=503,
            )
        body = await request.json()
        key = body.get("key")
        scope = body.get("scope")
        session_id = body.get("session_id")
        if not any([key, scope, session_id]):
            return JSONResponse(
                content={
                    "error": "MISSING_FIELDS",
                    "detail": "At least one of key, scope, session_id required",
                },
                status_code=400,
            )
        removed = await memory_store.forget(key=key, scope=scope, session_id=session_id)
        return JSONResponse(content={"removed": removed}, status_code=200)

    @app.get("/mcp/servers")
    async def list_mcp_servers_endpoint() -> JSONResponse:
        """List all configured MCP servers and their connection status.

        Returns 503 if no MCP servers configured.
        """
        if mcp_registry is None:
            return JSONResponse(
                content={"error": "MCP_NOT_CONFIGURED"},
                status_code=503,
            )
        return JSONResponse(
            content={
                "servers": mcp_registry.list_servers(),
                "total": len(mcp_registry.list_servers()),
            },
            status_code=200,
        )

    @app.get("/mcp/tools")
    async def list_mcp_tools_endpoint() -> JSONResponse:
        """List all tools discovered from connected MCP servers.

        Returns 503 if no MCP servers configured.
        """
        if mcp_registry is None:
            return JSONResponse(
                content={"error": "MCP_NOT_CONFIGURED"},
                status_code=503,
            )
        tools = mcp_registry.list_tools()
        return JSONResponse(
            content={"tools": tools, "total": len(tools)},
            status_code=200,
        )


    # =======================================================================
    # BOOTSTRAP + DISTRIBUTION ENDPOINTS (unchanged from v5.2)
    # =======================================================================

    @app.post("/bootstrap")
    async def bootstrap_endpoint(req: BootstrapRequest) -> JSONResponse:
        result = await bootstrap_engine.bootstrap(req)
        status_code = 201 if result.status == BootstrapStatus.COMPLETED else 500
        return JSONResponse(
            content={
                "node_id": result.node_id,
                "status": result.status.value,
                "address": result.address,
                "pid": result.pid,
                "error": result.error,
                "steps_completed": result.steps_completed,
                "steps_rolled_back": result.steps_rolled_back,
            },
            status_code=status_code,
        )

    @app.get("/setup.sh")
    async def setup_sh_endpoint(request: Request) -> Response:
        candidates = [
            Path(runtime_base_dir or ".") / "static" / "setup.sh",
            Path(runtime_base_dir or ".") / "setup.sh",
            Path(__file__).parent.parent / "static" / "setup.sh",
        ]
        for path in candidates:
            if path.exists():
                content = path.read_text(encoding="utf-8")
                host = request.headers.get("host", "")
                scheme = request.headers.get("x-forwarded-proto", "https")
                if host:
                    content = content.replace("__GATEWAY_PUBLIC_URL__", f"{scheme}://{host}")
                return PlainTextResponse(
                    content=content,
                    media_type="text/x-shellscript",
                    headers={"Content-Disposition": "inline; filename=setup.sh"},
                )
        return PlainTextResponse("#!/bin/bash\necho 'setup.sh not found'\nexit 1\n", status_code=404)

    @app.get("/runtime-bundle")
    async def runtime_bundle_endpoint() -> Response:
        base = Path(runtime_base_dir or ".")
        items_to_bundle = [
            ("runtime", base / "runtime"),
            ("seed", base / "seed"),
            ("static", base / "static"),
            ("node_runtime.py", base / "node_runtime.py"),
            ("requirements.txt", base / "requirements.txt"),
            ("pyproject.toml", base / "pyproject.toml"),
        ]
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for arcname, path in items_to_bundle:
                if path.exists():
                    tar.add(str(path), arcname=arcname)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/gzip",
            headers={"Content-Disposition": "attachment; filename=mesh-runtime.tar.gz"},
        )

    # -----------------------------------------------------------------------
    # v6.0 Phase 2 — Scheduler endpoints
    # -----------------------------------------------------------------------

    @app.post("/schedule")
    async def create_schedule_endpoint(entry: ScheduleEntry) -> JSONResponse:
        """Register a new schedule trigger."""
        if scheduler is None:
            return JSONResponse({"error": "Scheduler not enabled"}, status_code=503)
        schedule_id = await scheduler.add_entry(entry)
        return JSONResponse({"schedule_id": schedule_id, "trigger_type": entry.trigger_type}, status_code=201)

    @app.get("/schedule")
    async def list_schedule_endpoint() -> JSONResponse:
        """List all schedule entries."""
        if scheduler is None:
            return JSONResponse({"error": "Scheduler not enabled"}, status_code=503)
        entries = await scheduler.list_entries()
        return JSONResponse({"entries": [e.model_dump() for e in entries], "total": len(entries)})

    @app.delete("/schedule/{schedule_id}")
    async def delete_schedule_endpoint(schedule_id: str) -> JSONResponse:
        """Cancel and remove a schedule entry."""
        if scheduler is None:
            return JSONResponse({"error": "Scheduler not enabled"}, status_code=503)
        removed = await scheduler.remove_entry(schedule_id)
        if not removed:
            return JSONResponse({"error": f"Schedule not found: {schedule_id}"}, status_code=404)
        return JSONResponse({"schedule_id": schedule_id, "removed": True})

    @app.post("/schedule/{schedule_id}/trigger")
    async def trigger_schedule_endpoint(schedule_id: str) -> JSONResponse:
        """Manually trigger a schedule entry (debug/test)."""
        if scheduler is None:
            return JSONResponse({"error": "Scheduler not enabled"}, status_code=503)
        dispatched = await scheduler.trigger_manual(schedule_id)
        if not dispatched:
            return JSONResponse({"error": f"Schedule not found: {schedule_id}"}, status_code=404)
        return JSONResponse({"schedule_id": schedule_id, "triggered": True})

    @app.patch("/schedule/{schedule_id}")
    async def patch_schedule_endpoint(schedule_id: str, req: SchedulePatchRequest) -> JSONResponse:
        """Partially update a schedule entry (enable/disable, change interval, etc.)."""
        if scheduler is None:
            return JSONResponse({"error": "Scheduler not enabled"}, status_code=503)
        updates = req.model_dump(exclude_none=True)
        updated = await scheduler.patch_entry(schedule_id, updates)
        if updated is None:
            return JSONResponse({"error": f"Schedule not found: {schedule_id}"}, status_code=404)
        return JSONResponse(updated.model_dump())

    # -----------------------------------------------------------------------
    # v6.0 — EventBus endpoints
    # -----------------------------------------------------------------------

    @app.post("/emit")
    async def emit_endpoint(req: EmitRequest) -> JSONResponse:
        """Publish an event onto the EventBus."""
        if event_bus is None:
            return JSONResponse({"error": "EventBus not enabled"}, status_code=503)
        event = Event(
            event_type=req.event_type,
            source_node=req.source_node or config.node_id,
            payload=req.payload,
            correlation_id=req.correlation_id,
            reply_to=req.reply_to,
        )
        matched = await event_bus.emit(event)
        return JSONResponse(EmitResponse(
            event_id=event.event_id,
            matched_subscriptions=matched,
        ).model_dump())

    @app.post("/subscribe")
    async def subscribe_endpoint(req: SubscribeRequest) -> JSONResponse:
        """Register a subscription on the EventBus."""
        if event_bus is None:
            return JSONResponse({"error": "EventBus not enabled"}, status_code=503)
        sub = Subscription(
            subscriber_node=req.subscriber_node,
            callback_action=req.callback_action,
            callback_params_template=req.callback_params_template,
            event_type_pattern=req.event_type_pattern,
            source_node=req.source_node,
            payload_filter=req.payload_filter,
            debounce_seconds=req.debounce_seconds,
            max_deliveries=req.max_deliveries,
            description=req.description,
        )
        sub_id = await event_bus.subscribe(sub)
        return JSONResponse(SubscribeResponse(
            sub_id=sub_id,
            event_type_pattern=sub.event_type_pattern,
            subscriber_node=sub.subscriber_node,
        ).model_dump(), status_code=201)

    @app.delete("/subscriptions/{sub_id}")
    async def unsubscribe_endpoint(sub_id: str) -> JSONResponse:
        """Cancel a subscription."""
        if event_bus is None:
            return JSONResponse({"error": "EventBus not enabled"}, status_code=503)
        removed = await event_bus.unsubscribe(sub_id)
        if not removed:
            return JSONResponse({"error": f"Subscription not found: {sub_id}"}, status_code=404)
        return JSONResponse({"sub_id": sub_id, "removed": True})

    @app.get("/subscriptions")
    async def list_subscriptions_endpoint() -> JSONResponse:
        """List all active subscriptions."""
        if event_bus is None:
            return JSONResponse({"error": "EventBus not enabled"}, status_code=503)
        subs = await event_bus.get_subscriptions()
        return JSONResponse({"subscriptions": [s.model_dump() for s in subs], "total": len(subs)})

    @app.get("/events")
    async def list_events_endpoint(
        event_type: str | None = Query(default=None),
        source_node: str | None = Query(default=None),
        since: float | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=10000),
    ) -> JSONResponse:
        """Replay the event log with optional filters."""
        if event_bus is None:
            return JSONResponse({"error": "EventBus not enabled"}, status_code=503)
        events = await event_bus.get_events(
            event_type=event_type,
            source_node=source_node,
            since=since,
            limit=limit,
        )
        return JSONResponse({"events": [e.model_dump() for e in events], "total": len(events)})

    return app
