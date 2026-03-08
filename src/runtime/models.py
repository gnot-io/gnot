"""Pydantic models for the Execution Mesh request/response envelope.

Defines all data structures used across the mesh runtime including
action requests, responses, job status, node resolution, and the
v5.3 push/pull job model (node registry, heartbeat, job queue).

v5.6 changes:
  - ActionRequest.task_id  — optional, server auto-generates uuid4 if absent.
  - ActionRequest.trace    — optional, defaults to TraceInfo(hop_count=0, route_path=[]).
    Claude (or any caller) only needs to supply target_node_id + payload.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS: int = 60
DEFAULT_MAX_HOP: int = 10
DEFAULT_CACHE_TTL_SECONDS: int = 300


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    """Valid job lifecycle states."""

    ACCEPTED = "accepted"
    QUEUED = "queued"       # v5.3: sitting in gateway queue, waiting for worker pull
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class JobMode(str, Enum):
    """How the gateway delivered the job to the worker."""

    PUSH = "push"           # gateway proxied directly to reachable worker
    PULL = "pull"           # worker polled and claimed from gateway queue
    LOCAL = "local"         # executed on gateway itself


class NodeStatus(str, Enum):
    """Reachability status of a registered worker node."""

    ONLINE = "online"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TraceInfo(BaseModel):
    hop_count: int = 0
    route_path: list[str] = Field(default_factory=list)


class ActionPayload(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


class ActionRequest(BaseModel):
    target_node_id: str
    task_id: str | None = None          # v5.6: optional — server generates uuid4 if absent
    trace: TraceInfo | None = None      # v5.6: optional — defaults to hop_count=0, route_path=[]
    payload: ActionPayload
    # v5.11 — caller credentials: forwarded as-is, never logged, never stored in job params
    caller_credentials: dict[str, str] = Field(default_factory=dict)
    caller_token: str | None = None     # extracted from Authorization header by gateway

    @model_validator(mode="after")
    def _fill_defaults(self) -> "ActionRequest":
        if self.task_id is None:
            self.task_id = f"task-{uuid.uuid4().hex[:12]}"
        if self.trace is None:
            self.trace = TraceInfo()
        return self


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class SyncActionResponse(BaseModel):
    task_id: str
    status: str = "completed"
    output: dict[str, Any]


class AsyncActionResponse(BaseModel):
    task_id: str
    job_id: str
    status: str = "accepted"
    estimated_completion_seconds: int = DEFAULT_TIMEOUT_SECONDS


class JobStatusResponse(BaseModel):
    task_id: str
    job_id: str
    status: str
    progress: int | None = None
    output: dict[str, Any] | None = None
    error: str | None = None


class ResolveResponse(BaseModel):
    node_id: str
    address: str


class ErrorResponse(BaseModel):
    error: str
    node_id: str | None = None


class HealthResponse(BaseModel):
    node_id: str
    status: str = "healthy"
    uptime_seconds: float
    actions_loaded: int
    jobs_active: int
    queue_depths: dict[str, int] = Field(default_factory=dict)  # v5.7: pull-queue depth per node


# ---------------------------------------------------------------------------
# v5.3 — Node Registry models
# ---------------------------------------------------------------------------

class NodeRegistrationRequest(BaseModel):
    """Sent by a worker to POST /nodes/register on its gateway."""
    node_id: str
    address: str | None = None
    # v5.10 — BGP-style route advertisement
    actions: list[str] = Field(default_factory=list)
    advertise_routes: list[str] = Field(default_factory=list)  # sub-node IDs reachable via me
    capabilities: dict[str, list[str]] = Field(default_factory=dict)  # {node_id: [actions]}
    # v5.11 — full action specs for capability discovery
    action_specs: dict[str, dict] = Field(default_factory=dict)  # {action_name: ActionSpec dict}
    # v5.13 — action_specs for each advertised sub-node
    sub_route_specs: dict[str, dict] = Field(default_factory=dict)  # {sub_node_id: {action: spec}}


class NodeRegistrationResponse(BaseModel):
    node_id: str
    registered: bool
    message: str = ""


class HeartbeatRequest(BaseModel):
    node_id: str


class HeartbeatResponse(BaseModel):
    node_id: str
    acknowledged: bool


class NodeInfo(BaseModel):
    node_id: str
    address: str | None
    status: NodeStatus
    last_heartbeat: float | None


# ---------------------------------------------------------------------------
# v5.3 — Job Queue models
# ---------------------------------------------------------------------------

class QueuedJob(BaseModel):
    job_id: str
    task_id: str
    target_node_id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    timeout_seconds: int = 300          # v5.7: job fails if unclaimed after this many seconds
    claimed: bool = False
    claimed_at: float | None = None
    # v5.11 — carry caller auth through pull chain (not logged, not in params)
    caller_credentials: dict[str, str] = Field(default_factory=dict)
    caller_token: str | None = None


class PollResponse(BaseModel):
    node_id: str
    jobs: list[QueuedJob]


class ClaimRequest(BaseModel):
    node_id: str


class ClaimResponse(BaseModel):
    job_id: str
    claimed: bool
    reason: str = ""


class JobResultReport(BaseModel):
    node_id: str
    status: str        # "completed" or "failed"
    output: dict[str, Any] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# v5.8 — File Upload / Download models
# ---------------------------------------------------------------------------

class UploadResponse(BaseModel):
    """Returned after a successful POST /upload."""
    file_id: str
    filename: str
    size_bytes: int
    ttl_seconds: int
    download_url: str       # e.g. "/download/{file_id}"


class FileInfo(BaseModel):
    """Metadata for a stored upload — returned in GET /files listing."""
    file_id: str
    filename: str
    size_bytes: int
    created_at: float
    age_seconds: float
    uploader: str | None = None


class FileListResponse(BaseModel):
    files: list[FileInfo]
    total: int


# ---------------------------------------------------------------------------
# v5.9 — Intent / Agent Loop models
# ---------------------------------------------------------------------------

class IntentRequest(BaseModel):
    """POST /intent — natural language prompt from any channel (Telegram, etc.)."""
    prompt: str
    session_id: str | None = None          # caller-supplied; auto-gen if absent
    node_hint: str | None = None           # preferred target node (optional hint to agent)
    max_turns: int | None = None           # override config.intent_max_turns
    # v5.11 — caller credentials forwarded to every tool call in the agent loop
    caller_credentials: dict[str, str] = Field(default_factory=dict)


class ConversationMessage(BaseModel):
    """Single message in a conversation history."""
    role: str                              # "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None        # set for tool result messages
    tool_name: str | None = None           # set for tool result messages
    timestamp: float = 0.0


class SessionInfo(BaseModel):
    """Summary of a conversation session — returned by GET /sessions/{id}."""
    session_id: str
    created_at: float
    last_active: float
    age_seconds: float
    turn_count: int
    messages: list[ConversationMessage]


class IntentResponse(BaseModel):
    """Response from POST /intent."""
    session_id: str
    reply: str                             # final human-readable answer
    turns: int                             # how many LLM↔tool rounds were needed
    actions_taken: list[str]               # brief log: ["execute_command on node-1", ...]
    tokens_used: int
    truncated: bool = False                # True if max_turns was hit before completion


# ---------------------------------------------------------------------------
# v5.11 — Action specs & caller credentials
# ---------------------------------------------------------------------------

class CallerCredentialSpec(BaseModel):
    """One credential that a caller must supply to invoke an action."""
    description: str
    required: bool = True
    hint: str = ""           # shown to LLM / user to help them obtain the credential


class ActionSpec(BaseModel):
    """Full specification of one action — params schema + caller credential requirements."""
    description: str = ""
    params_schema: dict[str, Any] = Field(default_factory=dict)
    caller_credentials: dict[str, CallerCredentialSpec] = Field(default_factory=dict)
    async_action: bool = False


# ---------------------------------------------------------------------------
# v5.10 — Capability tree
# ---------------------------------------------------------------------------

class CapabilityNode(BaseModel):
    """One node in the capability tree — returned by GET /capabilities."""
    node_id: str
    actions: list[str]                   # action names (backward compat)
    action_specs: dict[str, ActionSpec] = Field(default_factory=dict)  # v5.11: full specs
    next_hop: str | None = None          # None = direct child of current node
    status: str = "unknown"              # v5.12: online / unreachable / unknown
    reachable: dict[str, "CapabilityNode"] = Field(default_factory=dict)

CapabilityNode.model_rebuild()   # resolve forward-ref self


class CapabilityTreeResponse(BaseModel):
    """Full capability tree rooted at the responding node."""
    node_id: str
    actions: list[str]
    reachable: dict[str, CapabilityNode]


# ---------------------------------------------------------------------------
# v6.0 — EventBus models
# ---------------------------------------------------------------------------

class Event(BaseModel):
    """A single event in the EventBus log."""
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str                                     # dot-notation: "artifact.written"
    source_node: str                                    # emitter node_id
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)
    correlation_id: str | None = None
    reply_to: str | None = None


class Subscription(BaseModel):
    """A registered subscription on the EventBus."""
    sub_id: str = Field(default_factory=lambda: f"sub-{uuid.uuid4().hex[:12]}")
    subscriber_node: str
    callback_action: str                                # action invoked on event match
    callback_params_template: dict[str, Any] = Field(default_factory=dict)
    event_type_pattern: str = "*"                       # exact / "test.*" / "*.failed" / "*"
    source_node: str | None = None                      # None = any source
    payload_filter: dict[str, Any] | None = None        # None = no filter
    debounce_seconds: float = 0.0
    max_deliveries: int | None = None                   # None = unlimited
    description: str = ""
    # Internal tracking fields
    delivery_count: int = 0
    last_delivered_at: float | None = None


# v6.0 — EventBus HTTP request / response models

class EmitRequest(BaseModel):
    """POST /emit — publish an event."""
    event_type: str
    source_node: str | None = None          # defaults to the receiving node's node_id
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    reply_to: str | None = None


class EmitResponse(BaseModel):
    """Response from POST /emit."""
    event_id: str
    matched_subscriptions: int


class SubscribeRequest(BaseModel):
    """POST /subscribe — register a subscription."""
    subscriber_node: str
    callback_action: str
    callback_params_template: dict[str, Any] = Field(default_factory=dict)
    event_type_pattern: str = "*"
    source_node: str | None = None
    payload_filter: dict[str, Any] | None = None
    debounce_seconds: float = 0.0
    max_deliveries: int | None = None
    description: str = ""


class SubscribeResponse(BaseModel):
    """Response from POST /subscribe."""
    sub_id: str
    event_type_pattern: str
    subscriber_node: str


# ---------------------------------------------------------------------------
# v6.0 — Scheduler models (Phase 2)
# ---------------------------------------------------------------------------

class ScheduleEntry(BaseModel):
    """A registered schedule trigger on the Scheduler."""
    schedule_id: str = Field(default_factory=lambda: f"sched-{uuid.uuid4().hex[:12]}")
    trigger_type: str                               # "condition" | "cron" | "event" | "once"
    target_node: str
    run_action: str
    run_params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    description: str = ""

    # condition trigger
    check_action: str | None = None
    check_params: dict[str, Any] = Field(default_factory=dict)
    check_interval_seconds: int = 60

    # cron trigger
    cron_expression: str | None = None             # 5-field cron: "*/5 * * * *"

    # event trigger
    on_event_type: str | None = None
    on_payload_filter: dict[str, Any] | None = None

    # once trigger
    run_at: float | None = None                    # unix timestamp

    # shared options
    max_concurrent: int = 1
    skip_if_running: bool = True
    timeout_seconds: int = 300
    retry_on_failure: int = 0

    # Internal runtime fields (not persisted)
    running_count: int = 0
    last_run_at: float | None = None
    last_result: str | None = None                 # "success" | "failed" | "skipped"


class SchedulePatchRequest(BaseModel):
    """PATCH /schedule/{id} — partial update."""
    enabled: bool | None = None
    run_params: dict[str, Any] | None = None
    check_interval_seconds: int | None = None
    cron_expression: str | None = None
    description: str | None = None
    timeout_seconds: int | None = None
    retry_on_failure: int | None = None
