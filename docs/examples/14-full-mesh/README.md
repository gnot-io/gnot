# Guide 14 — Full Mesh Topology (5+ Nodes)

**Difficulty:** Advanced  
**Prerequisite:** [Guide 13 — Auto-Scale Additional Nodes](../13-auto-scale/README.md)  
**Goal:** Operate a full 5-node mesh (deb-0 through deb-2, cen-0, alm-0), verify routing health, and run a fan-out workload across all nodes simultaneously.

---

## Target Topology

```
                    Internet
                       │
             https://deb0.yourdomain.com
                       │
              ┌────────▼─────────┐
              │  deb-0 (gateway) │  Debian, public
              │  port 8080       │
              └──────┬───────────┘
                     │  (all workers poll outbound)
        ┌────────────┼────────────────┬─────────────┐
        ▼            ▼                ▼             ▼
 ┌──────────┐ ┌──────────┐   ┌──────────┐  ┌──────────┐
 │  deb-1   │ │  deb-2   │   │  cen-0   │  │  alm-0   │
 │ port 8081│ │ port 8083│   │ CentOS   │  │AlmaLinux │
 │ (local)  │ │ (local)  │   │ NAT net1 │  │ NAT net2 │
 └──────────┘ └──────────┘   └──────────┘  └──────────┘
      │
      └─── deb-1a (sub-node, port 8082)
```

By this point you have set up all these nodes in earlier guides. This guide focuses on **health verification** and **full-mesh workloads**.

---

## Part 1 — Verify All Nodes are Online

```bash
export TOKEN="change-this-to-a-strong-secret"

# Full capabilities tree
curl -s http://localhost:8080/capabilities \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import sys, json

def print_node(node, indent=0):
    prefix = '  ' * indent
    status = node.get('status', 'unknown')
    actions = node.get('actions', [])
    icon = '✅' if status == 'online' else '❌'
    print(f'{prefix}{icon}  {node[\"node_id\"]}  ({status})  actions: {len(actions)}')
    for child in node.get('reachable', {}).values():
        print_node(child, indent + 1)

caps = json.load(sys.stdin)
print_node(caps)
"
```

Expected output:

```
✅  deb-0  (online)  actions: 5
  ✅  deb-1  (online)  actions: 8
    ✅  deb-1a  (online)  actions: 6
  ✅  deb-2  (online)  actions: 6
  ✅  cen-0  (online)  actions: 5
  ✅  alm-0  (online)  actions: 5
```

Check the queue depths (should all be 0 at idle):

```bash
curl -s http://localhost:8080/health \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

---

## Part 2 — Fan-Out Health Check

Run the same command on all nodes at once:

```bash
NODES=("deb-0" "deb-1" "deb-1a" "deb-2" "cen-0" "alm-0")
JOBS=()

# Submit all jobs simultaneously
for NODE in "${NODES[@]}"; do
    JOB=$(curl -s -X POST http://localhost:8080/action \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "{
        \"target_node_id\": \"$NODE\",
        \"payload\": {
          \"action\": \"execute_command\",
          \"params\": {\"command\": \"printf '$NODE: '; uname -r; df -h / | tail -1 | awk '{print \\\"disk:\\\" \$5}'; free -h | grep Mem | awk '{print \\\"mem_free:\\\" \$4}'\"}
        }
      }" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('job_id','ERR'))")
    JOBS+=("$NODE:$JOB")
    echo "Submitted $NODE → $JOB"
done

echo ""
echo "Waiting for all jobs to complete..."
sleep 15

# Collect results
echo ""
echo "=== Results ==="
for ENTRY in "${JOBS[@]}"; do
    NODE="${ENTRY%%:*}"
    JOB="${ENTRY#*:}"
    STDOUT=$(curl -s "http://localhost:8080/result/$JOB" \
      -H "Authorization: Bearer $TOKEN" \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('output',{}).get('stdout','<no output>').strip())" 2>/dev/null)
    echo "$STDOUT"
done
```

---

## Part 3 — Full-Mesh Prompt via Claude Web

Open Claude Web and paste this prompt (after updating your project's capability context):

```
I want to run a health check across my entire mesh. For each node in the mesh:
1. Check CPU load average (last 1 minute)
2. Check free disk space on /
3. Check free RAM

Present the results in a markdown table with columns: Node | OS | CPU Load | Disk Free | RAM Free | Status

Mark any node where CPU > 2.0 or disk < 20% or RAM < 500MB as ⚠️ WARNING.
```

Claude will fan out `execute_command` to all 5+ nodes, collect results, and build the table.

---

## Part 4 — Cross-Mesh Data Pipeline

A more complex workload that exercises routing across all nodes:

```
Perform the following across the mesh:
1. On cen-0: create a file /tmp/mesh-data.csv with 100 rows of random numbers (use python3 -c to generate it)
2. Transfer the file to alm-0 via the gateway staging area
3. On alm-0: calculate the sum and average of all numbers using python3
4. Write the result to /tmp/mesh-result.txt on deb-0
5. Read and show me the contents of /tmp/mesh-result.txt
```

This exercises: cen-0 (generate) → deb-0 staging (upload) → alm-0 (process) → deb-0 (store) — a complete cross-network pipeline.

---

## Part 5 — Resilience Test

Test the mesh's resilience by stopping a worker and observing automatic staleness detection:

```bash
# Stop deb-1
kill $(cat ~/gnot-nodes/deb-1/node.pid)

# Wait for heartbeat timeout (default: 120s, reduce to 30s in node.yaml for testing)
sleep 35   # if heartbeat_timeout_seconds: 30

# Check capabilities — deb-1 should appear as unreachable
curl -s http://localhost:8080/capabilities \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import sys, json
caps = json.load(sys.stdin)
deb1 = caps['reachable'].get('deb-1', {})
print('deb-1 status:', deb1.get('status', 'gone'))
"

# Try sending a job to deb-1 — should fail gracefully
curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_node_id":"deb-1","payload":{"action":"execute_command","params":{"command":"echo test"}}}' \
  | python3 -m json.tool

# Restart deb-1 — it should auto-re-register
nohup python3 /path/to/gnot/gnot/src/node_runtime.py \
    --config ~/gnot-nodes/deb-1/node.yaml \
    > ~/gnot-nodes/deb-1/node.log 2>&1 &
echo $! > ~/gnot-nodes/deb-1/node.pid
```

---

## Mesh Management Commands

```bash
# List all registered nodes
curl -s http://localhost:8080/nodes \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# Check all active jobs across the mesh
curl -s http://localhost:8080/health \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# List staged files in the transfer area
curl -s http://localhost:8080/files \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

---

## Summary

You now have a fully operational 5+ node mesh:
- ✅ Multi-network topology with NAT traversal
- ✅ Sub-node routing via BGP-style advertisement
- ✅ Fan-out workloads across all nodes
- ✅ Resilience tested — nodes self-recover on restart

**Next:** [Guide 15 — Security Hardening](../15-security-hardening/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
