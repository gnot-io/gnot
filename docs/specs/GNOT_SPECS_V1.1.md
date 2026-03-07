# Generative Node Orchestration Technology (GNOT)
## Full Architecture Specification v1.1

> *"A distributed execution mesh that any LLM can orchestrate natively — no SDK, no workflow code, no pre-provisioning."*

**Author:** Viet Tran  
**Organization:** gnot-io  
**Repository:** github.com/gnot-io/gnot  
**Website:** gnot.io  
**Version:** 1.1 (updated from v1.0 — reflects production experience and LLM interaction model clarifications)  
**Status:** Production-ready reference implementation

---

## Abstract

Generative Node Orchestration Technology (GNOT) defines an architecture for
**LLM-native distributed execution**: a uniform HTTP interface that any
tool-calling-capable LLM can use to discover and orchestrate a self-extending mesh
of heterogeneous compute nodes — with no framework SDK, no workflow DSL, and no
pre-written automation code.

The system bootstraps from a single *seed node* carrying three irreducible primitive
capabilities — `read_file`, `write_file`, and `execute_command` — and propagates
itself across any reachable machine over HTTP. Complex multi-step workflows are
expressed as natural-language intents, decomposed autonomously by a ReAct agent loop,
and executed across nodes that may span multiple NAT boundaries.

**Primary interaction model:** An external LLM — such as Claude accessed via its
web interface — acts as orchestrator by calling `POST /action` against the mesh.
This requires zero LLM configuration on any node and gives the operator access to
the full capability surface of a frontier model (vision, voice, sandboxed execution,
long context) at subscription cost. Node-local LLM orchestration via `POST /intent`
is an additive option for embedded or offline deployments.

This document provides the definitive architectural specification covering node
anatomy, communication protocols, routing semantics, capability discovery,
authentication, session management, file transfer, and deployment models.

---

## Table of Contents

1. [Design Philosophy](#1-design-philosophy)
2. [Positioning Among Related Systems](#2-positioning-among-related-systems)
3. [System Architecture Overview](#3-system-architecture-overview)
4. [Node Anatomy](#4-node-anatomy)
5. [Action Model](#5-action-model)
6. [Request and Response Envelope](#6-request-and-response-envelope)
7. [Job Lifecycle](#7-job-lifecycle)
8. [Routing Engine](#8-routing-engine)
9. [Push/Pull Delivery Model](#9-pushpull-delivery-model)
10. [BGP-style Route Advertisement](#10-bgp-style-route-advertisement)
11. [Capability Discovery](#11-capability-discovery)
12. [Authentication and Authorization](#12-authentication-and-authorization)
13. [Intent Agent Loop](#13-intent-agent-loop)
14. [Session Management](#14-session-management)
15. [File Transfer System](#15-file-transfer-system)
16. [Credential System](#16-credential-system)
17. [Self-Bootstrapping Mechanism](#17-self-bootstrapping-mechanism)
18. [Observability and Health](#18-observability-and-health)
19. [Node Configuration Reference](#19-node-configuration-reference)
20. [Complete API Reference](#20-complete-api-reference)
21. [Data Models Reference](#21-data-models-reference)
22. [Security Model](#22-security-model)
23. [Deployment Topology](#23-deployment-topology)
24. [Evolution History and Design Decisions](#24-evolution-history-and-design-decisions)
24. [Known Limitations and Future Work](#24-known-limitations-and-future-work)

---

## 1. Design Philosophy

### 1.1 Core Principle

> **What interface should infrastructure expose so that any LLM can orchestrate it natively?**

GNOT's answer: a uniform `POST /action` endpoint, a self-describing capability tree
at `GET /capabilities`, and a single generic `mesh_action` tool schema. Together,
these allow any tool-calling-capable LLM to discover and operate the full mesh
without SDK integration, static tool registration, or pre-written workflow code.

The system begins as a single seed node with three irreducible capabilities.
Everything else — additional nodes, specialized compute, new actions, complex
orchestration — emerges at runtime through LLM reasoning over those three primitives.

This philosophy has four implications:

1. **No SDK required.** The LLM interacts with the mesh over plain HTTP. There is
   no framework to install, no agent library to import, no tool registration step.

2. **No provisioning layer.** The LLM *is* the provisioning layer. It writes
   configuration files, installs software, starts processes, and registers new nodes
   without any external orchestration tooling.

3. **No schema at origin.** The seed node does not know what it will eventually do.
   Capability schemas emerge as new actions are created and registered.

4. **No topology assumption.** The routing layer adapts to the actual network topology
   at registration time, not at design time.

### 1.2 LLM Interaction Model

GNOT supports two complementary interaction patterns. **Pattern A is the primary
and production-validated mode.**

#### Pattern A — External LLM (Primary)

An LLM operating outside the mesh acts as orchestrator. The operator provides the
LLM with the capability description from `GET /capabilities` and the `mesh_action`
tool schema. The LLM drives the mesh via `POST /action` calls, each specifying
`target_node_id`, action name, and parameters. No LLM is configured on any node.

```
External LLM (Claude Web, API, local model...)
    │
    │  POST /action  →  result
    ▼
GNOT Mesh (zero LLM configuration on any node)
```

**Why Pattern A is preferred in production:**

| Advantage | Detail |
|-----------|--------|
| **Richer capability surface** | Frontier models provide vision, voice, sandboxed code execution, long context — without changes to node configuration |
| **Cost efficiency** | Subscription-based access is substantially more economical than per-token API billing for sustained multi-step workloads |
| **Zero inference overhead** | Execution nodes remain lightweight and resource-predictable; no GPU or model serving infrastructure required |
| **Continuous improvement** | The mesh interface is stable; the orchestrating model improves automatically |

In the reference deployment, Claude — accessed via its web interface — serves as the
external orchestrator. This is not a limitation; it is the intended primary mode.

#### Pattern B — Node-local LLM (Additive)

When a node is configured with an `llm_provider`, it activates `IntentHandler` and
exposes `POST /intent`. A caller submits a free-text prompt; the node's LLM
decomposes it into `mesh_action` tool calls, dispatches them through `GatewayRouter`,
and returns a synthesized response. This pattern suits embedded agents, air-gapped
deployments, and per-node autonomous decision making.

```
Any HTTP Caller (app, bot, script...)
    │
    │  POST /intent
    ▼
GNOT Node (LLM configured)
    │
    │  tool calls ↕ actions
    ▼
Node-local LLM (API or local inference)
```

Both patterns use the same `GatewayRouter` and routing layer. They can coexist
in the same deployment.

### 1.3 Three Irreducible Primitives

The seed node carries exactly three capabilities because they are both necessary
and sufficient to provision any Linux environment:

| Primitive | Necessity |
|-----------|-----------|
| `read_file` | Required to observe persistent state — inspect configs, read logs, verify installations. Without it the LLM cannot see the machine's state. |
| `write_file` | Required to modify persistent state — write configs, install scripts, create node.yaml. Without it nothing can be configured. |
| `execute_command` | Required to activate — run installers, start services, invoke package managers. Without it nothing written to disk can run. |

Removing any one eliminates an irreducible category of operation. Together they
form a minimal complete basis: any Linux environment can be fully bootstrapped
from them alone.

### 1.4 Transparency Across Delivery Modes

Whether an action executes synchronously on a local node, asynchronously on a node
across the internet, or is routed through multiple NAT boundaries and intermediate
nodes, the calling interface is identical:

```
POST /action → optional job_id → GET /result/{job_id}
```

The LLM neither knows nor needs to know the delivery mode. This transparency is a
first-order design constraint, not a convenience feature.

### 1.5 Composition Over Configuration

GNOT does not define a workflow language, a DAG executor, or a pipeline DSL. Complex
multi-node workflows are composed dynamically by the LLM through sequential tool calls.
The framework provides primitives; the LLM provides composition.

---

## 2. Positioning Among Related Systems

Understanding what GNOT is requires understanding what it is not.

### 2.1 LLM Orchestration Frameworks (LangChain, LlamaIndex)

**LangChain** and **LlamaIndex** follow the *wrapper model*: tools are statically
registered Python functions; the LLM is a consumer of pre-provisioned infrastructure.
All tool execution happens within a single process on a single host. There is no
mechanism for an LLM to target a specific remote machine, traverse NAT, or provision
a new node at runtime.

GNOT differs in one fundamental respect: the mesh *is* the infrastructure, and the
LLM discovers and extends it dynamically. An LLM instruction to "install software
on machine X and register it as a new execution node" is a first-class operation
in GNOT; it is not expressible in LangChain or LlamaIndex without custom engineering.

### 2.2 Personal AI Agent Frameworks (OpenClaw, ZeroClaw)

**OpenClaw** (formerly Clawdbot/Moltbot) is an open-source personal AI assistant
that connects an LLM to messaging platforms (Telegram, WhatsApp, Slack, etc.) and
exposes shell, browser, file, and calendar operations as skills. It is architecturally
relevant to GNOT — both route natural-language requests through a local gateway and
dispatch them to execution primitives.

**ZeroClaw** is a Rust-native reimplementation of the OpenClaw architecture,
optimized for resource-constrained deployments (3.4 MB binary, sub-10 ms cold start).

Both frameworks are designed around the **single-machine personal assistant** model:
the Gateway is a service on one host, skills execute locally, and there is no concept
of remote node registration, cross-machine routing, or NAT-transparent distributed
execution. An OpenClaw or ZeroClaw agent cannot execute a command on a second private
machine and return the result — the class of use cases GNOT is designed for.

### 2.3 Summary Comparison

| Capability | LangChain | LlamaIndex | OpenClaw | ZeroClaw | **GNOT** |
|-----------|:---------:|:----------:|:--------:|:--------:|:--------:|
| Multi-machine execution | ✗ | partial | ✗ | ✗ | ✓ |
| NAT traversal | ✗ | ✗ | ✗ | ✗ | ✓ |
| Dynamic node provisioning | ✗ | ✗ | ✗ | ✗ | ✓ |
| External LLM orchestration | ✓ | ✓ | ✓ | ✓ | ✓ |
| Node-local LLM (optional) | ✗ | ✗ | ✓ | ✓ | ✓ |
| Zero workflow code required | ✗ | ✗ | partial | partial | ✓ |
| Binary file transfer across NAT | ✗ | ✗ | ✗ | ✗ | ✓ |
| Self-bootstrapping from 3 primitives | ✗ | ✗ | ✗ | ✗ | ✓ |

---



### 2.1 Topology

```
                      ┌──────────────────────────────────────────────────┐
                      │              Public Internet                      │
                      │   (Cloudflare Tunnel / Direct Public IP)         │
                      └──────────────────────┬───────────────────────────┘
                                             │
     Claude Web ──────────────────────────── │
     Telegram Bot ──────────────────────── ──│
     External HTTP Client ──────────────── ──┤
                                             │
              ┌──────────────────────────────▼──────────────────────────┐
              │                   Gateway Node (deb-0)                   │
              │                                                          │
              │   ┌─────────────────────┐   ┌──────────────────────┐   │
              │   │ POST /action         │   │ POST /intent          │   │
              │   │ GET  /result/{id}    │   │ (ReAct Agent Loop)   │   │
              │   │ GET  /capabilities   │   │                      │   │
              │   └──────────┬──────────┘   └──────────────────────┘   │
              │              │ GatewayRouter                            │
              │   ┌──────────▼──────────────────────────────────────┐  │
              │   │  NodeRegistry   JobQueue   CredentialStore       │  │
              │   └──────────┬──────────────────────────────────────┘  │
              └──────────────┼──────────────────────────────────────────┘
                             │ pull queue (HTTP outbound to internet)
              ┌──────────────┼──────────────────────────────────────────┐
              │              │           Private Network / NAT           │
              │   ┌──────────▼─────────────────────────────────────┐   │
              │   │   Worker Node (cen-0)     WorkerAgent polls ──→ │   │
              │   │   auth_token: per-node                          │   │
              │   │   actions: execute_command, read_file, ...      │   │
              │   └────────────────────────────────────────────────┘   │
              │   ┌─────────────────────────────────────────────────┐  │
              │   │   Worker Node (alm-0)     WorkerAgent polls ──→ │  │
              │   └─────────────────────────────────────────────────┘  │
              │   ┌──────────────────────────────────────────────────┐  │
              │   │   Worker Node (cfsnk-0)   WorkerAgent polls ──→  │  │
              │   │   ┌────────────────────────────────────────────┐  │  │
              │   │   │  sub-node (cfsnk-0a)   deeper NAT layer    │  │  │
              │   │   └────────────────────────────────────────────┘  │  │
              │   └──────────────────────────────────────────────────┘  │
              └─────────────────────────────────────────────────────────┘
```

### 2.2 Principal Components

| Component | Description |
|-----------|-------------|
| **Gateway Node** | Public-facing GNOT node. Receives intent and action requests. Maintains NodeRegistry, JobQueue, CredentialStore. Routes to workers. Hosts LLM client and IntentHandler. |
| **Worker Node** | Private GNOT node behind NAT. Runs WorkerAgent that polls gateway for jobs. Executes actions locally and reports results. |
| **Seed Actions** | The three bootstrap primitives: `execute_command`, `read_file`, `write_file`. Present on every node. |
| **WorkerAgent** | Background asyncio service inside each worker. Manages registration, heartbeats, job polling, execution, and result reporting. |
| **GatewayRouter** | Request router on every node. Decides: execute local / push to remote / pull-enqueue / forward via next-hop. |
| **NodeRegistry** | Registry of known nodes on the gateway. Tracks addresses, last heartbeat, online status, capabilities, and routing entries (next-hop). |
| **JobQueue** | Per-node pull queue on the gateway. Holds jobs for workers that cannot be reached directly. |
| **IntentHandler** | ReAct agent loop on the gateway. Converts free-text prompts into sequences of mesh actions using an embedded LLM tool-call loop. |
| **ConversationStore** | Per-session message history for multi-turn intent conversations. |
| **CredentialStore** | Encrypted, persistent store for per-session caller credentials. |
| **UploadManager** | Staged file store on the gateway enabling binary file transfer between nodes via upload/download endpoints. |

---

## 3. Node Anatomy

### 3.1 Directory Structure

Every GNOT node has the following structure:

```
/opt/mesh/<node-id>/
├── node.yaml            # Node configuration
└── actions/
    ├── execute_command.py
    ├── execute_command.schema.json
    ├── read_file.py
    ├── read_file.schema.json
    ├── write_file.py
    ├── write_file.schema.json
    └── <custom_action>.py          # optional
    └── <custom_action>.schema.json # optional
```

The runtime itself (`node_runtime.py`, `runtime/`) is shared across all nodes and
installed once per machine.

### 3.2 Node Runtime Startup

```
node_runtime.py --config /opt/mesh/<node-id>/node.yaml
  │
  ├─ load_config()              → NodeConfig
  ├─ load_actions(actions_dir)  → ActionRegistry
  ├─ load_schemas(actions_dir)  → SchemaRegistry
  ├─ create_app(config, ...)    → FastAPI app
  │      ├─ AuthMiddleware      (Bearer token enforcement)
  │      ├─ CORSMiddleware
  │      ├─ request logging middleware
  │      ├─ lifespan:
  │      │    startup: job_manager.start_cleanup_loop()
  │      │             credential_store.load()
  │      │             credential_store.start_flush_task()
  │      │    shutdown: stop_cleanup_loop()
  │      │              credential_store.stop_flush_task() + final flush
  │      └─ all route handlers
  │
  ├─ if config.is_worker:
  │      attach_worker_agent(app, config, registry, schema_registry)
  │        → WorkerAgent(config, executor, action_registry, schema_registry)
  │        → asyncio background tasks: _register_loop, _heartbeat_loop, _poll_loop
  │
  └─ uvicorn.run(app, host, port)
```

### 3.3 Node Modes

A node can operate in one or more modes simultaneously:

| Mode | Condition | Role |
|------|-----------|------|
| **Standalone** | No `trusted_nodes`, no `gateway_node_id` | Executes actions locally only |
| **Gateway** | `trusted_nodes` configured | Accepts worker registrations, routes requests, hosts IntentHandler |
| **Worker** | `gateway_node_id` + `gateway_address` configured | Polls gateway, executes jobs, reports results |
| **Gateway+Worker** | Both conditions | Intermediate node — routes to sub-nodes and is itself a worker |

---

## 4. Action Model

### 4.1 Seed Actions

Three primitive actions are present on every node and form the bootstrap kernel:

#### `execute_command`

```json
{
  "action": "execute_command",
  "params": {
    "command": "string",           // required — shell command
    "timeout_seconds": 60          // optional, default 60, max 3600
  }
}
```

Output:
```json
{
  "exit_code": 0,
  "stdout": "...",
  "stderr": ""
}
```

This action is the most powerful primitive. It enables: installing software, starting
processes, creating directories, running scripts, building containers, and bootstrapping
new GNOT nodes. From a single `execute_command`, the entire mesh can be created.

#### `read_file`

```json
{
  "action": "read_file",
  "params": {
    "path": "string"               // required — absolute or relative path
  }
}
```

Output: `{ "content": "..." }`

#### `write_file`

```json
{
  "action": "write_file",
  "params": {
    "path": "string",              // required
    "content": "string"            // required
  }
}
```

Creates parent directories if they do not exist. Output: `{ "success": true }`

### 4.2 Plugin Action Contract

All non-seed actions are Python modules placed in the node's `actions/` directory.
The framework loads them at startup and calls them via the plugin contract:

```python
# Synchronous action
def run(params: dict, context: dict) -> dict:
    ...

# Asynchronous action (declare ASYNC = True)
ASYNC = True

async def run(params: dict, context: dict) -> dict:
    ...
```

**The `context` object** is injected by the runtime:

```python
context = {
    "task_id":            str,          # global workflow ID
    "job_id":             str | None,   # execution ID (async only)
    "node_id":            str,          # this node's ID
    "config":             NodeConfig,   # full node config
    "logger":             Logger,       # pre-configured logger
    "storage":            str,          # temp dir path
    "temp_dir":           str,          # temp dir path (alias)
    "llm":                LLMClient,    # shared LLM client (if configured)
    "caller_credentials": dict[str, str], # caller-supplied credentials
}
```

### 4.3 Action Schema

Each action may have a co-located JSON Schema file `{action_name}.schema.json`.
Schema files serve three purposes:

1. **Validation** — params are validated against the schema before execution (HTTP 422 on failure)
2. **LLM guidance** — params are exposed in `GET /capabilities` and the IntentHandler system prompt
3. **Credential declaration** — the `x-caller-credentials` extension declares required caller-supplied keys

Schema example:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "get_order_info",
  "description": "Retrieve order details from the CRM system.",
  "x-caller-credentials": {
    "crm_user_token": {
      "description": "CRM API key issued to the user by admin",
      "required": true,
      "hint": "Obtain from CRM admin panel under Settings > API Keys"
    }
  },
  "type": "object",
  "properties": {
    "order_id": {
      "type": "string",
      "minLength": 1,
      "description": "Order ID, e.g. ORD-123"
    }
  },
  "required": ["order_id"],
  "additionalProperties": false
}
```

`x-caller-credentials` is a vendor extension (JSON Schema Draft 7 permits `x-` prefix
extensions). Standard JSON Schema validators ignore it; the GNOT framework uses it to
enforce credential presence before dispatching actions.

### 4.4 Action Loader

At startup, the runtime scans `actions/`:

```python
action_registry = {}
schema_registry = {}

for py_file in actions_dir.glob("*.py"):
    module = importlib.import_module(py_file.stem)
    action_registry[py_file.stem] = module

for json_file in actions_dir.glob("*.schema.json"):
    action_name = json_file.stem.removesuffix(".schema")
    schema_registry[action_name] = json.loads(json_file.read_text())
```

Actions without schemas pass validation unconditionally.

---

## 5. Request and Response Envelope

### 5.1 Action Request

```json
{
  "target_node_id":    "string",        // required
  "payload": {
    "action":          "string",        // required
    "params":          {}               // required (may be empty)
  },
  "task_id":           "string",        // optional — auto-generated if absent
  "trace": {
    "hop_count":       0,               // optional — default 0
    "route_path":      []               // optional — default []
  },
  "caller_credentials": {}              // optional — caller-supplied auth keys
}
```

The server auto-generates `task_id` (`"task-<uuid4[:12]>"`) and initializes
`trace` defaults when absent. This allows minimal caller payloads:

```json
{
  "target_node_id": "cfsnk-0",
  "payload": {
    "action": "execute_command",
    "params": { "command": "df -h", "timeout_seconds": 10 }
  }
}
```

### 5.2 Synchronous Response

Returned when the action completes within the request lifecycle:

```json
{
  "task_id":   "task-abc123",
  "status":    "completed",
  "output":    { "exit_code": 0, "stdout": "...", "stderr": "" }
}
```

### 5.3 Asynchronous Response

Returned when the action is dispatched but not yet complete:

```json
{
  "task_id":                  "task-abc123",
  "job_id":                   "node0-job-00023",
  "status":                   "accepted",
  "estimated_completion_seconds": 30
}
```

**The presence of `job_id` is the sole indicator of async mode.** Callers do not
set a mode flag — the runtime decides based on action type and delivery path.

### 5.4 Error Response

```json
{
  "error":  "ERROR_CODE",
  "detail": "Human-readable explanation"
}
```

Standard error codes:

| Code | HTTP | Meaning |
|------|------|---------|
| `UNAUTHORIZED` | 401 | Missing Bearer token |
| `FORBIDDEN` | 403 | Invalid Bearer token |
| `UNTRUSTED_NODE` | 400 | Target node not in trusted list |
| `NODE_NOT_FOUND` | 400 | Cannot resolve target address |
| `MAX_HOP_EXCEEDED` | 400 | `hop_count > max_hop` |
| `LOOP_DETECTED` | 400 | Node already in `route_path` |
| `ACTION_NOT_FOUND` | 400 | No action with that name registered |
| `SCHEMA_VALIDATION_ERROR` | 422 | Params failed JSON Schema validation |
| `CALLER_NOT_ALLOWED` | 400 | Token not in caller_policies or action not permitted |
| `MISSING_CALLER_CREDENTIAL` | 400 | Action requires credential not present |
| `JOB_NOT_FOUND` | 404 | No job with that ID |
| `LLM_NOT_CONFIGURED` | 503 | POST /intent called but no LLM API key set |

---

## 6. Job Lifecycle

### 6.1 Job States

```
accepted → running → completed
                   ↘ failed
```

| State | Meaning |
|-------|---------|
| `accepted` | Job received, not yet started |
| `running` | Execution in progress |
| `completed` | Finished successfully |
| `failed` | Finished with error |
| `queued` | Awaiting pull by worker (pull-mode jobs only) |

### 6.2 Job IDs

- **Task ID** — global workflow identifier, created by the caller (or auto-generated).
  Spans the entire logical operation, potentially across many nodes.
  Format: `"task-<12-hex-chars>"`

- **Job ID** — execution identifier, created by the node that owns the job.
  Scoped to a single node's JobManager.
  Format: `"<node-id>-job-<5-digit-seq>"`

A single task may spawn many jobs:
```
Task T1
 ├── Job J1 (node-0, mkdir)
 ├── Job J2 (cen-0, mysqldump)
 ├── Job J3 (node-0, upload)
 └── Job J4 (alm-0, mysql restore)
```

### 6.3 Polling

```
GET /result/{job_id}
```

Response while running:
```json
{ "task_id": "...", "job_id": "...", "status": "running", "progress": 45 }
```

Response when complete:
```json
{ "task_id": "...", "job_id": "...", "status": "completed", "output": {...} }
```

### 6.4 Job TTL and Cleanup

Completed and failed jobs are retained for `job_ttl_seconds` (default 3600s) then
purged by a background cleanup task running every `cleanup_interval_seconds` (default 60s).
Jobs in terminal state (`completed`, `failed`) track a `completed_time` timestamp for TTL calculation.
Active jobs (`accepted`, `running`, `queued`) are never purged.

### 6.5 Pull Job Timeout

A job that has been enqueued for a worker (status `queued`) but not claimed within
`pull_job_timeout_seconds` (default 300s) is automatically marked `failed` with error
`"Pull job timed out after Xs — no worker claimed it"`. This check is applied lazily
at `GET /result/{job_id}` time, consistent with the system-wide lazy evaluation pattern.

---

## 7. Routing Engine

### 7.1 GatewayRouter Decision Tree

Every node runs a `GatewayRouter`. When `POST /action` is received:

```
route(request):
  1. HOP GUARD
     if request.trace.hop_count > config.max_hop:
         return MAX_HOP_EXCEEDED

  2. LOOP GUARD
     if config.node_id in request.trace.route_path:
         return LOOP_DETECTED

  3. LOCAL EXECUTION
     if request.target_node_id == config.node_id:
         return execute_local(request)

  4. NEXT-HOP ROUTING (v5.10 — multi-hop NAT)
     next_hop = node_registry.get_next_hop(request.target_node_id)
     if next_hop and next_hop != request.target_node_id:
         return _forward_via_next_hop(request, next_hop)

  5. TRUSTED CHECK
     if not node_registry.is_trusted(request.target_node_id):
         return UNTRUSTED_NODE

  6. PUSH OR PULL
     address = node_registry.get_address(request.target_node_id)
     if not address:
         return _pull(request)
     reachable = await node_registry.ping(request.target_node_id)
     if reachable:
         result = await _push(address, request)
         if isinstance(result, error):
             return await _pull(request)    # transparent fallback
         return result
     else:
         return _pull(request)
```

### 7.2 Route Result

`GET /result/{job_id}` is also routed:

```
route_result(job_id):
  1. Check local JobManager (covers LOCAL + PULL jobs)
  2. Check job_queue routing table (covers PUSH jobs → proxy to worker)
  3. If pull job is QUEUED → apply lazy timeout check
  4. NOT_FOUND if no match
```

### 7.3 Hop Counting and Loop Prevention

Each forward increments `trace.hop_count` and appends the current node to
`trace.route_path`. For a 3-layer topology:

```
Request arrives at node-0: hop_count=0, route_path=[]
  node-0 forwards to node-1: hop_count=1, route_path=["node-0"]
    node-1 forwards to node-1a: hop_count=2, route_path=["node-0","node-1"]
      node-1a executes locally
```

With `max_hop=10`, a network of 10 intermediate layers is supported.

---

## 8. Push/Pull Delivery Model

The Push/Pull model solves the fundamental NAT traversal problem: worker nodes behind
NAT cannot receive inbound connections from the gateway.

### 8.1 Push Mode

Used when: worker is directly reachable (intranet, or `self_address` set and `ping` succeeds).

```
Gateway POST /action
    → ping worker (GET /ping) → 200 OK
    → POST worker/action
    → receive result directly
    → return result to caller
```

### 8.2 Pull Mode

Used when: worker is behind NAT, or `self_address` is not set, or `ping` fails.

**Enqueue phase (gateway):**
```
Gateway POST /action
    → no address or ping fails
    → JobQueue.enqueue(target_node_id, ...)
    → return { job_id, status: "accepted" } to caller immediately
```

**Poll-claim-execute-report cycle (worker):**
```
WorkerAgent (every poll_interval_seconds, default 5s):
    → GET gateway/jobs/poll?node_id=<id>
    → receive list of queued jobs
    → for each job:
        POST gateway/jobs/{job_id}/claim   ← atomic (first-write-wins)
        execute action locally
        POST gateway/jobs/{job_id}/result
```

**Result retrieval (caller):**
```
GET /result/{job_id}
    → gateway JobManager has the job (gateway owns pull-mode job state)
    → returns current status: queued / running / completed / failed
```

### 8.3 Transparent Fallback

If push fails (network error, action error), the gateway falls back to pull
transparently. The caller's interface does not change — it always observes a
`job_id` for async delivery.

### 8.4 WorkerAgent

The `WorkerAgent` runs three concurrent asyncio tasks inside each worker node:

| Task | Interval | Action |
|------|----------|--------|
| Registration | Once at startup (12 retries) | `POST /nodes/register` with action list and capability tree |
| Heartbeat loop | `heartbeat_interval_seconds` (default 15s) | `POST /nodes/{node_id}/heartbeat` |
| Poll loop | `poll_interval_seconds` (default 5s) | Poll → claim → execute → report |
| Re-registration loop | `heartbeat_interval_seconds × 6` (default 90s) | Re-register with current live routes (triggers route withdrawal on gateway) |

### 8.5 Claim Atomicity

Multiple worker replicas (or two workers racing) calling
`POST /jobs/{job_id}/claim` concurrently are safe: only the first caller
succeeds. Subsequent callers receive an error and skip the job. This is enforced
by an asyncio lock inside `JobQueue`.

---

## 9. BGP-style Route Advertisement

### 9.1 Problem: Multi-layer NAT Routing

Without route advertisement, a gateway can only route to its *direct* worker children.
Consider:

```
Gateway (node-0)
    └── node-1 (direct worker, NAT layer 1)
            └── node-1a (sub-worker, NAT layer 2, has get_order_info)
```

Calling `POST /action { target: "node-1a" }` on node-0 fails with `UNTRUSTED_NODE`:
node-0's NodeRegistry has no entry for node-1a.

### 9.2 BGP Analogy

The Internet solves analogous routing problems with BGP: each Autonomous System
*advertises* the IP prefixes it can reach. Neighbors learn these routes and propagate
them further. Routers only need to know the *next hop*, not the full path.

GNOT maps this pattern:

| BGP Concept | GNOT Concept |
|-------------|-------------|
| Autonomous System | Node |
| IP prefix | Node ID |
| BGP neighbor | Gateway node |
| Route advertisement | `advertise_routes` in `POST /nodes/register` |
| Next-hop router | `next_hop` field in NodeRegistry entry |
| BGP UPDATE message | Periodic re-registration |
| BGP WITHDRAW message | Re-registration with smaller `advertise_routes` |

### 9.3 Advertisement Flow

When node-1a registers with node-1:

```
1. node-1a → POST node-1/nodes/register {
       node_id: "node-1a",
       actions: ["execute_command", "get_order_info"],
       action_specs: { "get_order_info": { description, params_schema, ... } }
   }

2. node-1 NodeRegistry: install entry node-1a (direct child, next_hop=null)
   node-1 WorkerAgent: update _sub_routes["node-1a"] = SubRouteInfo(...)
   node-1 WorkerAgent: re-register with node-0:
     POST node-0/nodes/register {
         node_id: "node-1",
         actions: ["execute_command"],
         advertise_routes: ["node-1a"],
         capabilities: { "node-1a": ["execute_command", "get_order_info"] },
         sub_route_specs: { "node-1a": { "get_order_info": {...} } }
     }

3. node-0 NodeRegistry: install entry node-1a (next_hop="node-1")
   node-0 GET /capabilities now shows node-1a with full action specs ✓
```

### 9.4 Multi-hop Propagation

For N layers, each intermediate node re-advertises all known sub-routes upward.
Crucially, each node only needs to know its *direct next hop*:

```
node-0 knows: node-1a → next_hop="node-1"
node-1 knows: node-1a → next_hop=null (direct child)
```

node-0 does not need to know node-1 exists as an intermediate — it just forwards
requests for node-1a to node-1, and node-1 handles the rest.

### 9.5 Route Withdrawal

When node-1a goes offline, node-1 detects staleness via lazy heartbeat check and
excludes it from the next periodic re-registration. node-0 compares the new
advertisement against its existing entries:

```python
async def withdraw_routes(self, via_node_id: str, keep_routes: list[str]) -> list[str]:
    withdrawn = [
        nid for nid, entry in _entries.items()
        if entry.next_hop == via_node_id and nid not in keep_routes
    ]
    for nid in withdrawn:
        del _entries[nid]
    return withdrawn
```

This is called by the `/nodes/register` handler every time a node re-registers.
After withdrawal, `GET /capabilities` no longer shows node-1a, and the IntentHandler
system prompt marks it as unreachable or omits it entirely.

### 9.6 Action Specs Cascade

Action specifications (`description`, `params_schema`, `x-caller-credentials`) must
propagate through multi-hop re-advertisement to be visible at the top-level gateway.
This is achieved via the `sub_route_specs` field in the registration payload.

Data structure on WorkerAgent:

```python
@dataclass
class SubRouteInfo:
    actions: list[str]
    action_specs: dict[str, dict] = field(default_factory=dict)

_sub_routes: dict[str, SubRouteInfo]  # sub_node_id → SubRouteInfo
```

When node-1 re-registers, it includes `sub_route_specs` containing the full specs
for every sub-node it knows about. node-0 installs these specs into the corresponding
NodeRegistry entries. The result: LLM calling `GET /capabilities` on node-0 sees
complete specs for node-1a's `get_order_info`, including required params and
credential requirements.

---

## 10. Capability Discovery

### 10.1 GET /capabilities

The primary discovery endpoint. Returns the full capability tree of the node and all
nodes reachable through it:

```json
{
  "node_id":  "node-0",
  "actions":  ["execute_command", "read_file", "write_file"],
  "reachable": {
    "cen-0": {
      "node_id":     "cen-0",
      "actions":     ["execute_command", "read_file", "write_file"],
      "action_specs": {
        "execute_command": {
          "description": "Execute a shell command and return stdout, stderr, exit_code.",
          "params_schema": {
            "command":         { "type": "string", "description": "Shell command" },
            "timeout_seconds": { "type": "integer", "default": 60 }
          },
          "caller_credentials": {},
          "async_action": true
        }
      },
      "status":   "online",
      "next_hop": null
    },
    "cfsnk-0a": {
      "node_id":     "cfsnk-0a",
      "actions":     ["get_order_info"],
      "action_specs": {
        "get_order_info": {
          "description": "Retrieve order details from CRM.",
          "params_schema": { "order_id": { "type": "string" } },
          "caller_credentials": {
            "crm_user_token": {
              "description": "CRM API key",
              "required": true,
              "hint": "Obtain from CRM admin panel"
            }
          },
          "async_action": false
        }
      },
      "status":   "online",
      "next_hop": "cfsnk-0"
    }
  }
}
```

Fields:
- `status` — `"online"` | `"unreachable"` | `"unknown"` (lazy staleness check applied at query time)
- `next_hop` — `null` for direct children; `"node-id"` for advertised sub-nodes (transparent to caller)
- `action_specs` — full spec cascade including params and credential requirements

### 10.2 GET /nodes

```json
{
  "nodes": [
    {
      "node_id":        "cen-0",
      "address":        null,
      "status":         "online",
      "last_heartbeat": 1741099560.3
    }
  ]
}
```

### 10.3 GET /skills (legacy)

Returns raw Markdown describing the node's capabilities. Retained for backward
compatibility with v5.0–v5.5 Claude Web workflows that injected skills as context.

---

## 11. Authentication and Authorization

### 11.1 HTTP Layer: AuthMiddleware

Every request to a non-exempt endpoint must carry a Bearer token:

```
Authorization: Bearer <token>
```

**Exempt paths** (no auth required):
```
GET /health      — health check
GET /ping        — reachability probe (gateway needs to probe workers without token)
GET /skills      — capability discovery
GET /setup.sh    — setup script (for bootstrapping new workers)
GET /runtime-bundle — runtime download (for bootstrapping new workers)
GET /docs        — Swagger UI
GET /openapi.json
```

**Auth enforcement** (implemented as FastAPI middleware):
- No tokens configured → open mode, all requests allowed
- Single `auth_token` → single shared token
- `allowed_tokens` list → per-node token model (see §11.3)
- Invalid or missing token → 401 / 403

Comparison uses `secrets.compare_digest` (constant-time) to prevent timing attacks.

### 11.2 Per-Node Token Model

Each worker has its own Bearer token, distinct from other workers. The gateway
maintains a list of all accepted tokens:

**Gateway config (`node.yaml`):**
```yaml
allowed_tokens:
  - "<TOKEN_CEN_0>"
  - "<TOKEN_ALM_0>"
  - "<TOKEN_CFSNK_0>"
  - "<TOKEN_EXTERNAL>"   # for external callers (Claude Web, curl, etc.)
```

**Worker config:**
```yaml
auth_token: "<TOKEN_CEN_0>"           # incoming requests to this node
gateway_auth_token: "<TOKEN_CEN_0>"   # outbound requests from this node to gateway
```

`gateway_auth_token` overrides `auth_token` for outbound calls. In most deployments
they are identical; they are separate fields to support cases where a worker accepts
a different token from internal peers than the token it presents to the gateway.

**Token revocation** is achieved by removing a single token from `allowed_tokens`
and restarting the gateway — no cluster-wide token rotation required.

**Token generation:** Use `openssl rand -hex 32` (256-bit entropy). Do not use UUID4
(only 122-bit effective entropy).

### 11.3 Action-Level Authorization: Caller Policies

Beyond HTTP-layer auth, individual actions can be restricted per-caller-token via
`caller_policies` in `node.yaml`:

```yaml
caller_policies:
  - token: "sk-sales-team"
    allowed_actions:
      - get_order_info
      - list_orders

  - token: "sk-devops"
    allowed_actions:
      - execute_command
      - read_file
      - write_file

  - token: "sk-admin"
    allowed_actions: "*"    # wildcard — all actions permitted
```

**Evaluation logic:**

```python
def check_caller_policy(policies, caller_token, action):
    if not policies:
        return True    # open mode — no policy configured
    if caller_token is None:
        return False
    for policy in policies:
        if policy.token == caller_token:
            return policy.allows(action)    # wildcard or explicit list
    return False    # token not in any policy → denied
```

If `caller_policies` is empty, all callers with a valid Bearer token may invoke
any action (backward compatible with pre-v5.11 deployments).

### 11.4 Caller Credential Requirements

Some actions require caller-supplied credentials (distinct from service credentials
managed by the action itself). These are declared in the action's schema:

```json
{
  "x-caller-credentials": {
    "crm_user_token": {
      "description": "CRM API key",
      "required": true,
      "hint": "Obtain from CRM admin panel under Settings > API Keys"
    }
  }
}
```

At execution time, the framework:
1. Reads required credential keys from `x-caller-credentials`
2. Checks that `ActionRequest.caller_credentials` contains all required keys
3. If missing → returns `MISSING_CALLER_CREDENTIAL` error (HTTP 400)
4. If present → injects `caller_credentials` into `context["caller_credentials"]`

`caller_credentials` are **never logged, never stored in params, never appear
in response bodies, and never appear in conversation history** (see §15 for session
credential storage which handles persistence securely).

---

## 12. Intent Agent Loop

### 12.1 Overview

`POST /intent` accepts a free-text prompt and executes a ReAct (Reasoning + Acting)
loop internally, converting the intent into a sequence of mesh actions and returning
a synthesized natural-language result.

This enables GNOT to be driven directly from natural-language interfaces:
Telegram bots, web chat UIs, CLI tools, or any HTTP client — without the caller
needing to understand the mesh structure or action APIs.

### 12.2 ReAct Loop

```
POST /intent { prompt, session_id?, caller_credentials?, max_turns? }
    │
    ├─ get_or_create(session_id)                    → Session
    ├─ session.add_message("user", prompt)
    │
    └─ for turn in range(max_turns):
         │
         ├─ build_system_prompt(capability_tree)     → includes all nodes, actions, specs
         ├─ llm.chat(session.messages, tools=[MESH_TOOL_SPEC], temperature=0.2)
         │
         ├─ if response contains tool_calls:
         │    ├─ session.add_raw(assistant_message_with_tool_calls)
         │    ├─ for each tool_call:
         │    │    ├─ router.route(ActionRequest)      → execute action
         │    │    ├─ if async → poll until complete
         │    │    └─ session.add_raw({ role: "tool", content: result_json })
         │    └─ continue loop
         │
         └─ if response is plain text (no tool_calls):
              ├─ session.add_message("assistant", text)
              └─ return IntentResponse                → loop exits
```

**Loop termination conditions:**
- LLM produces a plain text reply (normal completion)
- `max_turns` reached → returns partial reply with `truncated: true`

### 12.3 The `mesh_action` Tool

The single tool provided to the LLM:

```json
{
  "type": "function",
  "function": {
    "name": "mesh_action",
    "description": "Execute an action on a specific node in the execution mesh.",
    "parameters": {
      "type": "object",
      "properties": {
        "target_node_id": { "type": "string" },
        "action":         { "type": "string" },
        "params":         { "type": "object" }
      },
      "required": ["target_node_id", "action", "params"]
    }
  }
}
```

The simplicity is intentional. The LLM constructs the complete call from the
capability tree in the system prompt — it already knows what params each action
requires, including type constraints and credential requirements.

### 12.4 System Prompt Construction

The IntentHandler builds a system prompt dynamically from the live capability tree:

```
You are an AI agent with access to an execution mesh.
Use the mesh_action tool to execute tasks.

## Mesh topology — nodes, actions, and requirements

  - node-0 [gateway, THIS NODE]
    ┌─ execute_command: Execute a shell command (stdout, stderr, exit_code)
    │  param command (string, required): Shell command to execute
    │  param timeout_seconds (integer): Max execution time (default 60)

  - cen-0 (direct)
    ┌─ execute_command: ...
    ┌─ read_file: ...

  - cfsnk-0a via cfsnk-0
    ┌─ get_order_info: Retrieve order details from CRM
    │  param order_id (string, required): Order ID, e.g. ORD-123
    │  ⚠ caller_credential crm_user_token (required): CRM API key
    │    hint: Obtain from CRM admin panel under Settings > API Keys

  - cfsnk-0b via cfsnk-0 [UNREACHABLE — do not call]
    ┌─ send_notification: ...

## Routing rules
Target ANY node listed above — routing is automatic through any NAT topology.
Simply set target_node_id to the node that has the action you need.

## Caller credentials
Actions marked ⚠ require a credential the caller must supply.
Check if caller_credentials already contains the required key.
If missing, ask the user for it once; do not ask again in the same session.
```

Nodes marked `[UNREACHABLE]` are known but currently unresponsive. The LLM
does not attempt to call them, avoiding 5-minute timeout waits.

### 12.5 Intent Response

```json
{
  "session_id":    "telegram-chat-12345",
  "reply":         "The disk usage on cfsnk-0 is 67% (/dev/sda1: 450G used of 670G). Filesystem is healthy.",
  "turns":         3,
  "actions_taken": ["execute_command on cfsnk-0"],
  "tokens_used":   1240,
  "truncated":     false
}
```

### 12.6 LLM Provider Compatibility

`LLMClient` uses the OpenAI API format. Any compatible endpoint works:

| Provider | `llm_base_url` | Notes |
|----------|----------------|-------|
| Anthropic (native) | `https://api.anthropic.com/v1` | Direct API support |
| OpenAI | `https://api.openai.com/v1` | Default |
| LiteLLM proxy | `http://localhost:4000/v1` | Multi-provider gateway |
| Ollama | `http://localhost:11434/v1` | Local LLM |
| vLLM | `http://localhost:8000/v1` | Self-hosted |
| Groq | `https://api.groq.com/openai/v1` | Fast inference |

Tool calling support is required. Models without tool calling support cannot drive
the ReAct loop.

---

## 13. Session Management

### 13.1 ConversationStore

Sessions are identified by `session_id` (any string — typically a Telegram `chat_id`,
a UUID, or any caller-supplied identifier). Sessions persist across multiple
`POST /intent` calls, enabling multi-turn conversations.

**Session structure:**
```python
@dataclass
class Session:
    session_id:   str
    messages:     list[dict]    # OpenAI-format message history
    created_at:   float
    last_active:  float
```

Sessions expire after `session_ttl_seconds` (default 3600s). TTL is checked lazily
at `get_or_create()` time — expired sessions are silently recreated.

### 13.2 Session API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/intent` | Create/continue session. `session_id` in body. |
| `GET`  | `/sessions/{session_id}` | Inspect session messages and metadata |
| `DELETE` | `/sessions/{session_id}` | Delete session (reset conversation context) |
| `GET` | `/sessions` | List all active sessions |

### 13.3 Conversation History Format

The raw message list follows the OpenAI multi-turn format including tool call
messages:

```json
[
  { "role": "user",      "content": "What is disk usage on cfsnk-0?" },
  { "role": "assistant", "content": null,
    "tool_calls": [{ "id": "call_abc", "function": { "name": "mesh_action",
      "arguments": "{\"target_node_id\":\"cfsnk-0\",\"action\":\"execute_command\",
                     \"params\":{\"command\":\"df -h\"}}" }}] },
  { "role": "tool",      "tool_call_id": "call_abc",
    "content": "{\"status\":\"completed\",\"output\":{\"exit_code\":0,\"stdout\":\"...\"}}"},
  { "role": "assistant", "content": "The disk usage on cfsnk-0 is..." }
]
```

`caller_credentials` are **never added to this message list** — they are managed
separately in the CredentialStore (§15).

---

## 14. File Transfer System

### 14.1 Problem

The seed action `write_file` writes content inline in a JSON payload. This is
suitable for text files up to ~1 MB. For binary files (database dumps, compressed
archives, media) at hundreds of megabytes, a different transfer mechanism is required.

A common use case: back up a database on a private worker, transfer through the
public gateway, and restore on a different private worker — both behind separate NATs.

### 14.2 UploadManager

The gateway runs an `UploadManager` that stores uploaded files on disk:

```
/tmp/mesh-uploads/
  <file_id>_<sanitized_filename>    ← binary file on disk
```

Files are referenced by a `file_id` (16-hex-char UUID fragment), not by content hash.
Two uploads of the same file produce independent entries with independent TTLs.

### 14.3 File Transfer API

#### `POST /upload`

```
Content-Type: multipart/form-data
Field: file (binary)
Header: X-Node-ID: <uploader-node-id>   (optional, for audit)

Response 201:
{
  "file_id":      "a3f9b2c1d4e5f678",
  "filename":     "backup_20260304.sql.gz",
  "size_bytes":   1073741824,
  "ttl_seconds":  3600,
  "download_url": "/download/a3f9b2c1d4e5f678"
}

Response 413: { "error": "FILE_TOO_LARGE", "max_size_mb": 512 }
```

Worker invocation via `execute_command`:
```bash
curl -s -X POST https://gateway/upload \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Node-ID: cen-0" \
  -F "file=@/tmp/backup.sql.gz"
```

#### `GET /download/{file_id}`

Streams the file as `application/octet-stream`. File is not loaded into memory
(FastAPI `FileResponse`). Returns 404 if expired or not found.

Worker invocation:
```bash
curl -s -OJ https://gateway/download/a3f9b2c1d4e5f678 \
  -H "Authorization: Bearer $TOKEN"
# -O: save to file; -J: use Content-Disposition filename
```

#### `GET /files`

Lists all staged uploads with metadata (file_id, filename, size, age, uploader).
Lazy TTL: expired files are removed and excluded from listing.

#### `DELETE /files/{file_id}`

Explicit deletion before TTL expiry.

### 14.4 Configuration

```yaml
upload_dir:          /tmp/mesh-uploads    # storage directory
upload_max_size_mb:  512                  # max single file (default 512 MB)
upload_ttl_seconds:  3600                 # auto-expiry (default 1 hour)
```

### 14.5 Example: Cross-NAT Database Backup/Restore

Complete multi-step workflow orchestrated by a single `POST /intent`:

```
1. POST /action → cen-0 → execute_command
     "mysqldump -u root -p$PASS mydb | gzip > /tmp/backup.sql.gz"

2. POST /action → cen-0 → execute_command
     "curl -s -X POST $GW/upload -H 'Authorization: Bearer $TOKEN'
      -H 'X-Node-ID: cen-0' -F 'file=@/tmp/backup.sql.gz'"
   → parse file_id from stdout

3. POST /action → alm-0 → execute_command
     "curl -s -OJ $GW/download/<file_id> -H 'Authorization: Bearer $TOKEN'"

4. POST /action → alm-0 → execute_command
     "gunzip -c /tmp/backup.sql.gz | mysql -u root -p$PASS targetdb"

5. DELETE /files/<file_id>
```

Steps 1–5 are orchestrated autonomously by the IntentHandler in response to:
*"Backup database mydb from cen-0 and restore it on alm-0."*

---

## 15. Credential System

### 15.1 Two Kinds of Credentials

GNOT distinguishes two distinct types of credentials:

| Type | Definition | Managed by |
|------|-----------|-----------|
| **Caller credentials** | Keys that the human caller supplies to authenticate to backend systems (CRM tokens, ERP API keys) | GNOT CredentialStore — transported in `caller_credentials` field |
| **Service credentials** | Keys that an action uses to connect to internal infrastructure (DB passwords, service accounts) | The action itself — read from `os.environ`, never visible to the framework |

This separation ensures the framework is never responsible for secret rotation
of infrastructure credentials, while still providing a secure channel for user-level
tokens.

### 15.2 CredentialStore

An in-memory (optionally persistent) encrypted store keyed by `session_id`:

```python
class CredentialStore:
    async def merge(session_id, credentials: dict[str, str]) → None
        # Upsert: new keys added, existing keys overwritten

    async def get(session_id) → dict[str, str]
        # Return all credentials for session (decrypted); {} if expired/not found

    async def touch(session_id) → None
        # Extend TTL (called on each IntentHandler turn)

    async def clear(session_id) → None
        # Explicit deletion (e.g. on logout)
```

**Session lifecycle in IntentHandler:**
```python
# On each POST /intent:
stored = await credential_store.get(session_id)
merged = { **stored, **request.caller_credentials }  # request creds take precedence
await credential_store.merge(session_id, request.caller_credentials)  # persist new
await credential_store.touch(session_id)             # extend TTL
# Use merged credentials for all tool calls in this turn
```

Result: the caller supplies credentials once on the first relevant `POST /intent`
call; subsequent calls in the same session reuse them automatically.

### 15.3 Encryption: AES-256-GCM

Credentials are encrypted at rest using AES-256-GCM:

**Key derivation:**
```
encryption_key_bytes = SHA-256( credential_encryption_key + ":" + session_id )
```

Per-session key: compromising one session's key does not affect others.

**Encryption:**
```
nonce = os.urandom(12)              # 96-bit nonce (GCM standard)
ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
stored = base64( nonce[12] || ciphertext || GCM_auth_tag[16] )
```

- AEAD property: integrity protected — any tampering invalidates the auth tag
- Random nonce: same plaintext encrypts to different ciphertext on each call
- Fallback (no `cryptography` package): base64 encoding without encryption, warning logged

**Root secret configuration:**
```yaml
credential_encryption_key: "stable-dedicated-key"   # separate from auth_token
```

When `credential_encryption_key` is set, rotating `auth_token` (periodic security
hygiene) does not invalidate stored credentials. When unset, falls back to `auth_token`
(backward compatible with earlier versions but discouraged in production).

**Fingerprint for audit:**
```python
store.encryption_key_fingerprint  # "a3f7b2c1" — first 8 hex chars of SHA-256(key)
```
Logged at startup for operator verification without exposing the key.

### 15.4 Credential Persistence

Opt-in persistence across process restarts:

```yaml
credential_store_path: "/var/lib/mesh/credentials.json"
```

**Architecture: write-through with atomic flush**

```
In-memory dict   ← all reads and writes
      │
      │  dirty flag set on merge/clear/touch
      │
Background task  (flush every FLUSH_INTERVAL_SECONDS = 5s)
      │  if dirty: serialize → write .tmp → os.replace(.tmp, path)
      ↓
/var/lib/mesh/credentials.json
```

Atomic rename ensures readers never see a partial file. The persisted file
contains only ciphertext — no plaintext credentials.

**Startup:** `await credential_store.load()` reads the file, skips expired entries,
skips entries that fail decryption (key mismatch). Starts background flush task.

**Shutdown:** `await credential_store.stop_flush_task()` cancels the background task
and performs a final synchronous flush before process exit.

---

## 16. Self-Bootstrapping Mechanism

### 16.1 Mechanism

The three seed actions (`execute_command`, `write_file`, `read_file`) are sufficient
to create any new node. The LLM orchestrates the following sequence:

```
Step 1 — Create directory structure
  execute_command: "mkdir -p /opt/mesh/node-B/actions"

Step 2 — Write configuration
  write_file: path=/opt/mesh/node-B/node.yaml, content=<node yaml>

Step 3 — Write custom actions
  write_file: path=/opt/mesh/node-B/actions/custom_action.py, content=<python>
  write_file: path=/opt/mesh/node-B/actions/custom_action.schema.json, content=<json>

Step 4 — Install dependencies (if needed)
  execute_command: "pip install <package> --break-system-packages"

Step 5 — Start node process
  execute_command: "nohup python /opt/mesh/node_runtime.py \
                     --config /opt/mesh/node-B/node.yaml &"

Step 6 — Verify health
  execute_command: "curl -sf http://localhost:8081/health"
```

### 16.2 One-Liner Worker Install

Any Linux machine can join the mesh with a single command:

```bash
curl -sSL https://gateway.example.com/setup.sh | bash -s -- \
    --node-id cen-0 \
    --auth-token "$TOKEN_CEN_0" \
    --systemd
```

`GET /setup.sh` returns a universal setup script (auto-injected with the gateway URL).
`GET /runtime-bundle` returns the runtime as a tar.gz. Both endpoints are auth-exempt
so they can be fetched before a token is available.

The setup script handles: OS detection (Debian/Ubuntu, CentOS/RHEL, AlmaLinux/Rocky,
Arch, Alpine), Python 3.10+ installation, virtualenv setup, runtime download,
node.yaml generation, systemd service creation.

### 16.3 POST /bootstrap

For programmatic node creation from within the mesh (used in automated workflows):

```json
{
  "node_id":     "node-B",
  "port":        8081,
  "actions":     [
    { "name": "custom_action", "code": "...", "schema": "..." }
  ],
  "pip_packages":  ["httpx", "pandas"],
  "auth_token":  "sk-nodeB-secret",
  "extra_nodes": { "node-0": "http://127.0.0.1:8080" }
}
```

The bootstrap engine executes the creation steps with **automatic rollback**: if any
step fails, all completed steps are undone in reverse order (process killed, directory
removed, config reverted). The caller receives either a `completed` or `rolled_back`
status with a list of completed/rolled-back steps.

### 16.4 Evolution Model

GNOT clusters evolve through four levels:

| Level | State | Trigger |
|-------|-------|---------|
| **L0 — Seed Only** | Single node, 3 actions | Initial deployment |
| **L1 — Functional Nodes** | Domain-specific nodes (DB, API, ML) | LLM-driven bootstrap |
| **L2 — Specialized Clusters** | GPU nodes, storage nodes, CPU-heavy workers | LLM-triggered scale-out |
| **L3 — Self-Optimization** | LLM replaces slow nodes, scales on queue depth | Autonomous operation |

---

## 17. Observability and Health

### 17.1 GET /health

```json
{
  "node_id":       "node-0",
  "status":        "healthy",
  "uptime_seconds": 3600.5,
  "actions_loaded": 5,
  "jobs_active":    2,
  "queue_depths": {
    "cen-0":    3,
    "alm-0":    0,
    "cfsnk-0":  1
  }
}
```

`queue_depths`: unclaimed jobs per worker node in the pull queue. Useful for:
- Detecting stuck workers (queue depth growing, worker offline)
- Triggering scale-out decisions (queue too deep → bootstrap new worker)
- General health monitoring

### 17.2 Lazy Staleness Detection

Node liveness is checked lazily — not by a background poller, but at the moment
a routing or status query touches an entry:

```python
def _apply_lazy_staleness(entry):
    if entry.status != ONLINE:
        return
    if entry.last_heartbeat is None:
        return    # never sent heartbeat — status unknown
    age = time.time() - entry.last_heartbeat
    if age > heartbeat_timeout_seconds:
        entry.status = UNREACHABLE
```

Applied in: `get_address()`, `get_status()`, `ping()`, `build_capability_tree()`.

This ensures routing decisions always use up-to-date liveness information without
requiring a separate background process.

---

## 18. Node Configuration Reference

### 18.1 Complete `node.yaml` Schema

```yaml
# ── Identity ─────────────────────────────────────────────────────────────────
node_id: string                    # required — unique identifier in the mesh
listen:  "0.0.0.0:8080"            # optional — default "0.0.0.0:8080"

# ── Actions ──────────────────────────────────────────────────────────────────
actions_dir: string                # optional — default: ./actions

# ── Authentication (HTTP layer) ───────────────────────────────────────────────
auth_token: string                 # single shared token (simple deployments)
allowed_tokens:                    # per-node tokens (multi-worker deployments)
  - string
  - string
gateway_auth_token: string         # token this node presents to its gateway
                                   # overrides auth_token for outbound calls

# ── Authorization (action layer) ──────────────────────────────────────────────
caller_policies:                   # optional — empty = open mode
  - token: string
    allowed_actions:
      - string                     # specific actions, or "*" for all

# ── Gateway mode ──────────────────────────────────────────────────────────────
trusted_nodes:                     # list of worker node IDs this gateway accepts
  - string
heartbeat_timeout_seconds: 120     # mark worker UNREACHABLE after N seconds
ping_timeout_seconds: 3.0          # timeout for active ping probe

# ── Worker mode ───────────────────────────────────────────────────────────────
gateway_node_id: string            # ID of the gateway node
gateway_address: string            # HTTP address of the gateway (e.g. https://gw.example.com)
self_address: string               # optional — HTTP address of this node (enable push mode)
heartbeat_interval_seconds: 15
poll_interval_seconds: 5

# ── LLM (required for POST /intent) ───────────────────────────────────────────
llm_api_key: string                # or nested under llm: section
llm_base_url: string               # OpenAI-compatible endpoint
llm_default_model: string
llm_timeout_seconds: 120
llm_extra_headers:                 # e.g. anthropic-version header
  string: string

# ── Session / Intent ──────────────────────────────────────────────────────────
session_ttl_seconds: 3600
intent_max_turns: 20

# ── Credential Store ──────────────────────────────────────────────────────────
credential_encryption_key: string  # AES-256 root key (separate from auth_token)
credential_store_path: string      # optional — persist credentials to this JSON file

# ── File Upload ───────────────────────────────────────────────────────────────
upload_dir: /tmp/mesh-uploads
upload_max_size_mb: 512
upload_ttl_seconds: 3600

# ── Job Management ────────────────────────────────────────────────────────────
job_ttl_seconds: 3600
cleanup_interval_seconds: 60
pull_job_timeout_seconds: 300

# ── Routing ───────────────────────────────────────────────────────────────────
max_hop: 10
```

---

## 19. Complete API Reference

### 19.1 Core Execution Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/action` | ✅ | Submit action. Returns sync result or `{ job_id }` for async |
| `GET`  | `/result/{job_id}` | ✅ | Poll job status |
| `POST` | `/intent` | ✅ | Free-text intent → ReAct agent loop → natural language reply |
| `POST` | `/bootstrap` | ✅ | Programmatic node creation with auto-rollback |

### 19.2 Capability Discovery

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET`  | `/capabilities` | ✅ | Full capability tree with action specs and node status |
| `GET`  | `/skills` | ❌ | Raw Markdown capability description (legacy) |
| `GET`  | `/resolve/{node_id}` | ✅ | Resolve node address |

### 19.3 Worker Registration and Heartbeat

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/nodes/register` | ✅ | Worker registers with gateway. Carries actions, advertise_routes, action_specs, sub_route_specs |
| `POST` | `/nodes/{node_id}/heartbeat` | ✅ | Worker liveness signal |
| `GET`  | `/nodes` | ✅ | List registered nodes with status |

### 19.4 Pull Job Queue

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET`  | `/jobs/poll` | ✅ | Worker polls for queued jobs (`?node_id=<id>`) |
| `POST` | `/jobs/{job_id}/claim` | ✅ | Atomic job claim |
| `POST` | `/jobs/{job_id}/result` | ✅ | Worker reports execution result |

### 19.5 Session Management

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET`  | `/sessions/{session_id}` | ✅ | Inspect session |
| `DELETE` | `/sessions/{session_id}` | ✅ | Delete session |
| `GET`  | `/sessions` | ✅ | List all sessions |

### 19.6 File Transfer

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/upload` | ✅ | Upload file (`multipart/form-data`) |
| `GET`  | `/download/{file_id}` | ✅ | Download file (streamed) |
| `GET`  | `/files` | ✅ | List staged uploads |
| `DELETE` | `/files/{file_id}` | ✅ | Delete upload |

### 19.7 Infrastructure

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET`  | `/health` | ❌ | Node health, jobs_active, queue_depths |
| `GET`  | `/ping` | ❌ | Reachability probe (200 `{ pong: true }`) |
| `GET`  | `/setup.sh` | ❌ | Universal worker setup script |
| `GET`  | `/runtime-bundle` | ❌ | Runtime as tar.gz |

---

## 20. Data Models Reference

### 20.1 ActionRequest

```python
class ActionRequest(BaseModel):
    target_node_id:    str
    payload:           ActionPayload
    task_id:           str | None = None           # auto-generated if absent
    trace:             TraceInfo | None = None      # auto-initialized if absent
    caller_credentials: dict[str, str] = {}         # never logged
    caller_token:      str | None = None            # extracted from Authorization header
```

### 20.2 NodeRegistrationRequest

```python
class NodeRegistrationRequest(BaseModel):
    node_id:           str
    address:           str | None = None
    actions:           list[str] = []
    advertise_routes:  list[str] = []
    capabilities:      dict[str, list[str]] = {}
    action_specs:      dict[str, dict] = {}          # this node's own action specs
    sub_route_specs:   dict[str, dict] = {}          # {sub_node_id: {action_name: spec}}
```

### 20.3 CapabilityNode

```python
class CapabilityNode(BaseModel):
    node_id:      str
    actions:      list[str]
    action_specs: dict[str, ActionSpec] = {}
    status:       str = "unknown"           # "online" | "unreachable" | "unknown"
    next_hop:     str | None = None         # null = direct child
    reachable:    dict[str, CapabilityNode] = {}
```

### 20.4 ActionSpec

```python
class ActionSpec(BaseModel):
    description:        str = ""
    params_schema:      dict[str, Any] = {}
    caller_credentials: dict[str, CallerCredentialSpec] = {}
    async_action:       bool = False
```

### 20.5 CallerCredentialSpec

```python
class CallerCredentialSpec(BaseModel):
    description: str
    required:    bool = True
    hint:        str = ""
```

### 20.6 QueuedJob

```python
class QueuedJob(BaseModel):
    job_id:              str
    task_id:             str
    target_node_id:      str
    action:              str
    params:              dict
    created_at:          float
    claimed_by:          str | None = None
    caller_credentials:  dict[str, str] = {}    # separate from params, not logged
    caller_token:        str | None = None
```

### 20.7 CallerPolicy

```python
@dataclass(frozen=True)
class CallerPolicy:
    token:           str
    allowed_actions: list[str] | str    # list or "*"

    def allows(self, action: str) -> bool:
        if self.allowed_actions == "*":
            return True
        return action in self.allowed_actions
```

### 20.8 NodeConfig

```python
@dataclass(frozen=True)
class NodeConfig:
    node_id:                      str
    host:                         str = "0.0.0.0"
    port:                         int = 8080
    actions_dir:                  str = "actions"
    auth_token:                   str | None = None
    allowed_tokens:               tuple = ()
    gateway_auth_token:           str | None = None
    trusted_nodes:                list = field(default_factory=list)
    heartbeat_timeout_seconds:    int = 120
    ping_timeout_seconds:         float = 3.0
    gateway_node_id:              str | None = None
    gateway_address:              str | None = None
    self_address:                 str | None = None
    heartbeat_interval_seconds:   int = 15
    poll_interval_seconds:        int = 5
    max_hop:                      int = 10
    job_ttl_seconds:              int = 3600
    cleanup_interval_seconds:     int = 60
    pull_job_timeout_seconds:     int = 300
    llm_api_key:                  str | None = None
    llm_base_url:                 str = "https://api.openai.com/v1"
    llm_default_model:            str = "gpt-4o-mini"
    llm_timeout_seconds:          int = 120
    llm_extra_headers:            dict = field(default_factory=dict)
    session_ttl_seconds:          int = 3600
    intent_max_turns:             int = 10
    upload_dir:                   str = "/tmp/mesh-uploads"
    upload_max_size_mb:           int = 512
    upload_ttl_seconds:           int = 3600
    credential_encryption_key:    str | None = None
    credential_store_path:        str | None = None
    caller_policies:              tuple = ()

    @property
    def is_gateway(self) -> bool:
        return bool(self.trusted_nodes)

    @property
    def is_worker(self) -> bool:
        return bool(self.gateway_node_id and self.gateway_address)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)
```

---

## 21. Security Model

### 21.1 Threat Model

GNOT is designed for **trusted operator environments** where the people operating the
mesh are trusted, and the primary threat is external unauthorized access and accidental
misconfiguration. It is not designed to isolate untrusted code execution in a
multi-tenant environment.

### 21.2 Security Properties

| Property | Mechanism |
|----------|-----------|
| Network authentication | Bearer token (HTTP layer), constant-time comparison |
| Per-caller action authorization | `caller_policies` in node.yaml |
| Credential confidentiality | AES-256-GCM encryption at rest, never logged, never in response bodies |
| Credential isolation between sessions | Per-session key derivation (session_id in key material) |
| Routing loop prevention | `max_hop` + `route_path` cycle detection |
| Token revocation | Remove from `allowed_tokens`, restart node |
| Bootstrap security | `trusted_nodes` whitelist — only listed nodes may register |

### 21.3 Known Risks

| Risk | Severity | Mitigation |
|------|----------|-----------|
| `execute_command` is unrestricted | High | Run each node as non-root user with minimal filesystem permissions |
| Shared auth token blast radius (v5.12 and earlier) | Medium | Use `allowed_tokens` per-node model (v5.13b+) |
| In-memory credentials lost on restart | Low | Use `credential_store_path` for persistence |
| Timing side-channel with `allowed_tokens` list | Very Low | Token count is small; network jitter dominates |
| `auth_token` rotation invalidates credentials | Low | Set `credential_encryption_key` separately |

### 21.4 Production Hardening Checklist

```
[ ] Run each node as a dedicated non-root OS user
[ ] Set process resource limits (ulimit -n, -m)
[ ] Use per-node tokens (allowed_tokens, not shared auth_token)
[ ] Set credential_encryption_key separately from auth_token
[ ] Enable credential_store_path for persistence
[ ] Use cloudflared or equivalent tunnel for public endpoints (no direct port forwarding)
[ ] Add tokens.env to .gitignore; never commit secrets
[ ] Rotate credential_encryption_key annually or on compromise
[ ] Enable action-level caller_policies for multi-team deployments
```

---

## 22. Deployment Topology

### 22.1 Reference Cluster: 4-Node Production Setup

```
deb-0 (Debian)     Public, exposed via Cloudflare Tunnel → deb-0.vietml.com
  ├── cen-0    (CentOS)     Private, pull-only
  ├── alm-0    (AlmaLinux)  Private, pull-only
  └── cfsnk-0  (CentOS/RHEL) Private, pull-only
```

**Bootstrap sequence:**

```bash
# 1. Generate tokens (run once locally)
bash 00_gen_tokens.sh        # creates tokens.env
source tokens.env
export ANTHROPIC_API_KEY="sk-ant-..."

# 2. Setup gateway on Debian machine
bash 01_setup_deb0.sh        # installs runtime, creates systemd service
bash 02_setup_cloudflared.sh  # creates tunnel, configures DNS

# 3. Setup each worker (run on each worker machine)
curl -sSL https://deb-0.vietml.com/setup.sh | bash -s -- \
    --node-id cen-0 --auth-token "$TOKEN_CEN_0" --systemd
bash 03_register_worker.sh --node-id cen-0    # patches node.yaml, restarts service

# 4. Verify cluster
bash 04_test_cluster.sh      # health, auth rejection, node status, action execution
```

### 22.2 Token Architecture (Per-Node Model)

```
deb-0/node.yaml:
  allowed_tokens:
    - <TOKEN_CEN_0>        # only cen-0 knows this
    - <TOKEN_ALM_0>        # only alm-0 knows this
    - <TOKEN_CFSNK_0>      # only cfsnk-0 knows this
    - <TOKEN_EXTERNAL>     # for external callers (Claude Web, Telegram, curl)

cen-0/node.yaml:
  auth_token: <TOKEN_CEN_0>
  gateway_auth_token: <TOKEN_CEN_0>
  gateway_node_id: deb-0
  gateway_address: https://deb-0.vietml.com
```

### 22.3 Cloudflare Tunnel Integration

```
Internet → Cloudflare → cloudflared daemon → localhost:8080 (deb-0)
```

No port forwarding, no firewall changes, no public IP required for the gateway
machine. Workers make outbound HTTP connections to `https://deb-0.vietml.com` only.

---

## 23. Evolution History and Design Decisions

### 23.1 Version Progression

| Version | Major Feature |
|---------|--------------|
| **v5.0** | Seed node with 3 primitives, plugin action model, async job model, mesh routing, skill discovery |
| **v5.1** | Bearer token auth, JSON Schema validation, job TTL cleanup, `POST /bootstrap`, auto-rollback |
| **v5.2** | One-liner setup.sh, distribution endpoints (`GET /setup.sh`, `GET /runtime-bundle`), shared LLM client injection |
| **v5.3** | Push/Pull delivery model, NodeRegistry, JobQueue, WorkerAgent, 6 new worker endpoints |
| **v5.4** | Python-based Cloud Planner (ReAct loop in code) |
| **v5.5** | Deprecated Python planner in favor of Claude Web as native orchestrator; `mesh_ctl.py` CLI |
| **v5.6** | Optional `task_id` and `trace` in request envelope (auto-generated); curl-native Claude Web workflow |
| **v5.7** | Lazy node staleness detection, pull job timeout, FastAPI lifespan migration, queue depth in `/health` |
| **v5.8** | Binary file transfer: `POST /upload`, `GET /download/{id}`, `UploadManager` with lazy TTL |
| **v5.9** | `POST /intent` ReAct agent loop, `ConversationStore`, `LLMClient` tool calling, session endpoints |
| **v5.10** | BGP-style route advertisement, multi-hop NAT routing, `GET /capabilities`, WorkerAgent re-advertisement |
| **v5.11** | Action schema exposure in capabilities, caller authorization (`caller_policies`), caller credential delivery |
| **v5.12** | Route withdrawal (BGP cascade), result forwarding chain (poll-and-relay), session credential storage, AES-256-GCM encryption |
| **v5.13** | Separate `credential_encryption_key` (decouple from `auth_token`), credential persistence (atomic JSON), action specs cascade through multi-hop |
| **v5.13b** | Per-node tokens (`allowed_tokens`), `gateway_auth_token`, `AuthMiddleware` bug fix (was imported but not wired), cluster deployment model |

### 23.2 Key Design Decisions

**D1: Why lazy evaluation over background polling?**

Node staleness, pull job timeout, and upload TTL are all checked at query time rather
than by background processes. This eliminates background task coordination overhead,
makes behavior deterministic and easy to test, and ensures that routing decisions
are always made with current information at the moment they matter.

**D2: Why BGP analogy for routing?**

The Internet has solved mesh routing at massive scale. BGP's next-hop abstraction
maps cleanly to GNOT's problem: each node only needs to know the next hop for
a target, not the full path. The key insight is that intermediate nodes' full path
information is irrelevant to top-level callers — they just need to reach the target.

**D3: Why a single `mesh_action` tool rather than per-action tools?**

A single tool keeps the tool registration stable as the mesh evolves. New nodes and
actions join the mesh without requiring changes to the tool schema. The LLM learns
what to call from the capability tree in the system prompt, not from the tool
definition. This decoupling is intentional.

**D4: Why AES-256-GCM for credential storage rather than a KMS?**

KMS integration adds deployment complexity (IAM roles, network policies) incompatible
with the GNOT philosophy of minimal operational footprint. AES-256-GCM with per-session
key derivation provides strong isolation between sessions while requiring only a single
secret stored in node.yaml.

**D5: Why separate `credential_encryption_key` from `auth_token`?**

Security hygiene requires rotating `auth_token` periodically. Without separation,
rotation invalidates all active sessions — a non-obvious side effect. The two keys
serve different purposes: `auth_token` for network identity verification,
`credential_encryption_key` for data confidentiality. Separating them allows each
to be rotated on its own schedule.

**D6: Why per-node tokens rather than a shared cluster token?**

A shared token means revoking a compromised worker requires a cluster-wide rotation.
Per-node tokens allow single-node revocation. The gateway maintains a simple list
of accepted tokens; overhead is minimal (a frozenset lookup per request).

---

## 24. Known Limitations and Future Work

### 24.1 Current Limitations

Three limitations of architectural significance are acknowledged:

| Item | Description | Mitigation |
|------|-------------|-----------|
| **L1 — No container isolation** | `execute_command` has full host filesystem access | Run each node as a dedicated non-root OS user with restricted permissions. Container-level isolation via Docker or nsjail would require careful integration to avoid breaking the seed bootstrapping sequence. |
| **L2 — In-memory pull queue** | JobQueue is held in process memory; gateway restart loses all undelivered queued jobs | `pull_job_timeout_seconds` bounds the exposure window for any individual job. A SQLite-backed durable queue is planned (P1). |
| **L3 — Hot-reload configuration** | Adding or revoking tokens in `node.yaml` requires a node restart to take effect | Restart is fast at current scale. A SIGHUP handler or inotify watch is planned (P2). |

### 24.2 Future Work

| Priority | Feature | Description |
|----------|---------|-------------|
| P1 | SSE streaming | `POST /intent` returns `text/event-stream` with turn-by-turn updates |
| P1 | Durable job queue | SQLite-backed JobQueue surviving gateway restarts |
| P2 | Hot-reload config | SIGHUP or inotify watch on `node.yaml` for token changes without restart |
| P2 | Token audit logging | Map token fingerprint → node identity in access logs |
| P3 | Container isolation | Optionally run actions in Docker or nsjail per node |
| P3 | Metrics endpoint | Prometheus `/metrics`: job throughput, queue depths, latency |
| P3 | Topology dashboard | Read-only web UI for mesh topology and job history |

**GNOT v2 roadmap:** Multi-network membership (one node joining multiple gateways),
event emit/subscribe between nodes, and a built-in scheduler for periodic action
invocation are the primary architectural extensions planned for v2.

---

## Appendix A: Capability Tree Example (4-node cluster)

```json
{
  "node_id": "deb-0",
  "actions": ["execute_command", "read_file", "write_file"],
  "reachable": {
    "cen-0": {
      "node_id": "cen-0",
      "actions": ["execute_command", "read_file", "write_file"],
      "status": "online",
      "next_hop": null,
      "action_specs": {
        "execute_command": {
          "description": "Execute a shell command and return stdout, stderr, exit_code.",
          "params_schema": {
            "command": { "type": "string", "description": "Shell command to execute" },
            "timeout_seconds": { "type": "integer", "default": 60, "max": 3600 }
          },
          "caller_credentials": {},
          "async_action": true
        }
      }
    },
    "alm-0": {
      "node_id": "alm-0",
      "actions": ["execute_command", "read_file", "write_file"],
      "status": "online",
      "next_hop": null
    },
    "cfsnk-0": {
      "node_id": "cfsnk-0",
      "actions": ["execute_command", "read_file", "write_file"],
      "status": "online",
      "next_hop": null
    }
  }
}
```

---

## Appendix B: Example Intent Prompts

### B.1 Infrastructure Query

```
POST /intent
{ "prompt": "Xem dung lượng đĩa cứng trên máy CFSNK", "session_id": "s1" }
```

Agent calls: `execute_command("df -h")` on cfsnk-0 → returns formatted disk usage.

### B.2 Cross-Node File Operation

```
POST /intent
{
  "prompt": "Backup các file cấu hình Nginx từ máy CFSNK sang máy CENTOS vào folder /space3/backup/cfsnk",
  "session_id": "s2"
}
```

Agent orchestrates:
1. `execute_command("tar czf /tmp/nginx-config.tgz /etc/nginx && base64 /tmp/nginx-config.tgz")` on cfsnk-0
2. `write_file("/space3/backup/cfsnk/nginx-config.tgz.b64", content)` on cen-0
3. `execute_command("base64 -d /space3/backup/cfsnk/nginx-config.tgz.b64 > /space3/backup/cfsnk/nginx-config.tgz && tar xzf ...")` on cen-0

### B.3 Enterprise Workflow with Credentials

```
POST /intent
{
  "prompt": "Tôi muốn xem thông tin đơn hàng #ORD-4521",
  "session_id": "telegram-12345",
  "caller_credentials": { "crm_user_token": "sk-user-abc123" }
}
```

Agent calls: `get_order_info({ "order_id": "ORD-4521" })` on cfsnk-0a (via cfsnk-0),
passing `crm_user_token` through the pull chain to the action. CRM validates the token.

---

## Appendix C: Minimum Viable Node — Test Deployment

```yaml
# minimal-node.yaml
node_id: test-node
listen: 0.0.0.0:8080
auth_token: test-secret
```

```bash
# Start
python node_runtime.py --config minimal-node.yaml

# Test
curl -s http://localhost:8080/health
curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer test-secret" \
  -H "Content-Type: application/json" \
  -d '{"target_node_id":"test-node","payload":{"action":"execute_command","params":{"command":"hostname"}}}'
```

---

*Document: GNOT Architecture Specification v1.1*  
*Author: Viet Tran*     
*Organization: gnot-io*  
*Website: gnot.io*  
*Repository: github.com/gnot-io/gnot*  
*Runtime version: v5.13b*  
*Last updated: 2026-03-06*
