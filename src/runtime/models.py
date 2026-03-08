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
    SUSPENDED = "suspended"   # v6.0 Phase 4: blocked waiting for clarification
    RESUMING = "resuming"     # v6.0 Phase 4: answer received, resuming execution
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"   # v6.0 Phase 4: suspension timeout reached


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


# ---------------------------------------------------------------------------
# v6.0 Phase 4 — Task Suspension & Resumption models
# ---------------------------------------------------------------------------

class TaskCheckpoint(BaseModel):
    """Persisted state of a suspended intent task.

    Saved when the LLM calls suspend_and_ask mid-loop.
    Loaded on resume to reconstruct the conversation and continue execution.
    """
    checkpoint_id: str = Field(default_factory=lambda: f"cp-{uuid.uuid4().hex[:12]}")
    task_id: str                            # unique ID for this /intent invocation
    session_id: str                         # conversation session (message history)
    node_id: str                            # node that owns this checkpoint
    original_prompt: str                    # the user prompt that started the task
    messages: list[dict[str, Any]]          # full LLM message history at suspend point
    suspend_tool_call_id: str               # tool_call_id of the suspend_and_ask call (for tool result injection)
    turn_count: int                         # how many turns were completed before suspension
    suspended_at: float = Field(default_factory=time.time)
    suspension_reason: str = "clarification_needed"
    pending_question: str                   # the question posed
    pending_question_id: str               # correlation_id; used to match the answer
    asked_node: str = ""                    # agent-to-agent: specific node asked
    target_role: str = ""                   # agent-to-human: role required to answer
    timeout_seconds: int = 86400           # 24h default
    timeout_action: str = "use_assumption" # "use_assumption" | "fail"
    assumption: str = ""                   # value used if timeout reached
    status: str = "suspended"             # suspended | answered | resumed | timed_out
    answer: str | None = None             # filled in when answered
    answered_at: float | None = None


class TaskSuspendedResponse(BaseModel):
    """Returned by POST /intent when an agent task suspends mid-execution."""
    session_id: str
    task_id: str
    suspended: bool = True
    question: str
    question_id: str
    asked_node: str = ""
    target_role: str = ""
    timeout_seconds: int
    assumption: str
    message: str = "Task suspended — waiting for clarification"


class TaskListResponse(BaseModel):
    """Response from GET /tasks — lists active + suspended tasks."""
    tasks: list[dict[str, Any]]
    total: int


class TaskAnswerRequest(BaseModel):
    """POST /tasks/{task_id}/answer — manually inject an answer."""
    answer: str
    answered_by: str = "manual"


# ---------------------------------------------------------------------------
# v6.0 Phase 5 — Cluster Provisioning models
# ---------------------------------------------------------------------------

class NodeSpec(BaseModel):
    """Specification for one node in a cluster provisioning request."""
    node_id: str
    role: str                               # e.g. "pm", "developer", "gateway"
    listen: str                             # "0.0.0.0:<port>"
    skills_md: str = ""                     # role skills (loaded from blueprint)
    actions: list[dict[str, Any]] = Field(default_factory=list)  # list of {filename, content, schema_content?}
    blueprint: str | None = None            # role blueprint name (e.g. "pm")
    auth_token: str | None = None
    gateway_node_id: str | None = None
    gateway_address: str | None = None
    gateway_auth_token: str | None = None
    allowed_tokens: list[str] = Field(default_factory=list)
    extra_nodes: dict[str, str] = Field(default_factory=dict)
    pip_packages: list[str] = Field(default_factory=list)
    # v6 feature flags
    event_bus_enabled: bool = True
    scheduler_enabled: bool = True
    task_pool_enabled: bool = True
    checkpoint_store_enabled: bool = True
    session_backend: str = "persistent"
    memory_enabled: bool = True
    poll_interval_seconds: int = 5
    poll_interval_max_seconds: int = 60
    poll_backoff_multiplier: float = 1.5
    # LLM config (inherited from cluster spec if not set)
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    # Schedule wiring (filled by ClusterOrchestrator)
    schedule: list[dict[str, Any]] = Field(default_factory=list)
    # Runtime metadata
    is_gateway: bool = False


class ClusterSpec(BaseModel):
    """Blueprint for provisioning a complete cluster of nodes."""
    cluster_id: str = Field(default_factory=lambda: f"cluster-{uuid.uuid4().hex[:8]}")
    cluster_name: str = ""
    description: str = ""
    base_dir: str = "."
    runtime_entry: str = "node_runtime.py"
    # LLM config (inherited by all nodes unless overridden)
    llm_api_key: str | None = None
    llm_model: str = "claude-sonnet-4-20250514"
    llm_base_url: str = "https://api.anthropic.com/v1"
    # Auth
    gateway_token: str | None = None        # generated if not set
    # Nodes to provision
    gateway: NodeSpec | None = None         # primary gateway; auto-generated if None
    workers: list[NodeSpec] = Field(default_factory=list)
    # Port allocation
    port_range_start: int = 8090
    port_range_end: int = 8200
    # Post-provision wiring
    wire_subscriptions: bool = True         # auto-wire clarification.answered → handle_clarification_answer
    kickoff_on_provision: bool = False      # auto-emit cluster.started after provision
    kickoff_prompt: str = ""               # intent prompt for kickoff (if set)
    # Metadata
    tags: dict[str, str] = Field(default_factory=dict)


class ClusterNodeResult(BaseModel):
    """Result of provisioning a single node in a cluster."""
    node_id: str
    role: str
    address: str | None = None
    pid: int | None = None
    status: str = "pending"     # pending | running | failed
    error: str | None = None


class ClusterProvisionResult(BaseModel):
    """Result of POST /clusters/provision."""
    cluster_id: str
    status: str = "pending"     # provisioning | running | failed | partial
    gateway: ClusterNodeResult | None = None
    workers: list[ClusterNodeResult] = Field(default_factory=list)
    error: str | None = None
    provisioned_at: float = Field(default_factory=time.time)
    port_range_used: list[int] = Field(default_factory=list)


class ClusterInfo(BaseModel):
    """Cluster status — returned by GET /clusters and GET /clusters/{id}."""
    cluster_id: str
    cluster_name: str = ""
    status: str                 # provisioning | running | failed | torn_down
    gateway: ClusterNodeResult | None = None
    workers: list[ClusterNodeResult] = Field(default_factory=list)
    provisioned_at: float
    torn_down_at: float | None = None
    kickoff_sent: bool = False
    tags: dict[str, str] = Field(default_factory=dict)


class TeardownResult(BaseModel):
    """Result of POST /clusters/{id}/teardown."""
    cluster_id: str
    stopped_nodes: list[str] = Field(default_factory=list)
    failed_nodes: list[str] = Field(default_factory=list)
    archived: bool = False
    error: str | None = None


class BlueprintInfo(BaseModel):
    """Metadata entry for one blueprint in the catalog."""
    id: str
    type: str               # "role" | "team"
    name: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    path: str = ""          # relative file path

class BlueprintListResponse(BaseModel):
    """Response from GET /blueprints."""
    blueprints: list[BlueprintInfo]
    total: int

class GatewayConnectRequest(BaseModel):
    """POST /gateways/connect — runtime join a new gateway."""
    address: str
    auth_token: str | None = None
    node_id_override: str | None = None     # gateway node_id (resolved if None)

class GatewayConnectResponse(BaseModel):
    """Response from POST /gateways/connect."""
    address: str
    gateway_node_id: str
    connected: bool
    message: str = ""
