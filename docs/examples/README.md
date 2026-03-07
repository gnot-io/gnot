# GNOT Examples

Step-by-step guides for setting up and using a GNOT execution mesh — from a single node to a full multi-machine topology.

Each guide builds on the previous one. If you are new to GNOT, start with **Guide 01**.

---

## Learning Path

### Level 1 — Single Node Basics

| Guide | Title | What You Learn |
|-------|-------|----------------|
| [01](./01-setup-deb0/README.md) | **Set Up Your First GNOT Node (deb-0)** | Install GNOT, configure a standalone seed node, verify health and capabilities |
| [02](./02-curl-action/README.md) | **Call an Action via curl** | Execute shell commands on deb-0 via `POST /action` |
| [03](./03-claude-web-action/README.md) | **Call an Action from Claude Web** | Use Claude as the LLM orchestrator; expose deb-0 via Cloudflare Tunnel |
| [04](./04-configure-llm/README.md) | **Configure a Node-Local LLM** | Add Claude API to deb-0 config, activate `POST /intent` |
| [05](./05-curl-intent/README.md) | **Send a Prompt via /intent** | Send natural-language prompts to deb-0; LLM selects and executes actions |

### Level 2 — Second Node and Custom Actions

| Guide | Title | What You Learn |
|-------|-------|----------------|
| [06](./06-create-deb1-hello-action/README.md) | **Create deb-1 with a Custom "hello" Action** | Bootstrap a second node, write a custom action plugin, test from Claude Web |
| [07](./07-async-polling/README.md) | **Long-Running Actions and Async Polling** | Write an async action, poll `GET /result/{job_id}` from Claude Web |
| [08](./08-advanced-actions/README.md) | **Advanced Custom Actions** | Parameter validation, error handling, calling external APIs from an action |

### Level 3 — Multi-Node Orchestration

| Guide | Title | What You Learn |
|-------|-------|----------------|
| [09](./09-multi-node-execution/README.md) | **Execute Across Multiple Nodes** | Run the same action on deb-0 and deb-1 from a single prompt |
| [10](./10-sub-node-registration/README.md) | **Sub-Node Registration** | Register deb-1 as a sub-node of deb-0; test BGP-style route propagation |
| [11](./11-nat-traversal/README.md) | **NAT Traversal** | deb-1 sits behind a different NAT gateway; test pull-mode delivery |
| [12](./12-cross-node-communication/README.md) | **Cross-Node Communication** | Transfer files between nodes, build a simple data pipeline |

### Level 4 — Scale and Topology

| Guide | Title | What You Learn |
|-------|-------|----------------|
| [13](./13-auto-scale/README.md) | **Auto-Scale Additional Nodes** | Use `POST /bootstrap` to programmatically provision new nodes |
| [14](./14-full-mesh/README.md) | **Full Mesh Topology (5+ Nodes)** | Operate deb-0, cen-0, alm-0, and two more nodes as a complete mesh |
| [15](./15-security-hardening/README.md) | **Security Hardening** | `caller_policies`, credential rotation, running nodes as non-root users |

### Level 5 — Real-World Use Cases

| Guide | Title | What You Learn |
|-------|-------|----------------|
| [16](./16-content-automation/README.md) | **Content Automation Pipeline** | Topic → research → article → images/video → publish; fully LLM-orchestrated |
| [17](./17-telegram-crm/README.md) | **Telegram Bot + CRM Integration** | Telegram messages → `/intent` → private CRM node (no public address needed) |
| [18](./18-autonomous-dev/README.md) | **Autonomous Development Workflow** | LLM reads code, writes fixes, runs tests, loops until all tests pass |

---

## Environment Used in These Guides

| Node | Machine | Role |
|------|---------|------|
| **deb-0** | Debian (local) | Gateway node — public-facing via Cloudflare Tunnel |
| **cen-0** | CentOS (separate network) | Worker node — behind NAT |
| **alm-0** | AlmaLinux (separate network) | Worker node — behind NAT |

> All guides can be adapted to any Linux distribution. Where OS-specific commands differ, both variants are shown.

---

## Prerequisites (All Guides)

- Python 3.11+
- Git
- `curl`
- [Cloudflare Tunnel (`cloudflared`)](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) — required from Guide 03 onward

---

*GNOT — Generative Node Orchestration Technology*  
*[GitHub](https://github.com/gnot-io/gnot) · [Documentation](../docs/GNOT_SPECS_V1.1.md)*
