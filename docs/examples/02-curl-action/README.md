# Guide 02 — Call an Action via curl

**Difficulty:** Beginner  
**Prerequisite:** [Guide 01 — Set Up Your First GNOT Node](../01-setup-deb0/README.md)  
**Goal:** Execute the three seed actions (`execute_command`, `read_file`, `write_file`) directly from the command line using curl.

---

## How `POST /action` Works

Every action call follows the same envelope:

```
POST /action
Authorization: Bearer <your-token>
Content-Type: application/json

{
  "target_node_id": "<node to run on>",
  "payload": {
    "action": "<action name>",
    "params": { ... }
  }
}
```

The response is either a **synchronous result** (for fast actions) or a **job reference** for polling (for `execute_command`, which is always async):

```json
{ "job_id": "deb-0-job-a1b2c3d4", "status": "accepted" }
```

Then poll for the result:

```
GET /result/<job_id>
Authorization: Bearer <your-token>
```

Set these shell variables once so you don't repeat them in every command:

```bash
export NODE_URL="http://localhost:8080"
export TOKEN="change-this-to-a-strong-secret"   # must match auth_token in node.yaml
```

---

## Example 1 — Run a Shell Command

### Send the request

```bash
curl -s -X POST "$NODE_URL/action" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "deb-0",
    "payload": {
      "action": "execute_command",
      "params": { "command": "hostname && uptime" }
    }
  }' | python3 -m json.tool
```

Response:

```json
{
    "job_id": "deb-0-job-a1b2c3d4",
    "status": "accepted",
    "task_id": "task-xxxx"
}
```

`execute_command` is always asynchronous — it returns a `job_id` immediately and runs in the background.

### Poll for the result

```bash
JOB_ID="deb-0-job-a1b2c3d4"   # replace with actual job_id from above

curl -s "$NODE_URL/result/$JOB_ID" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

Response when complete:

```json
{
    "job_id": "deb-0-job-a1b2c3d4",
    "status": "completed",
    "output": {
        "exit_code": 0,
        "stdout": "deb-0\n up 2 days, 14:23,  1 user,  load average: 0.05, 0.03, 0.01\n",
        "stderr": ""
    }
}
```

> **Tip — poll in a loop:**
> ```bash
> while true; do
>   RESULT=$(curl -s "$NODE_URL/result/$JOB_ID" -H "Authorization: Bearer $TOKEN")
>   STATUS=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
>   echo "Status: $STATUS"
>   [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] && break
>   sleep 2
> done
> echo $RESULT | python3 -m json.tool
> ```

### One-liner: submit + auto-poll

`mesh_ctl.py` (included in the repo) handles the submit-then-poll loop automatically:

```bash
cd /path/to/gnot/mesh

python3 mesh_ctl.py run deb-0 execute_command \
  '{"command": "hostname && uptime"}'
```

Output:

```json
{
    "status": "completed",
    "output": {
        "exit_code": 0,
        "stdout": "deb-0\n up 2 days, 14:23 ...\n",
        "stderr": ""
    }
}
```

`mesh_ctl.py` polls every 3 seconds and waits up to 10 minutes. For most commands this is all you need.

---

## Example 2 — Read a File

`read_file` is synchronous (returns the result directly, no polling needed):

```bash
curl -s -X POST "$NODE_URL/action" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "deb-0",
    "payload": {
      "action": "read_file",
      "params": { "path": "/etc/os-release" }
    }
  }' | python3 -m json.tool
```

Response:

```json
{
    "content": "PRETTY_NAME=\"Debian GNU/Linux 12 (bookworm)\"\nNAME=\"Debian GNU/Linux\"\n...",
    "path": "/etc/os-release",
    "size_bytes": 389
}
```

Using `mesh_ctl.py`:

```bash
python3 mesh_ctl.py run deb-0 read_file '{"path": "/etc/os-release"}'
```

---

## Example 3 — Write a File

```bash
curl -s -X POST "$NODE_URL/action" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "deb-0",
    "payload": {
      "action": "write_file",
      "params": {
        "path": "/tmp/gnot-hello.txt",
        "content": "Hello from GNOT!\nWritten via POST /action\n"
      }
    }
  }' | python3 -m json.tool
```

Response:

```json
{
    "success": true,
    "path": "/tmp/gnot-hello.txt",
    "size_bytes": 40
}
```

Verify:

```bash
python3 mesh_ctl.py run deb-0 read_file '{"path": "/tmp/gnot-hello.txt"}'
```

---

## Example 4 — Run a Long Command with Timeout

```bash
python3 mesh_ctl.py run deb-0 execute_command \
  '{"command": "sleep 5 && echo done", "timeout_seconds": 30}'
```

The `timeout_seconds` parameter (default: 60, max: 3600) aborts the process if it runs too long. A timed-out job returns `exit_code: -1`.

---

## Example 5 — Check All Available Actions

```bash
curl -s "$NODE_URL/capabilities" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

This is the **capability discovery** endpoint — it lists every action available on this node (and any connected worker nodes). An LLM reads this to understand what the mesh can do.

---

## Action Reference (Seed Actions)

| Action | Mode | Required params | Optional params |
|--------|------|-----------------|-----------------|
| `execute_command` | async | `command` (string) | `timeout_seconds` (int, default 60) |
| `read_file` | sync | `path` (string) | — |
| `read_file_b64` | sync | `path` (string) | — |
| `write_file` | sync | `path`, `content` (strings) | — |
| `write_file_b64` | sync | `path`, `content_b64` (strings) | — |

> **Sync vs Async:** Synchronous actions return the result directly in the HTTP response. Asynchronous actions (currently only `execute_command`) return a `job_id` — you must poll `GET /result/{job_id}` to get the output.

---

## Troubleshooting

**`401 Unauthorized`:** Check that your `TOKEN` variable matches `auth_token` in `node.yaml`.

**`422 Unprocessable Entity`:** The request body has a schema error. Check `target_node_id` is set and `payload.action` matches an action name exactly.

**`status: "failed"` with `exit_code: 1`:** The command ran but returned a non-zero exit code. Check `stderr` in the response for details.

**`status: "queued"` — never changes:** The node is overwhelmed or crashed. Check the node log: `tail -f ~/gnot-nodes/deb-0/node.log`

---

## Summary

You can now:
- ✅ Call any action on deb-0 via `POST /action`
- ✅ Handle the async job pattern: submit → poll `GET /result/{job_id}`
- ✅ Use `mesh_ctl.py` as a convenient CLI wrapper
- ✅ Execute commands, read and write files on the node

**Next:** [Guide 03 — Call an Action from Claude Web](../03-claude-web-action/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
