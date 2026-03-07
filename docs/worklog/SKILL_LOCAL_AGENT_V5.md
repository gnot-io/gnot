---
name: local-agent-api
description: Use this skill whenever the user wants Claude to work directly on their local system — reading files, uploading code, running commands, managing services, or performing any operation on a remote codebase via the Local Agent API. Trigger this skill when the user mentions a repository code/ID, asks Claude to deploy, test, or run something on their machine, or when any workflow involves reading from or writing to a local codebase through an API. This skill is essential for end-to-end coding workflows where Claude should autonomously code, upload, test, and fix — without the user needing to copy-paste anything manually.
---

# Local Agent API Skill (Execution Mesh v5)

This skill enables Claude to operate autonomously on the user's local system via the Execution Mesh v5 runtime. Claude can read files, write files, execute shell commands, manage services, and run full dev workflows — all through a unified action-based API.

---

## Setup

| Parameter | Value |
|-----------|-------|
| `BASE_URL` | `https://local-agent-server-v5.vietml.com` |
| `TOKEN` | `mesh-secret-token-v51` |
| `NODE` | `node-0` (seed node, default target) |

All calls use the same `POST /action` endpoint with an **action envelope**.

---

## Core Rules

1. **Always propose plan first** — explain what you'll do, wait for user confirmation before executing
2. **Never edit remote files directly** — develop locally in `/home/claude` first, then write to remote
3. **4-space indentation always** — never use tabs
4. **TLS errors** — retry silently using `--retry 5 --retry-delay 3`, never notify user unless all retries fail
5. **Match existing code style** — read surrounding files before writing new ones
6. **Use task_id wisely** — use descriptive task IDs like `read-config-001`, `upload-main-py-002`

---

## API Format

Every operation uses a single endpoint: `POST /action`

### Request Envelope

```json
{
    "target_node_id": "node-0",
    "task_id": "descriptive-task-id",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {
        "action": "ACTION_NAME",
        "params": { ... }
    }
}
```

### curl Template

```bash
curl -s --retry 5 --retry-delay 3 -X POST $BASE_URL/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{ ... envelope ... }'
```

---

## Actions Reference

### Action 1 — read_file (Sync)

Read content from a file on the remote system.

```bash
curl -s --retry 5 --retry-delay 3 -X POST https://local-agent-server-v5.vietml.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-v51" \
  -d '{
    "target_node_id": "node-0",
    "task_id": "read-001",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {
        "action": "read_file",
        "params": {"path": "/absolute/path/to/file"}
    }
  }'
```

**Response (200):**
```json
{
    "task_id": "read-001",
    "status": "completed",
    "output": {
        "content": "file content here...",
        "size_bytes": 1234
    }
}
```

**Params schema:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | ✅ | Absolute path to file |

### Action 2 — write_file (Sync)

Write content to a file. Creates parent directories automatically.

```bash
curl -s --retry 5 --retry-delay 3 -X POST https://local-agent-server-v5.vietml.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-v51" \
  -d '{
    "target_node_id": "node-0",
    "task_id": "write-001",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {
        "action": "write_file",
        "params": {"path": "/absolute/path/to/file", "content": "file content"}
    }
  }'
```

**Response (200):**
```json
{
    "task_id": "write-001",
    "status": "completed",
    "output": {
        "success": true,
        "path": "/absolute/path/to/file"
    }
}
```

**Params schema:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | ✅ | Absolute path to write |
| `content` | string | ✅ | Content to write |

### Action 3 — execute_command (Async)

Execute a shell command. Returns a `job_id` — poll for result.

```bash
curl -s --retry 5 --retry-delay 3 -X POST https://local-agent-server-v5.vietml.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-v51" \
  -d '{
    "target_node_id": "node-0",
    "task_id": "exec-001",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {
        "action": "execute_command",
        "params": {"command": "ls -la /path/to/dir", "timeout_seconds": 60}
    }
  }'
```

**Response (202 Accepted):**
```json
{
    "task_id": "exec-001",
    "job_id": "node-0-job-abc12345",
    "status": "accepted",
    "estimated_completion_seconds": 30
}
```

**Then poll for result:**
```bash
curl -s --retry 5 --retry-delay 3 https://local-agent-server-v5.vietml.com/result/JOB_ID \
  -H "Authorization: Bearer mesh-secret-token-v51"
```

**Poll response (200):**
```json
{
    "task_id": "exec-001",
    "job_id": "node-0-job-abc12345",
    "status": "completed",
    "progress": 100,
    "output": {
        "exit_code": 0,
        "stdout": "command output...",
        "stderr": ""
    },
    "error": null
}
```

**Params schema:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `command` | string | ✅ | Shell command to execute |
| `timeout_seconds` | int | ❌ | Max exec time (1–3600, default 60) |

---

## Async Polling Pattern

`execute_command` is async. **Always poll after submitting:**

```bash
# Step 1: Submit command
RESPONSE=$(curl -s --retry 5 --retry-delay 3 -X POST https://local-agent-server-v5.vietml.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-v51" \
  -d '{
    "target_node_id": "node-0",
    "task_id": "exec-001",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {"action": "execute_command", "params": {"command": "pytest tests/ -v"}}
  }')

# Step 2: Extract job_id and poll (wait 2-3 seconds first)
JOB_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
sleep 3
curl -s --retry 5 --retry-delay 3 https://local-agent-server-v5.vietml.com/result/$JOB_ID \
  -H "Authorization: Bearer mesh-secret-token-v51"
```

**Simplified one-liner pattern (preferred for Claude):**

For convenience, Claude can combine submit + sleep + poll in one bash call:

```bash
# Submit, wait, then poll — all in one call
JOB_ID=$(curl -s --retry 5 --retry-delay 3 -X POST https://local-agent-server-v5.vietml.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-v51" \
  -d '{"target_node_id":"node-0","task_id":"exec-001","trace":{"hop_count":0,"route_path":[]},"payload":{"action":"execute_command","params":{"command":"pytest tests/ -v"}}}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])") && \
sleep 3 && \
curl -s --retry 5 --retry-delay 3 https://local-agent-server-v5.vietml.com/result/$JOB_ID \
  -H "Authorization: Bearer mesh-secret-token-v51"
```

For long-running commands (>5s), increase sleep or poll multiple times.

---

## Other Endpoints (No Auth Required)

```bash
# Health check
curl -s https://local-agent-server-v5.vietml.com/health

# Skills/capabilities description
curl -s https://local-agent-server-v5.vietml.com/skills

# Resolve node address
curl -s https://local-agent-server-v5.vietml.com/resolve/node-0 \
  -H "Authorization: Bearer mesh-secret-token-v51"
```

---

## Decision: Which Action to Use?

| Task | Action | Notes |
|------|--------|-------|
| Read a file | `read_file` | Returns content + size |
| Write/create a file | `write_file` | Auto-creates parent dirs |
| Upload code from Claude | `write_file` | Write content directly (no binary) |
| Upload 4+ files | `execute_command` | Base64 encode or use tar (see below) |
| Run git / npm / pip / docker | `execute_command` | Async, poll for result |
| Run tests | `execute_command` | Async, poll for result |
| Check logs / service status | `execute_command` | Async, poll for result |
| Restart a service | `execute_command` | Async, poll for result |
| List directory | `execute_command` | `find . -type f` or `ls -la` |
| Check file existence | `execute_command` | `test -f path && echo yes` |

---

## File Upload Strategies

### Single file: Direct write_file

```bash
# Write content directly — simplest approach
curl -s --retry 5 --retry-delay 3 -X POST https://local-agent-server-v5.vietml.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-v51" \
  -d '{
    "target_node_id": "node-0",
    "task_id": "upload-main-py",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {
        "action": "write_file",
        "params": {
            "path": "/home/debian/working/builder/experimental/my-project/src/main.py",
            "content": "import sys\n\ndef main():\n    print(\"Hello World\")\n\nif __name__ == \"__main__\":\n    main()\n"
        }
    }
  }'
```

### Multiple files: Sequential write_file calls

For 2-5 files, call `write_file` once per file. Each call is fast (sync).

### Many files (6+): Write locally, zip, transfer via base64

```bash
# Step 1: Write files locally in Claude's container
mkdir -p /home/claude/project/src
cat > /home/claude/project/src/main.py << 'EOF'
# ... file content ...
EOF
# ... more files ...

# Step 2: Zip and base64 encode
cd /home/claude/project
tar czf /tmp/upload.tar.gz .
BASE64=$(base64 -w0 /tmp/upload.tar.gz)

# Step 3: Transfer via execute_command
curl -s --retry 5 --retry-delay 3 -X POST https://local-agent-server-v5.vietml.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-v51" \
  -d "{
    \"target_node_id\": \"node-0\",
    \"task_id\": \"bulk-upload\",
    \"trace\": {\"hop_count\": 0, \"route_path\": []},
    \"payload\": {
        \"action\": \"execute_command\",
        \"params\": {
            \"command\": \"cd /target/dir && echo '$BASE64' | base64 -d | tar xzf -\"
        }
    }
  }"
```

---

## Standard Workflow

```
1. ANALYZE    → Understand request, propose solution
               → WAIT for user confirmation before proceeding

2. READ       → Use read_file to read key files
               → Use execute_command with "find" or "ls" to explore structure
               → Read relevant files to understand patterns and style

3. DEVELOP    → Write code locally in /home/claude
               → Use 4-space indentation
               → Match existing code style
               → Verify syntax before uploading

4. UPLOAD     → Single file: write_file (sync, instant)
               → Multiple files: multiple write_file calls
               → Bulk: tar+base64 via execute_command

5. TEST       → Use execute_command to run tests
               → Poll job_id for results
               → Read logs if needed

6. FIX        → If errors: loop back to step 3
               → Repeat until tests pass

7. REPORT     → Summarize what was done, what changed, test results
```

---

## Working Directory Reference

Commands run from the server's working directory. Common paths:

| Context | Path Pattern |
|---------|-------------|
| Repositories | `/home/debian/working/builder/experimental/{repo-name}/` |
| Temp files | `/tmp/` |
| Node mesh dir | Find with: `execute_command` → `find / -name "node_runtime.py" -type f 2>/dev/null` |

**Always use absolute paths** for `read_file` and `write_file`.  
If unsure of a path, first run `execute_command` with `find` or `ls` to discover it.

---

## Error Handling

| Error | HTTP Status | Meaning | Action |
|-------|-------------|---------|--------|
| `UNAUTHORIZED` | 401 | Missing Bearer token | Add `Authorization` header |
| `FORBIDDEN` | 403 | Wrong token | Check token value |
| `SCHEMA_VALIDATION_ERROR` | 422 | Invalid params (missing field, wrong type, extra property) | Fix params and retry |
| `ACTION_NOT_FOUND` | 400 | Action doesn't exist on target node | Check action name spelling |
| `NODE_NOT_FOUND` | 400 | Target node doesn't exist | Check target_node_id |
| `HOP_COUNT_EXCEEDED` | 400 | Too many routing hops | Reduce hop_count or call node directly |
| `LOOP_DETECTED` | 400 | Circular routing detected | Fix route_path |
| Job `status: "failed"` | 200 | Command execution failed | Check `error` field and `output.stderr` |
| TLS / connection error | — | Tunnel issue | Auto-retry (`--retry 5 --retry-delay 3`) |

---

## Exploration Commands

Use `execute_command` to explore the codebase:

```bash
# List directory structure
"command": "find /path/to/repo -type f -name '*.py' | head -50"

# Read package.json or requirements
"command": "cat /path/to/repo/requirements.txt"

# Check git status
"command": "cd /path/to/repo && git status && git log --oneline -10"

# Check running services
"command": "ps aux | grep python"

# Check recent logs
"command": "tail -100 /path/to/logs/app.log"

# Disk usage
"command": "df -h && du -sh /path/to/repo"

# Check if file exists
"command": "test -f /path/to/file && echo EXISTS || echo MISSING"
```

---

## Quick Reference

```bash
# === TEMPLATE: Read file ===
curl -s --retry 5 --retry-delay 3 -X POST https://local-agent-server-v5.vietml.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-v51" \
  -d '{"target_node_id":"node-0","task_id":"TASK_ID","trace":{"hop_count":0,"route_path":[]},"payload":{"action":"read_file","params":{"path":"PATH"}}}'

# === TEMPLATE: Write file ===
curl -s --retry 5 --retry-delay 3 -X POST https://local-agent-server-v5.vietml.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-v51" \
  -d '{"target_node_id":"node-0","task_id":"TASK_ID","trace":{"hop_count":0,"route_path":[]},"payload":{"action":"write_file","params":{"path":"PATH","content":"CONTENT"}}}'

# === TEMPLATE: Execute command (async → poll) ===
JOB_ID=$(curl -s --retry 5 --retry-delay 3 -X POST https://local-agent-server-v5.vietml.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-v51" \
  -d '{"target_node_id":"node-0","task_id":"TASK_ID","trace":{"hop_count":0,"route_path":[]},"payload":{"action":"execute_command","params":{"command":"COMMAND"}}}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])") && \
sleep 3 && \
curl -s --retry 5 --retry-delay 3 https://local-agent-server-v5.vietml.com/result/$JOB_ID \
  -H "Authorization: Bearer mesh-secret-token-v51"
```

---

## Best Practices

- **Explore before coding** — always read relevant files first to avoid style mismatches
- **Small targeted changes** — prefer minimal diffs over full rewrites
- **Verify before upload** — mentally check syntax and logic
- **Test after every upload** — don't assume it works
- **Use descriptive task_ids** — helps trace operations in logs
- **Keep user informed** — brief status updates at each phase
- **Absolute paths always** — `read_file` and `write_file` require absolute paths
- **Poll async jobs** — always wait + poll after `execute_command`
- **Handle JSON in content** — when writing JSON files via `write_file`, escape quotes properly in the curl command
