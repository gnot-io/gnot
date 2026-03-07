# Guide 01 — Set Up Your First GNOT Node (deb-0)

**Difficulty:** Beginner  
**Time:** ~10 minutes  
**Goal:** Install GNOT on a Debian/Ubuntu machine, start a standalone seed node, and verify it is running correctly.

---

## What You Will Build

A single GNOT node — called **deb-0** — running on your local Debian machine. This node exposes an HTTP API that you (or an LLM) can call to execute commands, read files, and write files on the machine.

```
Your terminal
     │
     │  curl http://localhost:8080/health
     ▼
┌─────────────────────────┐
│  deb-0  (Debian local)  │
│  localhost:8080         │
│                         │
│  actions:               │
│    execute_command      │
│    read_file            │
│    write_file           │
└─────────────────────────┘
```

In later guides, this node will become the **gateway** for a multi-machine mesh.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Debian 11+ / Ubuntu 22.04+ | This guide uses Debian. See the note below for other distros. |
| Python 3.11 or newer | Check with `python3 --version` |
| `git` | Install with `sudo apt install git` |
| Internet access | To clone the repo and install packages |

> **Other Linux distros:** GNOT runs on any Linux. For CentOS/AlmaLinux, replace `apt` commands with `dnf`. The Python and GNOT steps are identical.

---

## Step 1 — Clone the Repository

```bash
git clone https://github.com/gnot-io/gnot.git
cd gnot
```

---

## Step 2 — Install Python Dependencies

```bash
cd gnot/src
pip install -r requirements.txt
```

> **Tip:** If you are on a system-managed Python (Debian 12+, Ubuntu 23+), use a virtual environment to avoid conflicts:
> ```bash
> python3 -m venv .venv
> source .venv/bin/activate
> pip install -r gnot/src/requirements.txt
> ```

Verify the install:

```bash
python3 -c "import fastapi, uvicorn, httpx, yaml; print('OK')"
# Expected output: OK
```

---

## Step 3 — Create the Node Configuration

Create a directory for your node and write its configuration file:

```bash
mkdir -p ~/gnot-nodes/deb-0
```

Create `~/gnot-nodes/deb-0/node.yaml` with the following content:

```yaml
# ~/gnot-nodes/deb-0/node.yaml

# ── Identity ──────────────────────────────────────────────
node_id: deb-0
listen:  0.0.0.0:8080

# ── Actions ───────────────────────────────────────────────
# Path to action plugins (relative to this file's directory,
# or absolute). Leave as default to use the built-in seed actions.
actions_dir: /path/to/gnot/gnot/src/seed/actions

# ── Authentication ─────────────────────────────────────────
# Anyone calling this node must include this token in the
# Authorization header: "Authorization: Bearer <auth_token>"
auth_token: change-this-to-a-strong-secret

# ── Gateway mode ──────────────────────────────────────────
# List worker node IDs that are allowed to register here.
# Leave empty for now — we will add workers in a later guide.
trusted_nodes: []

# ── Job management ─────────────────────────────────────────
job_ttl_seconds: 3600
cleanup_interval_seconds: 60
```

> **Replace `/path/to/gnot`** with the actual path where you cloned the repo.  
> For example, if you cloned to `/home/alice/gnot`, set:
> ```yaml
> actions_dir: /home/alice/gnot/gnot/src/seed/actions
> ```

> **Choose a strong `auth_token`.** This is the only secret protecting your node.
> Generate one with: `python3 -c "import secrets; print(secrets.token_hex(24))"`

---

## Step 4 — Start the Node

```bash
cd /path/to/gnot
python3 gnot/src/node_runtime.py --config ~/gnot-nodes/deb-0/node.yaml
```

You should see output similar to:

```
INFO     node_id=deb-0 host=0.0.0.0 port=8080
INFO     Actions loaded: execute_command, read_file, read_file_b64, write_file, write_file_b64
INFO     Starting GNOT node deb-0 on 0.0.0.0:8080
INFO     Uvicorn running on http://0.0.0.0:8080
```

> **Run in the background (optional):**
> ```bash
> nohup python3 gnot/src/node_runtime.py --config ~/gnot-nodes/deb-0/node.yaml \
>     > ~/gnot-nodes/deb-0/node.log 2>&1 &
> echo $! > ~/gnot-nodes/deb-0/node.pid
> ```
>
> To stop it later: `kill $(cat ~/gnot-nodes/deb-0/node.pid)`

---

## Step 5 — Verify the Node is Running

Open a new terminal (or use the same terminal if you ran the node in the background).

### 5.1 Health check (no authentication required)

```bash
curl -s http://localhost:8080/health | python3 -m json.tool
```

Expected response:

```json
{
    "status": "ok",
    "node_id": "deb-0",
    "jobs_active": 0,
    "queue_depth": {}
}
```

### 5.2 Capabilities check (authentication required)

```bash
curl -s http://localhost:8080/capabilities \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  | python3 -m json.tool
```

Expected response (trimmed):

```json
{
    "node_id": "deb-0",
    "actions": [
        "execute_command",
        "read_file",
        "read_file_b64",
        "write_file",
        "write_file_b64"
    ],
    "status": "online",
    "reachable": {}
}
```

The `reachable` field is empty because no worker nodes have connected yet — that is expected for a standalone node.

---

## Understanding the Configuration

Here is what each section of `node.yaml` does:

| Field | Purpose |
|-------|---------|
| `node_id` | Unique name for this node in the mesh. Must be unique across all nodes. |
| `listen` | IP and port the HTTP server binds to. `0.0.0.0` means all interfaces. |
| `actions_dir` | Directory containing action plugins (`.py` + `.schema.json` pairs). |
| `auth_token` | Bearer token for all authenticated endpoints. Keep this secret. |
| `trusted_nodes` | Worker node IDs this gateway accepts. Empty = standalone mode. |

---

## Understanding the Seed Actions

Your node starts with five built-in actions:

| Action | What it does |
|--------|-------------|
| `execute_command` | Run any shell command and return stdout/stderr |
| `read_file` | Read a file as UTF-8 text |
| `read_file_b64` | Read a file as base64 (for binary files) |
| `write_file` | Write text content to a file |
| `write_file_b64` | Write base64-encoded content to a file (for binary files) |

These three primitives — execute, read, write — are sufficient to bootstrap any Linux environment. In later guides, you will use them to provision new nodes, install software, and add custom actions.

---

## Troubleshooting

**Port 8080 is already in use:**
```bash
# Find what is using the port
sudo ss -tlnp | grep 8080
# Use a different port in node.yaml:
listen: 0.0.0.0:8888
```

**`ModuleNotFoundError: No module named 'fastapi'`:**
```bash
# You may be in a different Python environment
which python3
# Re-run pip install in the correct environment
pip3 install -r gnot/src/requirements.txt
```

**`Permission denied` on the actions directory:**
```bash
chmod +x gnot/src/seed/actions/*.py
```

**401 Unauthorized on `/capabilities`:**  
Double-check the token in your curl command matches `auth_token` in `node.yaml` exactly, including case.

---

## Summary

You now have a running GNOT seed node with:

- ✅ HTTP API on `localhost:8080`
- ✅ Five seed actions loaded
- ✅ Bearer token authentication
- ✅ Health and capabilities endpoints responding

**Next:** [Guide 02 — Call an Action via curl](../02-curl-action/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
