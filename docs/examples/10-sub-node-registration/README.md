# Guide 10 — Sub-Node Registration

**Difficulty:** Intermediate  
**Prerequisite:** [Guide 09 — Execute Across Multiple Nodes](../09-multi-node-execution/README.md)  
**Goal:** Register deb-1 as a sub-node of deb-0 and observe how GNOT's BGP-inspired route advertisement makes deb-1's actions visible through deb-0's capabilities tree.

---

## Why Sub-Node Registration Matters

In a flat mesh, every node registers directly with the gateway. But in real deployments, some nodes may only be reachable through an intermediate node — not the gateway directly. This is the sub-node pattern:

```
Internet
   │
   ▼
deb-0  (gateway — public)
   │
   │  pull-mode polling (deb-1 calls home to deb-0)
   ▼
deb-1  (worker — may be private or behind NAT)
   │
   │  pull-mode polling (deb-1a calls home to deb-1)
   ▼
deb-1a  (sub-worker — deeper in the network)
```

Even though deb-0 has no direct path to deb-1a, it can still route requests to deb-1a — because deb-1 **advertises** deb-1a's routes upward, and deb-0 learns the next-hop is deb-1. This is the same principle as BGP route advertisement on the internet.

---

## How It Works (BGP Analogy)

| BGP Concept | GNOT Concept |
|------------|-------------|
| Autonomous System | Node |
| IP prefix advertisement | Node ID advertisement in `advertise_routes` |
| BGP neighbor | Gateway node |
| Next-hop router | `next_hop` in NodeRegistry |
| BGP UPDATE | Periodic re-registration |
| BGP WITHDRAW | Re-registration with fewer routes |

When deb-1a registers with deb-1:
1. deb-1 adds deb-1a to its registry
2. deb-1 re-registers with deb-0, including `advertise_routes: ["deb-1a"]`
3. deb-0 adds deb-1a to its registry with `next_hop = "deb-1"`
4. `GET /capabilities` on deb-0 now shows deb-1a

When deb-0 receives `POST /action { target: "deb-1a" }`:
1. It looks up deb-1a → next_hop = deb-1
2. It forwards the request to deb-1
3. deb-1 looks up deb-1a → direct child
4. deb-1 dispatches to deb-1a

---

## Part 1 — Create a Sub-Node (deb-1a)

For this guide, deb-1a runs on the same machine on port 8082. In a real deployment it would be on a different machine that only has network access to deb-1.

### Create actions and config directory

```bash
mkdir -p ~/gnot-nodes/deb-1a/actions
# Copy seed actions
cp /path/to/gnot/mesh/seed/actions/*.py   ~/gnot-nodes/deb-1a/actions/
cp /path/to/gnot/mesh/seed/actions/*.json ~/gnot-nodes/deb-1a/actions/
```

### Add a unique `identify.py` action

```python
# ~/gnot-nodes/deb-1a/actions/identify.py

def run(params: dict, context: dict) -> dict:
    """Return identification info for this sub-node."""
    import platform, socket
    return {
        "node_id": context.get("node_id", "unknown"),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "depth": "sub-node (deb-1a, routed via deb-1 → deb-0)",
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "identify",
  "description": "Return identification information for this sub-node.",
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

### Create `~/gnot-nodes/deb-1a/node.yaml`

```yaml
# ~/gnot-nodes/deb-1a/node.yaml

node_id: deb-1a
listen:  0.0.0.0:8082

actions_dir: /home/YOUR_USER/gnot-nodes/deb-1a/actions

auth_token: deb-1a-secret-token
gateway_auth_token: deb-1-secret-token   # token deb-1a uses when talking to deb-1

# ── Sub-node: registers with deb-1 (not deb-0 directly) ─────────────────────
gateway_node_id: deb-1
gateway_address: http://127.0.0.1:8081   # deb-1's address (same machine here)

# Enable push mode if deb-1 can reach deb-1a directly (same machine: yes)
self_address: http://127.0.0.1:8082

heartbeat_interval_seconds: 15
poll_interval_seconds: 5

job_ttl_seconds: 3600
cleanup_interval_seconds: 60
```

---

## Part 2 — Update deb-1 to Trust deb-1a

Edit `~/gnot-nodes/deb-1/node.yaml`:

```yaml
# Add deb-1a as a trusted sub-node
trusted_nodes:
  - deb-1a

allowed_tokens:
  - change-this-to-a-strong-secret   # from deb-0 forwarding requests
  - deb-1-secret-token               # deb-1's own callers
  - deb-1a-secret-token              # deb-1a registering
```

Restart deb-1:

```bash
kill $(cat ~/gnot-nodes/deb-1/node.pid)
cd /path/to/gnot
nohup python3 mesh/node_runtime.py --config ~/gnot-nodes/deb-1/node.yaml \
    > ~/gnot-nodes/deb-1/node.log 2>&1 &
echo $! > ~/gnot-nodes/deb-1/node.pid
```

---

## Part 3 — Start deb-1a

```bash
nohup python3 /path/to/gnot/mesh/node_runtime.py \
    --config ~/gnot-nodes/deb-1a/node.yaml \
    > ~/gnot-nodes/deb-1a/node.log 2>&1 &
echo $! > ~/gnot-nodes/deb-1a/node.pid
```

Watch deb-1a's log:

```bash
tail -20 ~/gnot-nodes/deb-1a/node.log
```

Expected:

```
INFO  WorkerAgent: registering with gateway deb-1 at http://127.0.0.1:8081
INFO  WorkerAgent: registration successful
```

Watch deb-1's log — it should re-register with deb-0 advertising deb-1a:

```bash
tail -20 ~/gnot-nodes/deb-1/node.log
```

Expected:

```
INFO  Sub-node deb-1a registered. Advertising routes upstream to deb-0.
INFO  WorkerAgent: re-registering with deb-0 (advertise_routes=['deb-1a'])
```

---

## Part 4 — Verify Route Propagation

```bash
curl -s http://localhost:8080/capabilities \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  | python3 -m json.tool
```

Expected structure — deb-1a appears under deb-1's `reachable`:

```json
{
  "node_id": "deb-0",
  "actions": [...],
  "reachable": {
    "deb-1": {
      "node_id": "deb-1",
      "status": "online",
      "actions": [...],
      "reachable": {
        "deb-1a": {
          "node_id": "deb-1a",
          "status": "online",
          "actions": ["execute_command", "read_file", "write_file", "identify"],
          "next_hop": null
        }
      }
    }
  }
}
```

> Note: From deb-0's perspective, `deb-1a` appears with `next_hop: "deb-1"` internally. The capabilities tree shows it nested under deb-1.

---

## Part 5 — Call deb-1a Through deb-0

Requests to deb-1a go through the same deb-0 endpoint — routing is transparent:

```bash
curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "deb-1a",
    "payload": {
      "action": "identify",
      "params": {}
    }
  }' | python3 -m json.tool
```

Expected:

```json
{
    "node_id": "deb-1a",
    "hostname": "deb-0",
    "platform": "Linux-6.1.0-...",
    "depth": "sub-node (deb-1a, routed via deb-1 → deb-0)"
}
```

The request path was: `POST /action on deb-0` → `forwarded to deb-1` → `dispatched to deb-1a`. Three hops, transparent to the caller.

---

## Part 6 — Observe Route Withdrawal

Stop deb-1a and watch the route disappear:

```bash
kill $(cat ~/gnot-nodes/deb-1a/node.pid)

# Wait ~30 seconds for heartbeat timeout, then check capabilities
sleep 35

curl -s http://localhost:8080/capabilities \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  | python3 -c "
import sys, json
caps = json.load(sys.stdin)
deb1 = caps['reachable'].get('deb-1', {})
deb1a = deb1.get('reachable', {}).get('deb-1a', 'NOT FOUND')
print('deb-1a status:', deb1a.get('status', 'gone') if isinstance(deb1a, dict) else deb1a)
"
```

When deb-1a stops sending heartbeats, deb-1 marks it unreachable and stops advertising it upward. Calls to `target_node_id: "deb-1a"` will fail with a routing error.

---

## Summary

You now understand:
- ✅ How sub-nodes register with intermediate worker nodes
- ✅ BGP-style route advertisement propagates routes upward
- ✅ Routing is transparent — callers target node IDs, not addresses
- ✅ Route withdrawal happens automatically when a node goes offline

**Next:** [Guide 11 — NAT Traversal](../11-nat-traversal/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
