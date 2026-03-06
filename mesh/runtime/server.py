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
)
from runtime.node_registry import NodeRegistry
from runtime.resolver import NodeNotFoundError, NodeResolver
from runtime.schema_validator import ActionSchemaValidator, SchemaRegistry, SchemaValidationError
from runtime.upload_manager import FileTooLargeError, UploadManager
from runtime.conversation_store import ConversationStore
from runtime.credential_store import CredentialStore
from runtime.intent_handler import IntentHandler

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
    executor = ActionExecutor(
        registry, job_manager, config.node_id,
        schema_validator=schema_validator,
        llm_client=llm_client,
        caller_policies=list(config.caller_policies),
    )

    # -- v5.3: gateway state ------------------------------------------------
    node_registry = NodeRegistry(
        trusted_node_ids=config.trusted_nodes,
        heartbeat_timeout_seconds=config.heartbeat_timeout_seconds,
        ping_timeout_seconds=config.ping_timeout_seconds,
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

    # -- v5.9: conversation store + intent handler --------------------------
    conversation_store = ConversationStore(
        ttl_seconds=config.session_ttl_seconds,
    )
    # v5.13 — encrypted session credential store
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
        )
        logger.info("IntentHandler ready — POST /intent enabled")
    else:
        logger.info("LLM not configured — POST /intent will return 503")
    start_time = time.time()

    # -- lifespan (replaces deprecated on_event) ----------------------------
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        job_manager.start_cleanup_loop()
        swept_uploads = await upload_manager.sweep_expired()
        swept_sessions = await conversation_store.sweep_expired()
        # v5.13 — load persisted credentials + start background flusher
        _cred_sessions = await credential_store.load()
        credential_store.start_flush_task()
        logger.info(
            "v5.13 startup — job cleanup active, trusted_nodes=%s, "
            "swept %d orphaned upload(s), %d stale session(s), "
            "loaded %d credential session(s), llm=%s",
            config.trusted_nodes, swept_uploads, swept_sessions,
            _cred_sessions, "enabled" if config.llm_enabled else "disabled",
        )
        yield
        job_manager.stop_cleanup_loop()
        # v5.13 — final credential flush before exit
        await credential_store.stop_flush_task()
        logger.info("Shutdown complete")

    app = FastAPI(
        title=f"Mesh Node — {config.node_id}",
        version="5.13.0",
        lifespan=lifespan,
    )

    # -- v5.10: worker_agent ref (mutable dict; node_runtime injects agent after startup) --
    worker_agent_ref: dict = {"agent": None}
    app.state.worker_agent_ref = worker_agent_ref
    app.state.node_registry = node_registry  # v5.12: exposed for WorkerAgent liveness check

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
        return JSONResponse(content=resp.model_dump(), status_code=200)

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

    return app
