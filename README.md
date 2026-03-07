# GNOT — Generative Node Orchestration Technology
x
> **A distributed execution mesh that any LLM can orchestrate natively —
> no SDK, no workflow code, no pre-provisioning.**

> **Official repository** — maintained by [gnot-io](https://github.com/gnot-io) · original author: [Tran Quoc Viet](https://github.com/gnot-io)
> Forks and derivatives are welcome under Apache 2.0. Please retain the [NOTICE](NOTICE) file and link back to this repository.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Runtime](https://img.shields.io/badge/runtime-v5.13b-green.svg)](https://github.com/gnot-io/gnot/releases)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.18886844.svg)](https://doi.org/10.5281/zenodo.18886844)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![GitHub contributors](https://img.shields.io/github/contributors/gnot-io/gnot.svg)](https://github.com/gnot-io/gnot/graphs/contributors)
[![Official](https://img.shields.io/badge/official-gnot--io-orange.svg)](https://github.com/gnot-io/gnot)

---

## What is GNOT?

GNOT is a minimal runtime that turns any Linux machine into a node in an **LLM-orchestrated execution mesh**. You give an LLM access to a single HTTP tool, and it can discover, provision, and operate an entire fleet of machines — across NAT boundaries, without pre-written workflow code.

The system boots from a **seed node** carrying exactly three capabilities:

| Primitive | What it enables |
|-----------|----------------|
| `read_file` | Observe the machine's state — configs, logs, source code |
| `write_file` | Modify persistent state — write configs, install scripts |
| `execute_command` | Activate software — run installers, start services, call APIs |

These three primitives form a **minimal complete basis**: any Linux environment can be bootstrapped from them, and nothing more is needed at seed time.

---

## How it works

An external LLM (Claude, GPT-4, any tool-calling model) is given:
1. The mesh capability description from `GET /capabilities`
2. One generic tool: `mesh_action(target_node_id, action, params)`

From this, the LLM can execute commands across any node in the mesh, transfer files between private networks, provision new nodes, install software, and compose arbitrarily complex multi-step workflows — all from a single natural-language prompt.

```
You (natural language)
        │
        ▼
External LLM (Claude Web, Claude API, GPT-4, local model...)
        │
        │  POST /action  →  result
        ▼
┌─────────────────────────────────────────────────────┐
│                   GNOT Mesh                          │
│                                                      │
│  Gateway Node (public)                               │
│       │                                              │
│       ├── Worker Node A  (behind NAT, polls gateway) │
│       ├── Worker Node B  (behind NAT, polls gateway) │
│       └── Worker Node C  (behind NAT, with sub-node) │
└─────────────────────────────────────────────────────┘
```

Workers never receive inbound connections. They poll the gateway outbound over HTTPS, making NAT traversal transparent to the LLM caller.

---

## Key features

- **NAT-transparent routing** — workers behind any NAT depth connect outbound; no port forwarding required
- **BGP-inspired route advertisement** — multi-hop delivery without topology prior knowledge
- **Three seed primitives** — sufficient to bootstrap any Linux environment and provision new nodes
- **Single `mesh_action` tool** — the LLM's interface to the entire mesh never changes as nodes join or leave
- **External LLM as primary orchestrator** — no LLM configuration required on any node
- **Optional node-local LLM** — activate `POST /intent` on any node for embedded/offline use
- **Binary file transfer across NAT** — staged upload/download between any two nodes
- **Per-session AES-256-GCM credential system** — caller credentials scoped and encrypted per session
- **Plugin action system** — drop a `.py` file in `actions/` to add new capabilities to any node
- **Lazy staleness detection** — no background polling; liveness evaluated at the moment it matters

---

## Production use cases

GNOT has been deployed on a four-node production cluster spanning two NAT layers. The following use cases were validated — each initiated by a **single natural-language prompt with zero pre-written workflow code**:

### Cross-network database migration
Two worker nodes on separate private networks with no direct connectivity. The LLM orchestrates a full MySQL migration — dump, base64-encode, transfer via gateway, decode, restore — entirely through three seed actions.

### Telegram-based CRM integration
A Telegram bot forwards customer messages as `POST /intent` to the gateway. The LLM resolves the intent, calls a private CRM node via pull-mode delivery, and returns a natural-language reply. The CRM node never needs a public address. Customer API tokens are stored encrypted in the per-session CredentialStore.

### Autonomous software development
A developer describes a feature or bug in natural language. The LLM autonomously reads source files, writes fixes, runs `pytest`, reads failures, revises, and re-tests — looping until all tests pass. No human intervention within the turn sequence.

### Automated YouTube content pipeline
Topic → research → script → AI-generated visuals → ffmpeg rendering → YouTube upload. The entire pipeline runs across mesh nodes from one prompt. No code written or executed by the user.

---

## Quickstart

### Requirements

- Python 3.11+
- Linux (tested on Debian, CentOS, Ubuntu)

### Installation

```bash
git clone https://github.com/gnot-io/gnot.git
cd gnot
pip install -r src/requirements.txt
```

### Start a standalone seed node

```yaml
# node.yaml
node_id: seed-0
listen: 0.0.0.0:8080
auth_token: your-secret-token
```

```bash
python src/node_runtime.py --config node.yaml
```

Verify:

```bash
curl -s http://localhost:8080/health

curl -s http://localhost:8080/capabilities \
  -H "Authorization: Bearer your-secret-token" | python -m json.tool
```

### Run your first action

```bash
curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "seed-0",
    "payload": {
      "action": "execute_command",
      "params": { "command": "hostname && uptime" }
    }
  }'
```

---

## Connecting Claude as orchestrator

### 1. Expose your gateway node

The gateway needs a public HTTPS address. The reference deployment uses [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) — no public IP or port forwarding required.

```bash
cloudflared tunnel --url http://localhost:8080
```

### 2. Get the capability description

```bash
curl -s https://your-gateway.example.com/capabilities \
  -H "Authorization: Bearer your-secret-token"
```

### 3. Configure Claude

In Claude's system prompt (or project instructions), paste:

```
You have access to a GNOT execution mesh via the mesh_action tool.
Mesh capabilities:
<paste GET /capabilities response here>

Use mesh_action to execute commands, read/write files, and orchestrate
workflows across the nodes listed above.
```

Register one tool:

```json
{
  "name": "mesh_action",
  "description": "Execute an action on a node in the GNOT mesh",
  "input_schema": {
    "type": "object",
    "properties": {
      "target_node_id": { "type": "string", "description": "Node ID from capabilities" },
      "action": { "type": "string", "description": "Action name, e.g. execute_command" },
      "params": { "type": "object", "description": "Action parameters" }
    },
    "required": ["target_node_id", "action", "params"]
  }
}
```

Claude can now orchestrate your entire mesh from natural language.

---

## Adding a worker node (behind NAT)

On the worker machine:

```yaml
# worker.yaml
node_id: worker-0
listen: 0.0.0.0:8081
auth_token: worker-secret

# Worker mode
gateway_node_id: seed-0
gateway_address: https://your-gateway.example.com
gateway_auth_token: your-secret-token

# Heartbeat
heartbeat_interval_seconds: 15
poll_interval_seconds: 5
```

```bash
python src/node_runtime.py --config worker.yaml
```

The worker registers itself with the gateway and begins polling. Within seconds it appears in `GET /capabilities` and becomes reachable through the mesh.

---

## Enabling node-local LLM (optional)

Add `llm_provider` config to activate `POST /intent` on any node:

```yaml
node_id: gateway-0
listen: 0.0.0.0:8080
auth_token: your-secret-token
trusted_nodes: [worker-0, worker-1]

# LLM (OpenAI-compatible endpoint)
llm_api_key: sk-...
llm_base_url: https://api.anthropic.com/v1
llm_default_model: claude-sonnet-4-20250514
llm_extra_headers:
  anthropic-version: "2023-06-01"
```

Now the node accepts free-text intents:

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Show disk usage on all worker nodes",
    "session_id": "my-session"
  }'
```

---

## Architecture overview

### Node modes

| Mode | Configuration | Role |
|------|--------------|------|
| **Standalone** | No `trusted_nodes`, no `gateway_node_id` | Executes actions locally only |
| **Gateway** | `trusted_nodes` set | Routes requests, hosts NodeRegistry and JobQueue |
| **Worker** | `gateway_node_id` + `gateway_address` set | Polls gateway, executes jobs, reports results |
| **Gateway+Worker** | Both | Intermediate node — routes to sub-nodes and is itself a worker |

### Routing

Every action request is routed through `GatewayRouter`:

```
POST /action  →  target local?
                    ├── yes → execute synchronously → return result
                    └── no  → node reachable directly?
                                  ├── yes → push job → wait for result
                                  └── no  → enqueue in pull queue → worker polls → result propagates back
```

Multi-hop routing follows a BGP-inspired next-hop model: each node only needs to know the next hop to reach a target, not the full path. Sub-nodes behind worker nodes are reachable through their parent transparently.

### Push / Pull delivery

| Mode | When | Description |
|------|------|-------------|
| **Synchronous** | Node is local | Direct execution, result in HTTP response |
| **Push** | Worker has `self_address` configured | Gateway POSTs job directly to worker |
| **Pull** | Worker is behind NAT | Gateway enqueues job; worker polls and reports result |

### Capability discovery

`GET /capabilities` returns the full capability tree — all reachable nodes, their actions, parameter schemas, and credential requirements — in a single response. This is the only document the LLM needs to operate the mesh.

---

## Writing a custom action

Drop two files into `src/seed/actions/` on any node:

```python
# src/seed/actions/get_system_info.py

def run(params: dict, context: dict) -> dict:
    import platform, psutil
    return {
        "hostname": platform.node(),
        "cpu_count": psutil.cpu_count(),
        "memory_gb": round(psutil.virtual_memory().total / 1e9, 1),
        "disk_free_gb": round(psutil.disk_usage("/").free / 1e9, 1),
    }
```

```json
// src/seed/actions/get_system_info.schema.json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "get_system_info",
  "description": "Return CPU, memory, and disk summary for this node.",
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

Restart the node. The action appears immediately in `GET /capabilities` and is available to any LLM orchestrating the mesh.

See [`docs/examples/`](docs/examples/) for 18 step-by-step walkthroughs.

For async actions, declare `ASYNC = True` and define `async def run(...)`.

---

## Security model

| Layer | Mechanism |
|-------|-----------|
| **Transport** | HTTPS (Cloudflare Tunnel or TLS termination) |
| **Authentication** | Bearer token per request (`Authorization: Bearer <token>`) |
| **Per-node isolation** | Each worker has its own token; revoking one node requires only removing its token from the gateway's `trusted_nodes` list |
| **Action authorization** | Optional `caller_policies` in `node.yaml` restricts which tokens may call which actions |
| **Credential storage** | AES-256-GCM with per-session key derivation; `credential_encryption_key` is separate from `auth_token` to allow independent rotation |

> ⚠️ `execute_command` has full host filesystem access by default. Run each node as a dedicated non-root OS user with restricted permissions. Container isolation via Docker/nsjail is planned for a future release.

---

## API reference (summary)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/action` | Execute action. Returns sync result or `{ job_id }` for async |
| `GET`  | `/result/{job_id}` | Poll async job status |
| `POST` | `/intent` | Free-text prompt → ReAct loop → natural-language reply *(requires llm_provider)* |
| `GET`  | `/capabilities` | Full capability tree for all reachable nodes |
| `GET`  | `/health` | Node health (unauthenticated) |
| `POST` | `/nodes/register` | Worker self-registration |
| `POST` | `/nodes/{id}/heartbeat` | Worker liveness signal |
| `GET`  | `/jobs/poll` | Worker polls for queued pull-mode jobs |
| `POST` | `/upload` | Upload binary file to gateway staged store |
| `GET`  | `/download/{file_id}` | Download staged file |
| `POST` | `/bootstrap` | Programmatic node creation with auto-rollback |

Full API reference: [`docs/specs/GNOT_SPECS_V1.1.md § 20`](docs/specs/GNOT_SPECS_V1.1.md)

---

## Repository structure

```
gnot/
├── docs/
│   ├── examples/                    # Step-by-step walkthrough examples (01–18)
│   ├── specs/                       # Full architecture specification
│   └── whitepapers/                 # Research papers (PDF + LaTeX source)
├── src/
│   ├── mesh_ctl.py                  # CLI management tool
│   ├── node_runtime.py              # Main entry point
│   ├── node-0/                      # Reference gateway node config
│   │   ├── node.yaml
│   │   └── skills.md
│   ├── runtime/                     # Core runtime modules
│   │   ├── action_executor.py       # Action dispatch and execution
│   │   ├── action_loader.py         # Plugin action loader
│   │   ├── auth.py                  # Bearer token authentication middleware
│   │   ├── bootstrap.py             # Programmatic node bootstrapping
│   │   ├── config.py                # NodeConfig loader
│   │   ├── conversation_store.py    # Per-session message history
│   │   ├── credential_store.py      # AES-256-GCM credential store
│   │   ├── gateway_router.py        # Top-level request router
│   │   ├── intent_handler.py        # ReAct agent loop (POST /intent)
│   │   ├── job_manager.py           # Async job lifecycle and cleanup
│   │   ├── job_queue.py             # Pull-mode job queue
│   │   ├── llm_client.py            # LLM provider client
│   │   ├── models.py                # Shared data models
│   │   ├── node_registry.py         # Node registration and liveness
│   │   ├── resolver.py              # Node address resolution
│   │   ├── router.py                # Intra-node routing
│   │   ├── schema_validator.py      # JSON Schema param validation
│   │   ├── server.py                # FastAPI app factory
│   │   ├── upload_manager.py        # Binary file staging
│   │   └── worker_agent.py          # WorkerAgent — register, heartbeat, poll
│   ├── seed/
│   │   └── actions/                 # Seed and built-in actions
│   │       ├── execute_command.py / .schema.json
│   │       ├── read_file.py / .schema.json
│   │       ├── read_file_b64.py / .schema.json
│   │       ├── write_file.py / .schema.json
│   │       ├── write_file_b64.py / .schema.json
│   │       ├── generate_image.py / .schema.json
│   │       ├── get_order_info.py / .schema.json
│   │       └── llm_chat.py / .schema.json
│   └── tests/
│       ├── test_actions.py
│       ├── test_auth.py
│       ├── test_bootstrap.py
│       ├── test_gateway_router.py
│       ├── test_integration.py
│       ├── test_job_manager.py
│       ├── test_node_registry.py
│       ├── test_router.py
│       ├── test_v52_features.py … test_v513_features.py
│       └── (+ 20 additional test modules)
├── CONTRIBUTING.md
└── README.md
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [`docs/specs/GNOT_SPECS_V1.1`](docs/specs/GNOT_SPECS_V1.1.md) | Full architecture specification v1.1 — node anatomy, routing, protocols, API reference, security model, deployment topology |
| [arXiv preprint](https://doi.org/10.5281/zenodo.18886844) | Research paper: *GNOT: Generative Node Orchestration Technology — A Minimal-Seed Architecture for LLM-Native Distributed Execution* |

---

## Contributing

Contributions are welcome! Whether you want to fix a bug, add a new action, improve documentation, or share a use case — please read [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

- **Branch model:** PRs should target `dev`, not `main`
- **Easiest start:** write a custom action (`.py` + `.schema.json`) under `src/seed/actions/`
- **Discussions:** [github.com/gnot-io/gnot/discussions](https://github.com/gnot-io/gnot/discussions)

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

---

## Citation

If you use GNOT in research or build on this architecture, please cite:

```bibtex
@misc{tran2026gnot,
  author    = {Tran, Quoc Viet},
  title     = {{GNOT}: Generative Node Orchestration Technology ---
               A Minimal-Seed Architecture for {LLM}-Native Distributed Execution},
  year      = {2026},
  doi       = {10.5281/zenodo.18886844},
  url       = {https://doi.org/10.5281/zenodo.18886844}
}
```

---

*gnot-io · gnot.io · github.com/gnot-io/gnot*