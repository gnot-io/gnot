"""Node configuration loader and validator.

Reads a node.yaml file and produces a validated NodeConfig dataclass
with all fields required by the mesh runtime.

v5.3 additions:
  Gateway fields: trusted_nodes, heartbeat_timeout_seconds, ping_timeout_seconds
  Worker fields:  gateway_node_id, gateway_address, self_address,
                  heartbeat_interval_seconds, poll_interval_seconds

v5.7 additions:
  pull_job_timeout_seconds — gateway: fail a queued pull job after N seconds
                             with no worker claiming it (lazy check on /result).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from runtime.models import DEFAULT_CACHE_TTL_SECONDS, DEFAULT_MAX_HOP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required top-level keys
# ---------------------------------------------------------------------------

REQUIRED_FIELDS: set[str] = {"node_id", "listen"}

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_JOB_TTL_SECONDS: int = 3600
DEFAULT_CLEANUP_INTERVAL_SECONDS: int = 60

# v5.3 defaults
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS: int = 30    # gateway: mark node unreachable after N s
DEFAULT_PING_TIMEOUT_SECONDS: float = 3.0      # gateway: ping timeout
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: int = 10   # worker: send heartbeat every N s
DEFAULT_POLL_INTERVAL_SECONDS: int = 5         # worker: poll gateway every N s

# v5.7 defaults
DEFAULT_PULL_JOB_TIMEOUT_SECONDS: int = 300    # gateway: fail queued pull job after N s

# v5.8 defaults
DEFAULT_UPLOAD_DIR: str = "/tmp/mesh-uploads"
DEFAULT_UPLOAD_MAX_SIZE_MB: int = 512
DEFAULT_UPLOAD_TTL_SECONDS: int = 3600         # uploaded file auto-expires after 1 hour

# v5.11 defaults
DEFAULT_CALLER_POLICIES: list = []   # empty = level 1 (open)

# v5.9 defaults
DEFAULT_INTENT_MAX_TURNS: int = 10        # max agent loop iterations per prompt
DEFAULT_SESSION_TTL_SECONDS: int = 3600   # conversation session expiry

# v6.0 defaults — EventBus
DEFAULT_EVENT_BUS_ENABLED: bool = True
DEFAULT_EVENT_MAX_LOG_SIZE: int = 10_000
DEFAULT_EVENT_DELIVERY_TIMEOUT: float = 10.0
DEFAULT_EVENT_DELIVERY_RETRY_COUNT: int = 3
DEFAULT_EVENT_DELIVERY_RETRY_BACKOFF: float = 2.0

# v6.0 Phase 2 defaults — Scheduler
DEFAULT_SCHEDULER_ENABLED: bool = True

# v6.0 Phase 2 defaults — Adaptive polling
DEFAULT_POLL_INTERVAL_MAX_SECONDS: int = 60
DEFAULT_POLL_BACKOFF_MULTIPLIER: float = 1.5

# v6.0 Phase 2 defaults — Registration policy
# NOTE: "open" matches historical v5.x behavior (any authenticated node accepted).
# Set to "whitelist" in node.yaml for stricter environments.
DEFAULT_REGISTRATION_POLICY: str = "whitelist"  # open | whitelist | invite_only

# v6.0 Phase 3 defaults — Session backend
DEFAULT_SESSION_BACKEND: str = "memory"    # "memory" | "persistent"
DEFAULT_SESSION_STORAGE_DIR: str = "./sessions"
DEFAULT_MAX_MESSAGES_PER_SESSION: int = 0  # 0 = unlimited

# v6.0 Phase 3 defaults — Agent memory
DEFAULT_MEMORY_ENABLED: bool = False
DEFAULT_MEMORY_STORAGE_DIR: str = "./memory"
DEFAULT_MEMORY_INJECT_INTO_PROMPT: bool = True
DEFAULT_MEMORY_MAX_ENTRIES: int = 1000

# v6.0 Phase 4 defaults — Task pool
DEFAULT_TASK_POOL_ENABLED: bool = True
DEFAULT_MAX_ACTIVE_TASKS: int = 3

# v6.0 Phase 4 defaults — Checkpoint store
DEFAULT_CHECKPOINT_STORE_ENABLED: bool = True
DEFAULT_CHECKPOINT_STORAGE_PATH: str = "/tmp/gnot-checkpoints"
DEFAULT_CHECKPOINT_DEFAULT_TIMEOUT: int = 86400    # 24h
DEFAULT_CHECKPOINT_SWEEP_INTERVAL: int = 3600      # 1h


# ---------------------------------------------------------------------------
# v5.11 — Caller authorization policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CallerPolicy:
    """Maps a Bearer token to the set of actions the caller may invoke.

    allowed_actions can be:
      - A list of action names: ["get_order_info", "list_orders"]
      - The string "*" — caller may invoke any action (level 1 / open)
    """
    token: str
    allowed_actions: list[str] | str   # list or "*"

    def allows(self, action: str) -> bool:
        if self.allowed_actions == "*":
            return True
        return action in self.allowed_actions


def check_caller_policy(
    policies: list["CallerPolicy"],
    caller_token: str | None,
    action: str,
) -> bool:
    """Return True if caller is allowed to invoke action.

    Rules:
      - No policies configured → open (level 1): always True
      - Token not in any policy → deny (return False)
      - Token found → check allowed_actions
    """
    if not policies:
        return True   # level 1: no restrictions
    if caller_token is None:
        return False  # policies exist but no token provided
    for policy in policies:
        if policy.token == caller_token:
            return policy.allows(action)
    return False  # token not in any policy


# ---------------------------------------------------------------------------
# v6.0 Phase 2 — Scheduler configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SchedulerConfig:
    """Configuration for the Scheduler autonomy engine."""
    enabled: bool = DEFAULT_SCHEDULER_ENABLED


# ---------------------------------------------------------------------------
# v6.0 Phase 3 — Session backend config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionConfig:
    """Session storage configuration."""
    backend: str = DEFAULT_SESSION_BACKEND        # "memory" | "persistent"
    storage_dir: str = DEFAULT_SESSION_STORAGE_DIR
    default_ttl_seconds: int = 0                  # 0 = infinite
    max_messages_per_session: int = DEFAULT_MAX_MESSAGES_PER_SESSION


# ---------------------------------------------------------------------------
# v6.0 Phase 3 — Agent memory config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryConfig:
    """Agent memory storage configuration."""
    enabled: bool = DEFAULT_MEMORY_ENABLED
    storage_dir: str = DEFAULT_MEMORY_STORAGE_DIR
    inject_into_prompt: bool = DEFAULT_MEMORY_INJECT_INTO_PROMPT
    max_entries: int = DEFAULT_MEMORY_MAX_ENTRIES


# ---------------------------------------------------------------------------
# v6.0 Phase 4 — Task Pool & Checkpoint Store config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskPoolConfig:
    """Configuration for the TaskPool concurrent task manager."""
    enabled: bool = DEFAULT_TASK_POOL_ENABLED
    max_active_tasks: int = DEFAULT_MAX_ACTIVE_TASKS


@dataclass(frozen=True)
class CheckpointStoreConfig:
    """Configuration for the CheckpointStore task state persistence."""
    enabled: bool = DEFAULT_CHECKPOINT_STORE_ENABLED
    path: str = DEFAULT_CHECKPOINT_STORAGE_PATH
    default_timeout_seconds: int = DEFAULT_CHECKPOINT_DEFAULT_TIMEOUT
    sweep_interval_seconds: int = DEFAULT_CHECKPOINT_SWEEP_INTERVAL


# ---------------------------------------------------------------------------
# v6.0 — EventBus configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EventBusConfig:
    """Configuration for the EventBus pub/sub system."""
    enabled: bool = DEFAULT_EVENT_BUS_ENABLED
    max_log_size: int = DEFAULT_EVENT_MAX_LOG_SIZE
    delivery_timeout_seconds: float = DEFAULT_EVENT_DELIVERY_TIMEOUT
    delivery_retry_count: int = DEFAULT_EVENT_DELIVERY_RETRY_COUNT
    delivery_retry_backoff: float = DEFAULT_EVENT_DELIVERY_RETRY_BACKOFF
    persistence_path: str | None = None     # if set, persist events to JSONL file




# ---------------------------------------------------------------------------
# v6.0 Phase 7 — Transport bridge defaults + config
# ---------------------------------------------------------------------------

DEFAULT_TELEGRAM_ENABLED: bool = False
DEFAULT_TELEGRAM_BOTS_STORAGE_PATH: str = "./telegram-bots"
DEFAULT_TELEGRAM_POLL_INTERVAL_SECONDS: int = 1
DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS: int = 30
DEFAULT_TELEGRAM_API_TIMEOUT_SECONDS: int = 10
DEFAULT_TELEGRAM_MESSAGE_FORMAT: str = "full"
DEFAULT_TELEGRAM_LANGUAGE: str = "vi"


@dataclass(frozen=True)
class TelegramTransportConfig:
    """Configuration for the Telegram transport bridge (Phase 7)."""
    enabled: bool = DEFAULT_TELEGRAM_ENABLED
    gnot_base_url: str | None = None          # if None, auto-detect from listen:
    token_encryption_key: str | None = None   # if None, use auth_token
    bots_storage_path: str = DEFAULT_TELEGRAM_BOTS_STORAGE_PATH
    default_message_format: str = DEFAULT_TELEGRAM_MESSAGE_FORMAT
    default_language: str = DEFAULT_TELEGRAM_LANGUAGE
    poll_interval_seconds: int = DEFAULT_TELEGRAM_POLL_INTERVAL_SECONDS
    poll_timeout_seconds: int = DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS
    api_timeout_seconds: int = DEFAULT_TELEGRAM_API_TIMEOUT_SECONDS

# ---------------------------------------------------------------------------
# NodeConfig dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeConfig:
    """Typed representation of a node's configuration."""

    node_id: str
    listen: str
    nodes: dict[str, str] = field(default_factory=dict)
    default_resolver: str | None = None
    max_hop: int = DEFAULT_MAX_HOP
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    actions_dir: str | None = None
    skills_file: str | None = None
    auth_token: str | None = None
    job_ttl_seconds: int = DEFAULT_JOB_TTL_SECONDS
    cleanup_interval_seconds: int = DEFAULT_CLEANUP_INTERVAL_SECONDS

    # LLM configuration
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_default_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 120.0
    llm_extra_headers: dict[str, str] = field(default_factory=dict)

    # v5.3 — Gateway fields
    trusted_nodes: list[str] = field(default_factory=list)
    heartbeat_timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
    ping_timeout_seconds: float = DEFAULT_PING_TIMEOUT_SECONDS

    # v5.7 — Gateway fields
    pull_job_timeout_seconds: int = DEFAULT_PULL_JOB_TIMEOUT_SECONDS

    # v5.8 — Upload manager
    upload_dir: str = DEFAULT_UPLOAD_DIR
    upload_max_size_mb: int = DEFAULT_UPLOAD_MAX_SIZE_MB
    upload_ttl_seconds: int = DEFAULT_UPLOAD_TTL_SECONDS

    # v5.11 — Caller authorization
    caller_policies: tuple = field(default_factory=tuple)  # tuple of CallerPolicy

    # v5.13 — Credential store
    credential_encryption_key: str | None = None   # separate AES key; if None, derive from auth_token
    credential_store_path: str | None = None       # if set, persist credentials to this JSON file

    # v5.13 — Per-node auth tokens
    # Gateway: set allowed_tokens = list of all worker tokens → each worker has a unique token
    # Worker: set gateway_auth_token = token this worker uses when calling its gateway
    allowed_tokens: tuple = field(default_factory=tuple)  # tuple[str] — accepted Bearer tokens
    gateway_auth_token: str | None = None  # token to present to gateway (overrides auth_token)

    # v5.9 — Intent / agent loop
    intent_max_turns: int = DEFAULT_INTENT_MAX_TURNS
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    intent_system_prompt: str | None = None   # override default agent system prompt

    # v5.3 — Worker fields
    gateway_node_id: str | None = None     # which node is this worker's gateway
    gateway_address: str | None = None     # HTTP address of the gateway
    self_address: str | None = None        # worker's own address (if reachable from gateway)
    heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS

    # v6.0 — EventBus
    event_bus: EventBusConfig = field(default_factory=EventBusConfig)

    # v6.0 Phase 2 — Scheduler
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    schedule: tuple = field(default_factory=tuple)  # tuple[dict] — static entries from node.yaml

    # v6.0 Phase 2 — Adaptive polling
    poll_interval_max_seconds: int = DEFAULT_POLL_INTERVAL_MAX_SECONDS
    poll_backoff_multiplier: float = DEFAULT_POLL_BACKOFF_MULTIPLIER

    # v6.0 Phase 2 — Multi-gateway
    additional_gateways: tuple = field(default_factory=tuple)  # tuple[dict] of {address, auth_token}

    # v6.0 Phase 2 — Registration policy
    registration_policy: str = DEFAULT_REGISTRATION_POLICY  # open | whitelist | invite_only

    # v6.0 Phase 3 — Session backend
    session: SessionConfig = field(default_factory=SessionConfig)

    # v6.0 Phase 3 — Agent memory
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    # v6.0 Phase 3 — MCP servers (list of {id, transport, command?, url?, env?})
    mcp_servers: tuple = field(default_factory=tuple)

    # v6.0 Phase 4 — Task Pool & Checkpoint Store
    task_pool: "TaskPoolConfig" = field(default_factory=lambda: TaskPoolConfig())
    checkpoint_store: "CheckpointStoreConfig" = field(default_factory=lambda: CheckpointStoreConfig())

    # v6.0 Phase 5 — Cluster Orchestrator
    cluster_orchestrator: "ClusterOrchestratorConfig" = field(
        default_factory=lambda: ClusterOrchestratorConfig()
    )

    # v6.0 Phase 6 — External Participant Interaction
    participants_dir: str = "/tmp/gnot-participants"
    channel_log_dir: str = "/tmp/gnot-channels"

    # v6.0 Phase 7 — Transport bridges
    telegram: "TelegramTransportConfig" = field(default_factory=lambda: TelegramTransportConfig())

    # Derived helpers -------------------------------------------------------

    @property
    def host(self) -> str:
        return self.listen.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.listen.rsplit(":", 1)[1])

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def is_worker(self) -> bool:
        """True if this node is a worker that should register with a gateway."""
        return bool(self.gateway_node_id and self.gateway_address)

    @property
    def is_gateway(self) -> bool:
        """True if this node acts as a gateway.

        A node is a gateway if it has a pre-configured trusted_nodes list
        (whitelist / invite_only policy), or if it explicitly runs with the
        open registration policy (auto-accepts any authenticated node).
        """
        return bool(self.trusted_nodes) or self.registration_policy == "open"

    @property
    def upload_max_size_bytes(self) -> int:
        """Max upload file size in bytes (derived from upload_max_size_mb)."""
        return self.upload_max_size_mb * 1024 * 1024


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(config_path: str | Path) -> NodeConfig:
    """Load and validate a node.yaml configuration file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    missing = REQUIRED_FIELDS - set(raw.keys())
    if missing:
        raise ValueError(f"Missing required config fields: {missing}")

    # Parse llm section (flat or nested)
    llm_section = raw.get("llm", {})
    llm_api_key = llm_section.get("api_key") or raw.get("llm_api_key")
    llm_base_url = llm_section.get("base_url") or raw.get("llm_base_url", "https://api.openai.com/v1")
    llm_default_model = llm_section.get("default_model") or raw.get("llm_default_model", "gpt-4o-mini")
    llm_timeout = llm_section.get("timeout_seconds") or raw.get("llm_timeout_seconds", 120.0)
    llm_extra_headers = llm_section.get("extra_headers") or raw.get("llm_extra_headers", {})

    config = NodeConfig(
        node_id=raw["node_id"],
        listen=raw["listen"],
        nodes=raw.get("nodes", {}),
        default_resolver=raw.get("default_resolver"),
        max_hop=raw.get("max_hop", DEFAULT_MAX_HOP),
        cache_ttl_seconds=raw.get("cache_ttl_seconds", DEFAULT_CACHE_TTL_SECONDS),
        actions_dir=raw.get("actions_dir"),
        skills_file=raw.get("skills_file"),
        auth_token=raw.get("auth_token"),
        job_ttl_seconds=raw.get("job_ttl_seconds", DEFAULT_JOB_TTL_SECONDS),
        cleanup_interval_seconds=raw.get("cleanup_interval_seconds", DEFAULT_CLEANUP_INTERVAL_SECONDS),
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_default_model=llm_default_model,
        llm_timeout_seconds=llm_timeout,
        llm_extra_headers=llm_extra_headers,
        # v5.3
        trusted_nodes=raw.get("trusted_nodes", []),
        heartbeat_timeout_seconds=raw.get("heartbeat_timeout_seconds", DEFAULT_HEARTBEAT_TIMEOUT_SECONDS),
        ping_timeout_seconds=float(raw.get("ping_timeout_seconds", DEFAULT_PING_TIMEOUT_SECONDS)),
        # v5.7
        pull_job_timeout_seconds=raw.get("pull_job_timeout_seconds", DEFAULT_PULL_JOB_TIMEOUT_SECONDS),
        gateway_node_id=raw.get("gateway_node_id"),
        gateway_address=raw.get("gateway_address"),
        self_address=raw.get("self_address"),
        heartbeat_interval_seconds=raw.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
        poll_interval_seconds=raw.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS),
        # v5.8
        upload_dir=raw.get("upload_dir", DEFAULT_UPLOAD_DIR),
        upload_max_size_mb=raw.get("upload_max_size_mb", DEFAULT_UPLOAD_MAX_SIZE_MB),
        upload_ttl_seconds=raw.get("upload_ttl_seconds", DEFAULT_UPLOAD_TTL_SECONDS),
        # v5.11
        caller_policies=tuple(
            CallerPolicy(
                token=p["token"],
                allowed_actions=p.get("allowed_actions", "*"),
            )
            for p in raw.get("caller_policies", [])
            if "token" in p
        ),
        # v5.9
        intent_max_turns=raw.get("intent_max_turns", DEFAULT_INTENT_MAX_TURNS),
        session_ttl_seconds=raw.get("session_ttl_seconds", DEFAULT_SESSION_TTL_SECONDS),
        intent_system_prompt=raw.get("intent_system_prompt"),
        # v5.13
        credential_encryption_key=raw.get("credential_encryption_key"),
        credential_store_path=raw.get("credential_store_path"),
        allowed_tokens=tuple(raw.get("allowed_tokens", [])),
        gateway_auth_token=raw.get("gateway_auth_token"),
    )

    # Parse v6.0 event_bus section (nested dict, all optional)
    _eb = raw.get("event_bus", {}) or {}
    _eb_config = EventBusConfig(
        enabled=_eb.get("enabled", DEFAULT_EVENT_BUS_ENABLED),
        max_log_size=int(_eb.get("max_log_size", DEFAULT_EVENT_MAX_LOG_SIZE)),
        delivery_timeout_seconds=float(_eb.get("delivery_timeout_seconds", DEFAULT_EVENT_DELIVERY_TIMEOUT)),
        delivery_retry_count=int(_eb.get("delivery_retry_count", DEFAULT_EVENT_DELIVERY_RETRY_COUNT)),
        delivery_retry_backoff=float(_eb.get("delivery_retry_backoff", DEFAULT_EVENT_DELIVERY_RETRY_BACKOFF)),
        persistence_path=_eb.get("persistence_path"),
    )

    # Parse v6.0 Phase 2 fields
    _sched_section = raw.get("scheduler", {}) or {}
    _sched_config = SchedulerConfig(
        enabled=_sched_section.get("enabled", DEFAULT_SCHEDULER_ENABLED),
    )
    _static_schedule = tuple(raw.get("schedule", []) or [])
    _additional_gateways = tuple(raw.get("additional_gateways", []) or [])
    _registration_policy = raw.get("registration_policy", DEFAULT_REGISTRATION_POLICY)

    # Re-build config with all v6 fields populated (frozen dataclass requires reconstruction)
    config = NodeConfig(
        **{f.name: getattr(config, f.name) for f in config.__dataclass_fields__.values()
           if f.name not in ("event_bus", "scheduler", "schedule", "additional_gateways",
                             "registration_policy", "poll_interval_max_seconds",
                             "poll_backoff_multiplier",
                             "session", "memory", "mcp_servers",
                             "task_pool", "checkpoint_store",
                             "cluster_orchestrator", "telegram")},
        event_bus=_eb_config,
        scheduler=_sched_config,
        schedule=_static_schedule,
        additional_gateways=_additional_gateways,
        registration_policy=_registration_policy,
        poll_interval_max_seconds=int(raw.get("poll_interval_max_seconds", DEFAULT_POLL_INTERVAL_MAX_SECONDS)),
        poll_backoff_multiplier=float(raw.get("poll_backoff_multiplier", DEFAULT_POLL_BACKOFF_MULTIPLIER)),
        # v6.0 Phase 3
        session=_parse_session_config(raw),
        memory=_parse_memory_config(raw),
        mcp_servers=tuple(raw.get("mcp_servers", []) or []),
        # v6.0 Phase 4
        task_pool=_parse_task_pool_config(raw),
        checkpoint_store=_parse_checkpoint_store_config(raw),
        # v6.0 Phase 5
        cluster_orchestrator=_parse_cluster_orchestrator_config(raw),
        # v6.0 Phase 7
        telegram=_parse_telegram_config(raw),
    )

    logger.info(
        "Loaded config: %s — listen=%s, gateway=%s, worker=%s, trusted_nodes=%s, auth=%s, llm=%s",
        config.node_id,
        config.listen,
        "yes" if config.is_gateway else "no",
        "yes" if config.is_worker else "no",
        config.trusted_nodes or "[]",
        "enabled" if config.auth_token else "disabled",
        "enabled" if config.llm_enabled else "disabled",
    )
    return config


# ---------------------------------------------------------------------------
# v6.0 Phase 3 — Config section parsers
# ---------------------------------------------------------------------------

def _parse_session_config(raw: dict) -> "SessionConfig":
    """Parse the 'session:' section from node.yaml."""
    s = raw.get("session", {}) or {}
    # Also support legacy flat field: session_ttl_seconds → default_ttl_seconds
    legacy_ttl = raw.get("session_ttl_seconds", 0)
    return SessionConfig(
        backend=s.get("backend", DEFAULT_SESSION_BACKEND),
        storage_dir=s.get("storage_dir", DEFAULT_SESSION_STORAGE_DIR),
        default_ttl_seconds=int(s.get("default_ttl_seconds", legacy_ttl)),
        max_messages_per_session=int(
            s.get("max_messages_per_session", DEFAULT_MAX_MESSAGES_PER_SESSION)
        ),
    )


def _parse_memory_config(raw: dict) -> "MemoryConfig":
    """Parse the 'memory:' section from node.yaml."""
    m = raw.get("memory", {}) or {}
    return MemoryConfig(
        enabled=m.get("enabled", DEFAULT_MEMORY_ENABLED),
        storage_dir=m.get("storage_dir", DEFAULT_MEMORY_STORAGE_DIR),
        inject_into_prompt=m.get("inject_into_prompt", DEFAULT_MEMORY_INJECT_INTO_PROMPT),
        max_entries=int(m.get("max_entries", DEFAULT_MEMORY_MAX_ENTRIES)),
    )


# ---------------------------------------------------------------------------
# v6.0 Phase 4 — Config section parsers
# ---------------------------------------------------------------------------

def _parse_task_pool_config(raw: dict) -> "TaskPoolConfig":
    """Parse the 'task_pool:' section from node.yaml."""
    t = raw.get("task_pool", {}) or {}
    return TaskPoolConfig(
        enabled=t.get("enabled", DEFAULT_TASK_POOL_ENABLED),
        max_active_tasks=int(t.get("max_active_tasks", DEFAULT_MAX_ACTIVE_TASKS)),
    )


def _parse_checkpoint_store_config(raw: dict) -> "CheckpointStoreConfig":
    """Parse the 'checkpoint_store:' section from node.yaml."""
    c = raw.get("checkpoint_store", {}) or {}
    return CheckpointStoreConfig(
        enabled=c.get("enabled", DEFAULT_CHECKPOINT_STORE_ENABLED),
        path=c.get("path", DEFAULT_CHECKPOINT_STORAGE_PATH),
        default_timeout_seconds=int(
            c.get("default_timeout_seconds", DEFAULT_CHECKPOINT_DEFAULT_TIMEOUT)
        ),
        sweep_interval_seconds=int(
            c.get("sweep_interval_seconds", DEFAULT_CHECKPOINT_SWEEP_INTERVAL)
        ),
    )


# ---------------------------------------------------------------------------
# v6.0 Phase 5 — Cluster Orchestrator config
# ---------------------------------------------------------------------------

DEFAULT_PORT_RANGE_START: int = 8090
DEFAULT_PORT_RANGE_END: int = 8200
DEFAULT_BLUEPRINTS_DIR: str = "./blueprints"
DEFAULT_CLUSTERS_STATE_PATH: str = "./clusters.json"


@dataclass(frozen=True)
class ClusterOrchestratorConfig:
    """Configuration for the ClusterOrchestrator (Phase 5)."""
    enabled: bool = True
    port_range_start: int = DEFAULT_PORT_RANGE_START
    port_range_end: int = DEFAULT_PORT_RANGE_END
    blueprints_dir: str = DEFAULT_BLUEPRINTS_DIR
    state_path: str = DEFAULT_CLUSTERS_STATE_PATH


def _parse_cluster_orchestrator_config(raw: dict) -> "ClusterOrchestratorConfig":
    """Parse the 'cluster_orchestrator:' section from node.yaml (Phase 5)."""
    c = raw.get("cluster_orchestrator", {}) or {}
    return ClusterOrchestratorConfig(
        enabled=c.get("enabled", True),
        port_range_start=int(c.get("port_range_start", DEFAULT_PORT_RANGE_START)),
        port_range_end=int(c.get("port_range_end", DEFAULT_PORT_RANGE_END)),
        blueprints_dir=c.get("blueprints_dir", DEFAULT_BLUEPRINTS_DIR),
        state_path=c.get("state_path", DEFAULT_CLUSTERS_STATE_PATH),
    )


# ---------------------------------------------------------------------------
# v6.0 Phase 7 — Telegram transport config parser
# ---------------------------------------------------------------------------

def _parse_telegram_config(raw: dict) -> "TelegramTransportConfig":
    """Parse the 'transports.telegram:' section from node.yaml."""
    transports = raw.get("transports", {}) or {}
    t = transports.get("telegram", {}) or {}
    return TelegramTransportConfig(
        enabled=t.get("enabled", DEFAULT_TELEGRAM_ENABLED),
        gnot_base_url=t.get("gnot_base_url"),
        token_encryption_key=t.get("token_encryption_key"),
        bots_storage_path=t.get("bots_storage_path", DEFAULT_TELEGRAM_BOTS_STORAGE_PATH),
        default_message_format=t.get("default_message_format", DEFAULT_TELEGRAM_MESSAGE_FORMAT),
        default_language=t.get("default_language", DEFAULT_TELEGRAM_LANGUAGE),
        poll_interval_seconds=int(t.get("poll_interval_seconds", DEFAULT_TELEGRAM_POLL_INTERVAL_SECONDS)),
        poll_timeout_seconds=int(t.get("poll_timeout_seconds", DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS)),
        api_timeout_seconds=int(t.get("api_timeout_seconds", DEFAULT_TELEGRAM_API_TIMEOUT_SECONDS)),
    )
