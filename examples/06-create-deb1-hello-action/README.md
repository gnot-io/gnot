# Guide 06 — Create deb-1 with a Custom "hello" Action

**Difficulty:** Intermediate  
**Prerequisite:** [Guide 05 — Send a Prompt via /intent](../05-curl-intent/README.md)  
**Goal:** Bootstrap a second GNOT node (deb-1) on the same Debian machine, write a custom `hello` action plugin, register deb-1 as a worker node under deb-0, and call the new action from Claude Web.

---

## What You Will Build

```
deb-0  (gateway, port 8080)
  └── deb-1  (worker, port 8081)
               └── action: hello
```

deb-1 is a worker node: it polls deb-0 for jobs, executes them locally, and reports results back. The `hello` action is the simplest possible custom action — a foundation for building real plugins.

---

## Part 1 — Write the Custom Action

A GNOT action is two files: a `.py` runner and a `.schema.json` descriptor.

### Create the action directory

```bash
mkdir -p ~/gnot-nodes/deb-1/actions
```

### `hello.py` — the action logic

```python
# ~/gnot-nodes/deb-1/actions/hello.py

def run(params: dict, context: dict) -> dict:
    """Return a greeting with optional personalization."""
    name = params.get("name", "World")
    language = params.get("language", "en")

    greetings = {
        "en": f"Hello, {name}!",
        "es": f"¡Hola, {name}!",
        "fr": f"Bonjour, {name}!",
        "vi": f"Xin chào, {name}!",
        "ja": f"こんにちは、{name}！",
    }

    message = greetings.get(language, greetings["en"])

    return {
        "message": message,
        "node": context.get("node_id", "unknown"),
        "language": language,
    }
```

Save this file as `~/gnot-nodes/deb-1/actions/hello.py`.

> **Key points about action structure:**
> - The function must be named `run`
> - It receives `params` (dict from the caller) and `context` (runtime info injected by GNOT)
> - It must return a JSON-serializable dict
> - No `ASYNC = True` flag → synchronous action (returns result immediately)

### `hello.schema.json` — the action descriptor

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "hello",
  "description": "Return a greeting message. Supports multiple languages.",
  "type": "object",
  "properties": {
    "name": {
      "type": "string",
      "description": "Name to greet",
      "default": "World"
    },
    "language": {
      "type": "string",
      "description": "Language code: en, es, fr, vi, ja",
      "enum": ["en", "es", "fr", "vi", "ja"],
      "default": "en"
    }
  },
  "additionalProperties": false
}
```

Save this as `~/gnot-nodes/deb-1/actions/hello.schema.json`.

> **Why the schema matters:** GNOT serves the schema through `GET /capabilities`. The LLM reads it to understand what parameters the action accepts, their types, and what they mean — without any pre-written tool registration code.

---

## Part 2 — Configure deb-1 as a Worker Node

Create `~/gnot-nodes/deb-1/node.yaml`:

```yaml
# ~/gnot-nodes/deb-1/node.yaml

# ── Identity ──────────────────────────────────────────────
node_id: deb-1
listen:  0.0.0.0:8081          # different port — same machine as deb-0

# ── Actions ───────────────────────────────────────────────
# Use our custom actions directory (plus seed actions alongside)
actions_dir: /home/YOUR_USER/gnot-nodes/deb-1/actions

# ── Authentication ─────────────────────────────────────────
# deb-1's own token (for inbound calls to deb-1 directly)
auth_token: deb-1-secret-token

# Token deb-1 uses when talking to deb-0 (must match deb-0's auth_token or allowed_tokens)
gateway_auth_token: change-this-to-a-strong-secret

# ── Worker mode ───────────────────────────────────────────
gateway_node_id: deb-0
gateway_address: http://127.0.0.1:8080   # deb-0 is on same machine

# Optional: if deb-0 can reach deb-1 directly (push mode)
# Since they're on the same machine, we can enable this:
self_address: http://127.0.0.1:8081

heartbeat_interval_seconds: 15
poll_interval_seconds: 5

# ── Job management ─────────────────────────────────────────
job_ttl_seconds: 3600
cleanup_interval_seconds: 60
```

Replace `YOUR_USER` with your actual username (output of `whoami`).

---

## Part 3 — Update deb-0 to Trust deb-1

Edit `~/gnot-nodes/deb-0/node.yaml` and add deb-1 to `trusted_nodes`:

```yaml
# Add this section (or update if trusted_nodes already exists):
trusted_nodes:
  - deb-1
```

Also add deb-1 to `allowed_tokens` so its registration requests are accepted:

```yaml
allowed_tokens:
  - change-this-to-a-strong-secret   # deb-0's own callers
  - deb-1-secret-token               # deb-1 itself
```

Restart deb-0:

```bash
kill $(cat ~/gnot-nodes/deb-0/node.pid)
cd /path/to/gnot
nohup python3 mesh/node_runtime.py --config ~/gnot-nodes/deb-0/node.yaml \
    > ~/gnot-nodes/deb-0/node.log 2>&1 &
echo $! > ~/gnot-nodes/deb-0/node.pid
```

---

## Part 4 — Start deb-1

deb-1 also needs the seed actions (execute_command, read_file, write_file). Copy them alongside the custom action:

```bash
cp /path/to/gnot/mesh/seed/actions/*.py   ~/gnot-nodes/deb-1/actions/
cp /path/to/gnot/mesh/seed/actions/*.json ~/gnot-nodes/deb-1/actions/
```

Start deb-1:

```bash
cd /path/to/gnot
nohup python3 mesh/node_runtime.py --config ~/gnot-nodes/deb-1/node.yaml \
    > ~/gnot-nodes/deb-1/node.log 2>&1 &
echo $! > ~/gnot-nodes/deb-1/node.pid
```

Watch the log — you should see deb-1 register with deb-0:

```bash
tail -f ~/gnot-nodes/deb-1/node.log
```

Look for:

```
INFO  WorkerAgent: registering with gateway deb-0 at http://127.0.0.1:8080
INFO  WorkerAgent: registration successful
INFO  WorkerAgent: heartbeat loop started (interval=15s)
INFO  WorkerAgent: poll loop started (interval=5s)
```

---

## Part 5 — Verify the Mesh

### Check deb-0 now sees deb-1

```bash
curl -s http://localhost:8080/capabilities \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  | python3 -m json.tool
```

Expected: deb-1 appears under `reachable`, with the `hello` action listed:

```json
{
  "node_id": "deb-0",
  "actions": ["execute_command", "read_file", ...],
  "reachable": {
    "deb-1": {
      "node_id": "deb-1",
      "actions": ["execute_command", "read_file", "write_file", "hello", ...],
      "status": "online"
    }
  }
}
```

### Call `hello` via curl

```bash
curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "deb-1",
    "payload": {
      "action": "hello",
      "params": { "name": "GNOT", "language": "en" }
    }
  }' | python3 -m json.tool
```

Expected:

```json
{
    "message": "Hello, GNOT!",
    "node": "deb-1",
    "language": "en"
}
```

Try other languages:

```bash
# Vietnamese
curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  -H "Content-Type: application/json" \
  -d '{"target_node_id":"deb-1","payload":{"action":"hello","params":{"name":"World","language":"vi"}}}' \
  | python3 -m json.tool
```

---

## Part 6 — Call from Claude Web

Update your Claude project instructions with the new capabilities (paste fresh `GET /capabilities` output). Then try:

```
Say hello to me in French on deb-1, using my name "Alice".
```

Claude will call `hello` on deb-1 with `{"name": "Alice", "language": "fr"}` and report the result.

You can also ask Claude to discover the mesh dynamically:

```
What nodes are in the mesh, and what custom actions does each node have (beyond the seed actions)?
```

Claude will call `GET /capabilities`, parse the response, and summarize.

---

## How Action Loading Works

When GNOT starts a node:
1. It scans `actions_dir` for `*.py` files
2. For each `.py` file, it looks for a matching `.schema.json`
3. It imports the module and registers the `run` function
4. The action becomes available immediately via `POST /action`
5. On registration with the gateway, the full schema is advertised via `GET /capabilities`

**To add a new action:** Drop two files in `actions_dir` and restart the node (or use `POST /bootstrap` in later guides for hot-loading).

---

## Summary

You now have:
- ✅ A two-node mesh: deb-0 (gateway) + deb-1 (worker)
- ✅ A custom `hello` action on deb-1
- ✅ deb-1 registered and visible in `GET /capabilities`
- ✅ Action callable from curl and Claude Web

**Next:** [Guide 07 — Long-Running Actions and Async Polling](../07-async-polling/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
