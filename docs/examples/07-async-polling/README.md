# Guide 07 — Long-Running Actions and Async Polling

**Difficulty:** Intermediate  
**Prerequisite:** [Guide 06 — Create deb-1 with a Custom "hello" Action](../06-create-deb1-hello-action/README.md)  
**Goal:** Write an async action that takes time to complete, understand the job lifecycle, and poll for results — both manually with curl and through Claude Web.

---

## Sync vs Async Actions

| Type | How to declare | When to use | Response |
|------|---------------|-------------|----------|
| **Sync** | No special flag (default) | Fast operations (<1s) | Result returned directly in HTTP response |
| **Async** | `ASYNC = True` at module level | Slow operations, shell commands, I/O | Returns `job_id`; caller polls `GET /result/{job_id}` |

All `execute_command` actions are async by default. Your custom actions can also be async — useful for anything that takes seconds or minutes.

---

## Part 1 — Write an Async Action

### Create `slow_report.py`

```python
# ~/gnot-nodes/deb-1/actions/slow_report.py
"""
Async action that generates a system report after a configurable delay.
Demonstrates ASYNC = True and long-running action patterns.
"""

import asyncio
import platform
import time

ASYNC = True   # ← this is all it takes to make an action async


async def run(params: dict, context: dict) -> dict:
    """
    Simulate a slow operation (e.g. scanning files, querying a DB)
    then return a system report.
    """
    delay_seconds = params.get("delay_seconds", 5)
    include_env = params.get("include_env", False)

    start = time.time()

    # Simulate slow work
    await asyncio.sleep(delay_seconds)

    # Gather system info
    import subprocess

    def run_cmd(cmd):
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return result.stdout.strip()

    report = {
        "hostname":     platform.node(),
        "os":           platform.platform(),
        "python":       platform.python_version(),
        "uptime":       run_cmd("uptime -p"),
        "disk_usage":   run_cmd("df -h / | tail -1 | awk '{print $5}'"),
        "memory_free":  run_cmd("free -h | grep Mem | awk '{print $4}'"),
        "elapsed_seconds": round(time.time() - start, 2),
    }

    if include_env:
        import os
        # Only include safe env vars
        safe_keys = ["PATH", "HOME", "USER", "SHELL", "LANG"]
        report["env"] = {k: os.environ.get(k, "") for k in safe_keys}

    return report
```

### Create `slow_report.schema.json`

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "slow_report",
  "description": "Generate a system report after a configurable delay. Useful for testing async behavior.",
  "type": "object",
  "properties": {
    "delay_seconds": {
      "type": "integer",
      "description": "Seconds to wait before generating the report (simulates slow work)",
      "minimum": 1,
      "maximum": 300,
      "default": 5
    },
    "include_env": {
      "type": "boolean",
      "description": "Include selected environment variables in the report",
      "default": false
    }
  },
  "additionalProperties": false
}
```

Save both files in `~/gnot-nodes/deb-1/actions/`.

Restart deb-1 to load the new action:

```bash
kill $(cat ~/gnot-nodes/deb-1/node.pid)
cd /path/to/gnot
nohup python3 gnot/src/node_runtime.py --config ~/gnot-nodes/deb-1/node.yaml \
    > ~/gnot-nodes/deb-1/node.log 2>&1 &
echo $! > ~/gnot-nodes/deb-1/node.pid
```

---

## Part 2 — Manual Async Polling with curl

### Step 1: Submit the action

```bash
export TOKEN="change-this-to-a-strong-secret"

RESPONSE=$(curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "deb-1",
    "payload": {
      "action": "slow_report",
      "params": { "delay_seconds": 8 }
    }
  }')

echo $RESPONSE | python3 -m json.tool
```

Response is immediate:

```json
{
    "job_id": "deb-1-job-a1b2c3d4",
    "status": "accepted",
    "task_id": "task-xxxx"
}
```

### Step 2: Extract the job_id

```bash
JOB_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "Job ID: $JOB_ID"
```

### Step 3: Poll for status

```bash
# Poll once — likely still running
curl -s "http://localhost:8080/result/$JOB_ID" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

While running:

```json
{
    "job_id": "deb-1-job-a1b2c3d4",
    "status": "running"
}
```

Possible status values:

| Status | Meaning |
|--------|---------|
| `queued` | Job is in the pull queue, waiting for deb-1 to pick it up |
| `running` | deb-1 is executing the action |
| `completed` | Action finished successfully — `output` field has the result |
| `failed` | Action raised an exception — `error` field has the message |

### Step 4: Poll in a loop

```bash
while true; do
  RESULT=$(curl -s "http://localhost:8080/result/$JOB_ID" \
    -H "Authorization: Bearer $TOKEN")
  STATUS=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "$(date '+%H:%M:%S')  status: $STATUS"
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    echo ""
    echo $RESULT | python3 -m json.tool
    break
  fi
  sleep 2
done
```

After ~8 seconds:

```json
{
    "job_id": "deb-1-job-a1b2c3d4",
    "status": "completed",
    "output": {
        "hostname": "deb-0",
        "os": "Linux-6.1.0-debian-amd64",
        "python": "3.11.2",
        "uptime": "up 2 days, 3 hours",
        "disk_usage": "42%",
        "memory_free": "1.2G",
        "elapsed_seconds": 8.01
    }
}
```

---

## Part 3 — Using mesh_ctl.py (Auto-Poll)

`mesh_ctl.py` handles the entire submit + poll loop:

```bash
cd /path/to/gnot/gnot/src

python3 mesh_ctl.py run deb-1 slow_report \
  '{"delay_seconds": 10, "include_env": true}'
```

It polls every 3 seconds and prints the final result when done. No manual polling needed.

To get the `job_id` without waiting (fire and forget):

```bash
python3 mesh_ctl.py run deb-1 slow_report \
  '{"delay_seconds": 30}' --no-wait
# Output: { "job_id": "deb-1-job-xxxx", "status": "accepted" }
```

Then poll later:

```bash
python3 mesh_ctl.py result deb-1-job-xxxx
```

---

## Part 4 — Calling from Claude Web

In your Claude Web session, try:

```
On deb-1, run the slow_report action with a 10-second delay.
While we wait, tell me — what is a ReAct agent loop?
Then show me the report when it's ready.
```

Claude will:
1. Submit the async job to deb-1 (gets `job_id`)
2. Answer your question about ReAct loops (using its own knowledge — no action needed)
3. Poll `GET /result/{job_id}` and report the result when done

This is the natural async pattern: submit work, do other things, collect results.

---

## Part 5 — Multiple Concurrent Jobs

You can submit several async jobs and poll them in parallel:

```bash
# Submit three jobs at once
JOB1=$(curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_node_id":"deb-1","payload":{"action":"slow_report","params":{"delay_seconds":5}}}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

JOB2=$(curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_node_id":"deb-0","payload":{"action":"execute_command","params":{"command":"sleep 3 && echo job2-done"}}}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

echo "Job 1: $JOB1"
echo "Job 2: $JOB2"

# Poll both simultaneously
for JOB in $JOB1 $JOB2; do
  STATUS=$(curl -s "http://localhost:8080/result/$JOB" \
    -H "Authorization: Bearer $TOKEN" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "$JOB → $STATUS"
done
```

---

## Job Lifecycle Summary

```
POST /action submitted
        │
        ▼
    ┌─────────────────────────────────────────────┐
    │  Gateway                                     │
    │                                             │
    │  target is deb-0 (local)?  ─── yes ──►  run immediately
    │         │                                    │
    │         no                                   │
    │         ▼                                    │
    │  deb-1 reachable directly?  ─── yes ──►  push job → run
    │         │                                    │
    │         no (behind NAT)                      │
    │         ▼                                    │
    │  enqueue in pull queue  ◄───────────────────┘
    │         │
    │         ▼  (deb-1 polls every 5s)
    │  deb-1 claims job → status: running
    │         │
    │         ▼
    │  deb-1 executes action
    │         │
    │         ▼
    │  deb-1 reports result → status: completed/failed
    └─────────────────────────────────────────────┘
            │
            ▼
    GET /result/{job_id}  →  final result
```

---

## Summary

You now understand:
- ✅ How to write an async action (`ASYNC = True`)
- ✅ The job lifecycle: accepted → queued/running → completed/failed
- ✅ Manual polling with `curl GET /result/{job_id}`
- ✅ Automatic polling with `mesh_ctl.py`
- ✅ How Claude Web handles async jobs naturally

**Next:** [Guide 08 — Advanced Custom Actions](../08-advanced-actions/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
