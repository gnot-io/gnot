# Guide 09 — Execute Across Multiple Nodes

**Difficulty:** Intermediate  
**Prerequisite:** [Guide 08 — Advanced Custom Actions](../08-advanced-actions/README.md)  
**Goal:** Run actions on deb-0 and deb-1 simultaneously (or sequentially) from a single prompt, and understand how Claude orchestrates multi-node workflows.

---

## The Single-Mesh Mental Model

From the caller's perspective, all nodes are addressed through the **same gateway endpoint**. You do not need different URLs for different nodes — you just change `target_node_id`:

```
POST http://localhost:8080/action   ← always the gateway
  { "target_node_id": "deb-0", ... }   ← run on deb-0
  { "target_node_id": "deb-1", ... }   ← run on deb-1
  { "target_node_id": "cen-0", ... }   ← will run on CentOS (future guides)
```

---

## Part 1 — Parallel Execution via curl

Submit two jobs without waiting, then collect both results:

```bash
export TOKEN="change-this-to-a-strong-secret"

# Submit both jobs simultaneously
JOB_DEB0=$(curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_node_id":"deb-0","payload":{"action":"execute_command","params":{"command":"echo deb-0: $(hostname) $(uptime -p)"}}}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

JOB_DEB1=$(curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_node_id":"deb-1","payload":{"action":"execute_command","params":{"command":"echo deb-1: $(hostname) $(uptime -p)"}}}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

echo "Submitted: $JOB_DEB0  $JOB_DEB1"

# Poll both until done
for JOB in $JOB_DEB0 $JOB_DEB1; do
  while true; do
    STATUS=$(curl -s "http://localhost:8080/result/$JOB" \
      -H "Authorization: Bearer $TOKEN" \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] && break
    sleep 1
  done
  echo "--- $JOB ---"
  curl -s "http://localhost:8080/result/$JOB" \
    -H "Authorization: Bearer $TOKEN" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['output']['stdout'].strip())"
done
```

---

## Part 2 — Multi-Node via /intent

With the node-local LLM configured (Guide 04), a single prompt can drive the whole mesh:

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Check the disk usage on both deb-0 and deb-1. Compare them and tell me which node has less free space.",
    "session_id": "multi-node-demo"
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

The LLM will call `execute_command` on both nodes and compare the results in a single response.

---

## Part 3 — Multi-Node via Claude Web

In your Claude Web session:

```
Check the disk usage, memory usage, and load average on both deb-0 and deb-1.
Present the results in a comparison table.
```

Claude will:
1. Call `execute_command` on deb-0: `df -h / && free -h && uptime`
2. Call `execute_command` on deb-1: same command
3. Format both results as a markdown comparison table

Example prompt for a real use case:

```
Deploy a test file to both deb-0 and deb-1, then verify the deployment was successful on each node.
The file should be at /tmp/deployment-test.txt and contain: "Deployed at <timestamp>"
```

Claude will:
1. Get the current timestamp
2. `write_file` on deb-0 with the content
3. `write_file` on deb-1 with the content
4. `read_file` on deb-0 to verify
5. `read_file` on deb-1 to verify
6. Report success or any failures

---

## Part 4 — Fan-Out Pattern

"Run the same check on all nodes" is a common pattern:

```bash
# Via /intent: fan out to all known nodes
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "On every node in the mesh, run: df -h / | tail -1. Show the results as a table with columns: Node, Filesystem, Size, Used, Available, Use%",
    "session_id": "fanout-demo"
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

The LLM reads `GET /capabilities` to discover all nodes, then fans out the command.

---

## Summary

You can now:
- ✅ Target different nodes in the same `POST /action` call
- ✅ Submit parallel jobs and collect results
- ✅ Use `/intent` to orchestrate multi-node workflows with a single prompt

**Next:** [Guide 10 — Sub-Node Registration](../10-sub-node-registration/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
