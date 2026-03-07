# Guide 13 — Auto-Scale Additional Nodes

**Difficulty:** Advanced  
**Prerequisite:** [Guide 12 — Cross-Node Communication](../12-cross-node-communication/README.md)  
**Goal:** Use `POST /bootstrap` to programmatically create a new GNOT node on deb-0 (or any machine) without manual file editing — all from an API call or a single LLM prompt.

---

## Two Ways to Provision Nodes

| Method | When to use |
|--------|------------|
| Manual config + start (Guides 01–06) | Learning, first-time setup, full control |
| `POST /bootstrap` | Automation, LLM-driven scale-out, CI/CD |

`POST /bootstrap` lets you create a new node — with custom actions, pip packages, and full configuration — in a single API call. It includes **automatic rollback**: if any step fails, all completed steps are undone.

---

## The Bootstrap API

```
POST /bootstrap
Authorization: Bearer <token>
Content-Type: application/json

{
  "node_id":      "new-node-id",
  "port":          8083,
  "auth_token":   "new-node-secret",
  "actions": [
    {
      "name":   "my_action",
      "code":   "<python source as string>",
      "schema": "<json schema as string>"
    }
  ],
  "pip_packages": ["httpx", "pandas"],
  "extra_nodes":  { "deb-0": "http://127.0.0.1:8080" }
}
```

Response:

```json
{
  "status": "completed",
  "node_id": "new-node-id",
  "port": 8083,
  "steps_completed": [
    "create_directory",
    "write_config",
    "install_packages",
    "write_actions",
    "start_process",
    "verify_health"
  ]
}
```

On failure:

```json
{
  "status": "rolled_back",
  "node_id": "new-node-id",
  "failed_step": "install_packages",
  "error": "pip install failed: ...",
  "steps_rolled_back": ["write_config", "create_directory"]
}
```

---

## Part 1 — Bootstrap a New Node via curl

Let's create `deb-2` on deb-0's machine with a custom `greet` action:

```bash
export TOKEN="change-this-to-a-strong-secret"

# Read the action code into a variable (JSON-escape newlines)
ACTION_CODE=$(cat << 'PYEOF'
def run(params, context):
    greeting = params.get("greeting", "Hello")
    target = params.get("target", "World")
    return {
        "message": f"{greeting}, {target}! From node {context.get('node_id')}",
        "node_id": context.get("node_id"),
    }
PYEOF
)

ACTION_SCHEMA=$(cat << 'JSEOF'
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "greet",
  "description": "Return a customizable greeting from this node.",
  "type": "object",
  "properties": {
    "greeting": {"type": "string", "default": "Hello"},
    "target":   {"type": "string", "default": "World"}
  },
  "additionalProperties": false
}
JSEOF
)

# Build and send the bootstrap request
python3 - << 'SCRIPT'
import json, subprocess, os

token = os.environ["TOKEN"]

payload = {
    "node_id":    "deb-2",
    "port":       8083,
    "auth_token": "deb-2-secret-token",
    "actions": [
        {
            "name":   "greet",
            "code":   open("/dev/stdin").read() if False else """
def run(params, context):
    greeting = params.get("greeting", "Hello")
    target = params.get("target", "World")
    return {
        "message": f"{greeting}, {target}! From node {context.get('node_id')}",
        "node_id": context.get("node_id"),
    }
""",
            "schema": json.dumps({
                "$schema": "http://json-schema.org/draft-07/schema#",
                "title": "greet",
                "description": "Return a customizable greeting from this node.",
                "type": "object",
                "properties": {
                    "greeting": {"type": "string", "default": "Hello"},
                    "target":   {"type": "string", "default": "World"}
                },
                "additionalProperties": False
            })
        }
    ],
    "pip_packages": [],
    "extra_nodes": {"deb-0": "http://127.0.0.1:8080"},
}

result = subprocess.run(
    ["curl", "-s", "-X", "POST", "http://localhost:8080/bootstrap",
     "-H", f"Authorization: Bearer {token}",
     "-H", "Content-Type: application/json",
     "-d", json.dumps(payload)],
    capture_output=True, text=True
)
print(json.dumps(json.loads(result.stdout), indent=2))
SCRIPT
```

---

## Part 2 — Bootstrap via /intent (LLM-Driven)

This is where GNOT's power becomes visible. Tell the LLM what you want; it figures out the bootstrap call:

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Create a new GNOT node called deb-3 on port 8084. Give it a custom action called get_process_count that returns the number of currently running processes. Register it with this gateway and verify it is online.",
    "session_id": "bootstrap-demo"
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

The LLM will:
1. Design the `get_process_count` action code and schema
2. Call `POST /bootstrap` with a complete payload
3. Verify health via `GET /health` on the new node
4. Report the result

---

## Part 3 — Bootstrap with pip Packages

Bootstrap a node that needs external libraries:

```bash
curl -s -X POST http://localhost:8080/bootstrap \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "node_id": "analytics-node",
    "port": 8085,
    "auth_token": "analytics-secret",
    "pip_packages": ["psutil", "requests"],
    "actions": [
      {
        "name": "system_metrics",
        "code": "import psutil\ndef run(params, context):\n    return {\n        \"cpu_percent\": psutil.cpu_percent(interval=1),\n        \"memory_percent\": psutil.virtual_memory().percent,\n        \"disk_percent\": psutil.disk_usage(\"/\").percent,\n        \"process_count\": len(psutil.pids()),\n    }\n",
        "schema": "{\"$schema\":\"http://json-schema.org/draft-07/schema#\",\"title\":\"system_metrics\",\"description\":\"Get CPU, memory, disk, and process metrics.\",\"type\":\"object\",\"properties\":{},\"additionalProperties\":false}"
      }
    ]
  }' | python3 -m json.tool
```

The bootstrap engine installs `psutil` and `requests` via pip before starting the node.

---

## Part 4 — Auto-Scale Based on Queue Depth

A practical auto-scaling pattern: monitor `GET /health` and bootstrap new nodes when the queue is overloaded.

```python
#!/usr/bin/env python3
"""
Simple auto-scaler: bootstrap a new worker if any node's queue depth > threshold.
"""

import httpx, json, time, os

GATEWAY = os.environ.get("NODE_URL", "http://localhost:8080")
TOKEN   = os.environ.get("TOKEN", "change-this-to-a-strong-secret")
THRESHOLD = 5   # queue depth that triggers scale-out

def get_health():
    r = httpx.get(f"{GATEWAY}/health", headers={"Authorization": f"Bearer {TOKEN}"})
    return r.json()

def bootstrap_worker(node_id: str, port: int):
    payload = {
        "node_id":    node_id,
        "port":       port,
        "auth_token": f"{node_id}-secret",
        "actions":    [],   # seed actions only
    }
    r = httpx.post(
        f"{GATEWAY}/bootstrap",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json=payload,
        timeout=120,
    )
    return r.json()

def check_and_scale():
    health = get_health()
    queue_depths = health.get("queue_depths", {})

    for node_id, depth in queue_depths.items():
        if depth > THRESHOLD:
            new_id = f"{node_id}-overflow-{int(time.time())}"
            new_port = 8090 + (int(time.time()) % 100)
            print(f"[auto-scale] {node_id} queue={depth} > {THRESHOLD}. Bootstrapping {new_id} on :{new_port}")
            result = bootstrap_worker(new_id, new_port)
            print(f"[auto-scale] Result: {result['status']}")

if __name__ == "__main__":
    print("Auto-scaler running (check every 30s)...")
    while True:
        check_and_scale()
        time.sleep(30)
```

---

## Bootstrap Step Sequence

When `POST /bootstrap` is called, these steps execute in order (with rollback on failure):

1. `create_directory` — `mkdir -p /tmp/gnot-nodes/<node_id>/actions`
2. `write_config` — write `node.yaml`
3. `install_packages` — `pip install <pip_packages>` (skipped if empty)
4. `write_actions` — write `.py` and `.schema.json` files
5. `start_process` — `nohup python gnot/src/node_runtime.py --config ...`
6. `verify_health` — `GET /health` with retry (up to 10s)

---

## Summary

You can now:
- ✅ Create new nodes programmatically via `POST /bootstrap`
- ✅ Include custom actions and pip dependencies in the bootstrap payload
- ✅ Use `/intent` for LLM-driven auto-provisioning
- ✅ Build auto-scaling logic on top of `GET /health` queue depths

**Next:** [Guide 14 — Full Mesh Topology (5+ Nodes)](../14-full-mesh/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
