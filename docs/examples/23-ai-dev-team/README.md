# Guide 23 — AI Dev Team: Multi-Agent Software Development

**Difficulty:** Advanced  
**Prerequisite:** [Guide 13 — Auto-Scale Additional Nodes](../13-auto-scale/README.md) · [Guide 18 — Autonomous Dev Workflow](../18-autonomous-dev/README.md)  
**Goal:** Build a full AI software development team where each role (Product Manager, Analyst, Architect, Developer, Tester, Reviewer) runs as a dedicated GNOT node with its own persona, memory, and tools — and the agents collaborate autonomously to deliver working software from a one-line requirement.

---

## Architecture

```
You (one sentence requirement)
        │
        ▼  POST /intent
┌───────────────────────────────────────────────────────┐
│  pm-node  (Project Manager)                           │
│  • Breaks requirement into tasks                      │
│  • Dispatches to agents via POST /action              │
│  • Tracks progress, resolves blockers                 │
│  • Reports back to you when done                      │
└──────┬──────────┬────────────┬───────────┬────────────┘
       │          │            │           │
       ▼          ▼            ▼           ▼
 analyst-node  architect-node  dev-node  test-node
 (Analyst)     (Architect)     (Developer)(Tester)
 • Parses      • Creates       • Writes   • Runs tests
   specs         design doc      code       reports bugs
 • Writes      • Defines       • Commits  • Writes test
   user          API schema      to /tmp    cases
   stories     • Chooses       
                 stack                     
                                     ▼
                                reviewer-node
                                (Code Reviewer)
                                • Reviews code
                                • Approves/requests changes
```

Every agent node:
- Has its own **`skills.md`** that defines its role, persona, and decision rules
- Uses the **`llm_chat`** seed action to think and produce structured output
- Uses **`write_file` / `read_file` / `execute_command`** to act on the filesystem
- Communicates with other agents by writing artifacts to a **shared workspace** (`/tmp/devteam/<project>/`)

---

## Shared Workspace Convention

All agents read and write to a project directory on the gateway node (deb-0):

```
/tmp/devteam/<project-id>/
├── requirement.txt       ← PM writes: raw requirement from user
├── user-stories.md       ← Analyst writes
├── design.md             ← Architect writes: tech stack, API schema, DB schema
├── tasks.json            ← PM writes: task list with assignments
├── src/                  ← Developer writes: all source code
│   ├── main.py / app.js
│   └── ...
├── tests/                ← Tester writes: test files
│   └── test_main.py
├── test-report.txt       ← Tester writes: pytest/jest output
├── review.md             ← Reviewer writes: code review comments
└── status.json           ← PM maintains: current phase, blockers, completion
```

---

## Part 1 — Bootstrap All Agent Nodes

Each agent node is bootstrapped via `POST /bootstrap` with a role-specific `skills.md`.

### Bootstrap Script

Save as `/tmp/bootstrap-devteam.py` on deb-0 and run it:

```python
#!/usr/bin/env python3
"""
Bootstrap the AI Dev Team — 6 agent nodes on the same machine,
each with a distinct persona via skills.md.
"""

import httpx
import json
import time

GATEWAY = "http://localhost:8080"
TOKEN   = "change-this-to-a-strong-secret"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# ── Agent definitions ────────────────────────────────────────────────────────

AGENTS = [
    {
        "node_id":    "pm-node",
        "port":       8090,
        "auth_token": "pm-secret",
        "skills_md": """# pm-node — Project Manager Agent

## Role
You are a senior software project manager. You receive requirements from users,
coordinate the dev team, and deliver working software.

## Responsibilities
- Break down requirements into concrete, testable tasks
- Assign tasks to the right agents (analyst, architect, developer, tester, reviewer)
- Track progress by reading status.json in the workspace
- Unblock agents when they report issues
- Report completion to the user with a summary

## Workspace
All agents share /tmp/devteam/<project_id>/
Always read existing files before writing to avoid conflicts.

## Output Format
When creating tasks.json, use this schema:
{
  "project_id": "...",
  "requirement": "...",
  "tasks": [
    {"id": "T1", "agent": "analyst-node", "action": "...", "status": "pending"},
    ...
  ]
}

## Decision Rules
- Always run tasks in order: analyst → architect → developer → tester → reviewer
- If a task fails, read the error and either fix it yourself or re-assign
- Only report "done" when test-report.txt shows all tests passing
- Maximum 3 retry attempts per task before escalating to the user
""",
    },
    {
        "node_id":    "analyst-node",
        "port":       8091,
        "auth_token": "analyst-secret",
        "skills_md": """# analyst-node — Requirements Analyst Agent

## Role
You are a senior business analyst. You translate raw requirements into
clear, structured user stories and acceptance criteria.

## Input
Read /tmp/devteam/<project_id>/requirement.txt

## Output
Write /tmp/devteam/<project_id>/user-stories.md with:
- 3-7 user stories in "As a ... I want ... So that ..." format
- Acceptance criteria for each story (bullet list)
- Out-of-scope items (what this project explicitly does NOT do)
- Key entities/concepts glossary

## Quality Standards
- Each story must have ≥ 2 acceptance criteria
- Stories must be independently testable
- No implementation details — focus on WHAT not HOW
""",
    },
    {
        "node_id":    "architect-node",
        "port":       8092,
        "auth_token": "architect-secret",
        "skills_md": """# architect-node — Software Architect Agent

## Role
You are a senior software architect. You design the technical solution
based on user stories and produce a complete technical design document.

## Input
Read /tmp/devteam/<project_id>/user-stories.md

## Output
Write /tmp/devteam/<project_id>/design.md with:

### 1. Tech Stack
Choose the minimal viable stack. For a Python backend: FastAPI + SQLite.
For a frontend: plain HTML/JS or React. Justify every choice.

### 2. Project Structure
List every file that will be created, e.g.:
- src/main.py        — FastAPI app entry point
- src/models.py      — Pydantic models
- src/db.py          — SQLite helper
- tests/test_api.py  — pytest tests

### 3. API Design
For each endpoint: method, path, request body, response schema, status codes.

### 4. Data Model
Table definitions with column names, types, constraints.

### 5. Implementation Notes
Key patterns, third-party libraries needed, any gotchas.
""",
    },
    {
        "node_id":    "dev-node",
        "port":       8093,
        "auth_token": "dev-secret",
        "pip_packages": ["fastapi", "uvicorn", "httpx", "pytest", "pytest-asyncio"],
        "skills_md": """# dev-node — Software Developer Agent

## Role
You are a senior software engineer. You write clean, production-quality code
that implements the technical design exactly.

## Input
Read /tmp/devteam/<project_id>/design.md for the full spec.
Read /tmp/devteam/<project_id>/user-stories.md for acceptance criteria.

## Output
Write every file listed in design.md under /tmp/devteam/<project_id>/src/
Write every test file under /tmp/devteam/<project_id>/tests/

## Code Standards
- Python: type hints everywhere, docstrings on public functions, no bare except
- Keep functions under 30 lines; extract helpers freely
- Write code that passes the tests the Tester will write
- Include a requirements.txt listing all pip dependencies
- Include a README.md with: how to install, how to run, how to test

## Definition of Done
All files listed in design.md exist and are syntactically valid Python.
""",
    },
    {
        "node_id":    "test-node",
        "port":       8094,
        "auth_token": "test-secret",
        "pip_packages": ["pytest", "pytest-asyncio", "httpx", "fastapi"],
        "skills_md": """# test-node — QA Engineer Agent

## Role
You are a senior QA engineer. You write comprehensive tests and run them.

## Input
Read /tmp/devteam/<project_id>/design.md for the API spec.
Read /tmp/devteam/<project_id>/user-stories.md for acceptance criteria.
Read all files in /tmp/devteam/<project_id>/src/ to understand the implementation.

## Output
1. Write test files to /tmp/devteam/<project_id>/tests/
2. Run: cd /tmp/devteam/<project_id> && pip install -r src/requirements.txt -q && pytest tests/ -v
3. Write the full pytest output to /tmp/devteam/<project_id>/test-report.txt
4. Write a summary: PASS/FAIL, count, any errors

## Test Coverage Requirements
- Happy path for every API endpoint
- Edge cases: empty inputs, invalid data, missing fields
- Error cases: 404, 422, 500 responses
- At least 5 test functions minimum

## If Tests Fail
Write specific failure details to test-report.txt so the Developer can fix them.
Do NOT modify source code — only test files.
""",
    },
    {
        "node_id":    "reviewer-node",
        "port":       8095,
        "auth_token": "reviewer-secret",
        "skills_md": """# reviewer-node — Code Reviewer Agent

## Role
You are a principal engineer doing code review. You catch bugs, security issues,
and style problems before code ships.

## Input
Read all files in /tmp/devteam/<project_id>/src/
Read /tmp/devteam/<project_id>/test-report.txt
Read /tmp/devteam/<project_id>/design.md

## Output
Write /tmp/devteam/<project_id>/review.md with:

### Summary
APPROVED / CHANGES REQUESTED

### Critical Issues (must fix before merge)
List any bugs, security vulnerabilities, data loss risks.

### Suggestions (nice to have)
Style, performance, maintainability improvements.

### Compliance Check
- [ ] All endpoints from design.md are implemented
- [ ] Error handling present on all I/O operations
- [ ] No hardcoded secrets or credentials
- [ ] README is complete and accurate
- [ ] All tests pass (from test-report.txt)

## Decision
If no Critical Issues: write "APPROVED".
If Critical Issues exist: write "CHANGES REQUESTED" with specific line-level fixes.
""",
    },
]


def bootstrap_agent(agent: dict) -> dict:
    """Bootstrap a single agent node via POST /bootstrap."""
    payload = {
        "node_id":      agent["node_id"],
        "port":         agent["port"],
        "auth_token":   agent["auth_token"],
        "actions":      [],   # only seed actions needed
        "pip_packages": agent.get("pip_packages", []),
        "skills_md":    agent["skills_md"],
        "extra_nodes":  {"deb-0": "http://127.0.0.1:8080"},
    }

    print(f"  Bootstrapping {agent['node_id']} on :{agent['port']} ...", end=" ", flush=True)
    r = httpx.post(
        f"{GATEWAY}/bootstrap",
        headers=HEADERS,
        json=payload,
        timeout=180,   # pip installs can take time
    )
    result = r.json()
    status = result.get("status", "unknown")
    print(status)
    if status != "completed":
        print(f"    Error: {result.get('error', result)}")
    return result


if __name__ == "__main__":
    print("=== Bootstrapping AI Dev Team ===\n")
    results = {}
    for agent in AGENTS:
        results[agent["node_id"]] = bootstrap_agent(agent)
        time.sleep(2)  # give each node time to start

    print("\n=== Results ===")
    for node_id, result in results.items():
        status = result.get("status")
        icon = "✅" if status == "completed" else "❌"
        print(f"{icon}  {node_id}: {status}")

    print("\nCheck capabilities:")
    print("  curl -s http://localhost:8080/capabilities -H 'Authorization: Bearer $TOKEN' | python3 -m json.tool")
```

Run it:

```bash
cd /path/to/gnot
python3 /tmp/bootstrap-devteam.py
```

Expected output:

```
=== Bootstrapping AI Dev Team ===

  Bootstrapping pm-node on :8090 ... completed
  Bootstrapping analyst-node on :8091 ... completed
  Bootstrapping architect-node on :8092 ... completed
  Bootstrapping dev-node on :8093 ... completed
  Bootstrapping test-node on :8094 ... completed
  Bootstrapping reviewer-node on :8095 ... completed

=== Results ===
✅  pm-node: completed
✅  analyst-node: completed
✅  architect-node: completed
✅  dev-node: completed
✅  test-node: completed
✅  reviewer-node: completed
```

---

## Part 2 — The PM Orchestrator Action

The PM node needs a custom action that wires the full pipeline together. This action is the "brain" — it reads the `skills.md` of each agent and uses `llm_chat` to invoke each role in sequence, passing outputs between them.

### `run_devteam.py` (on pm-node)

```python
# actions/run_devteam.py  — deployed to pm-node via bootstrap or manual install
"""
Orchestrate the full AI dev team pipeline for a given requirement.
Each agent is invoked via llm_chat with its skills.md as the system prompt,
and writes its output to the shared workspace.
"""

import asyncio
import httpx
import json
import os
import time
import uuid

ASYNC = True

GATEWAY   = "http://127.0.0.1:8080"
WORKSPACE = "/tmp/devteam"

AGENT_PORTS = {
    "analyst":   8091,
    "architect": 8092,
    "developer": 8093,
    "tester":    8094,
    "reviewer":  8095,
}

AGENT_TOKENS = {
    "analyst":   "analyst-secret",
    "architect": "architect-secret",
    "developer": "dev-secret",
    "tester":    "test-secret",
    "reviewer":  "reviewer-secret",
}


async def call_agent(agent: str, prompt: str, timeout: int = 300) -> str:
    """Call a specific agent node's /intent endpoint."""
    port  = AGENT_PORTS[agent]
    token = AGENT_TOKENS[agent]
    url   = f"http://127.0.0.1:{port}/intent"

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"prompt": prompt, "session_id": f"devteam-{agent}"},
        )
        r.raise_for_status()
        return r.json().get("response", "")


def read_workspace(project_id: str, filename: str) -> str:
    path = os.path.join(WORKSPACE, project_id, filename)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""


def write_status(project_id: str, phase: str, completed: list, current: str):
    path = os.path.join(WORKSPACE, project_id, "status.json")
    with open(path, "w") as f:
        json.dump({
            "phase": phase,
            "completed_phases": completed,
            "current": current,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, f, indent=2)


async def run(params: dict, context: dict) -> dict:
    requirement = params["requirement"]
    project_id  = params.get("project_id") or f"proj-{uuid.uuid4().hex[:8]}"
    max_dev_retries = params.get("max_dev_retries", 2)

    # Create workspace
    workspace = os.path.join(WORKSPACE, project_id)
    os.makedirs(workspace, exist_ok=True)

    # Write raw requirement
    with open(os.path.join(workspace, "requirement.txt"), "w") as f:
        f.write(requirement)

    log = []
    completed = []

    def phase_log(phase: str, msg: str):
        entry = f"[{phase.upper()}] {msg}"
        log.append(entry)
        print(entry)

    # ── Phase 1: Analysis ──────────────────────────────────────────────────
    write_status(project_id, "analysis", completed, "analyst")
    phase_log("analysis", "Analyst reading requirement...")

    await call_agent("analyst",
        f"The project workspace is at {workspace}. "
        f"Read requirement.txt and write user-stories.md as per your skills."
    )
    completed.append("analysis")
    phase_log("analysis", f"✅ user-stories.md written")

    # ── Phase 2: Architecture ──────────────────────────────────────────────
    write_status(project_id, "architecture", completed, "architect")
    phase_log("architecture", "Architect reading user stories...")

    await call_agent("architect",
        f"The project workspace is at {workspace}. "
        f"Read user-stories.md and write design.md as per your skills."
    )
    completed.append("architecture")
    phase_log("architecture", "✅ design.md written")

    # ── Phase 3: Development (with retry) ─────────────────────────────────
    write_status(project_id, "development", completed, "developer")
    dev_attempt = 0
    test_passed = False

    while dev_attempt <= max_dev_retries and not test_passed:
        dev_attempt += 1
        phase_log("development", f"Developer writing code (attempt {dev_attempt})...")

        if dev_attempt == 1:
            dev_prompt = (
                f"The project workspace is at {workspace}. "
                f"Read design.md and user-stories.md. "
                f"Implement all files listed in the design under {workspace}/src/ "
                f"and write {workspace}/tests/ as per your skills."
            )
        else:
            # Pass test failures back to developer
            report = read_workspace(project_id, "test-report.txt")
            dev_prompt = (
                f"The project workspace is at {workspace}. "
                f"The tests failed. Here is the test report:\n\n{report}\n\n"
                f"Fix the bugs in {workspace}/src/ without modifying the test files."
            )

        await call_agent("developer", dev_prompt, timeout=600)
        completed_dev = completed + ["development"]
        phase_log("development", f"✅ Code written (attempt {dev_attempt})")

        # ── Phase 4: Testing ───────────────────────────────────────────────
        write_status(project_id, "testing", completed_dev, "tester")
        phase_log("testing", "Tester running test suite...")

        await call_agent("tester",
            f"The project workspace is at {workspace}. "
            f"Read design.md and src/ files, write comprehensive tests to tests/, "
            f"run them with pytest, and write the full output to test-report.txt."
        )

        report = read_workspace(project_id, "test-report.txt")
        test_passed = "passed" in report.lower() and "failed" not in report.lower().split("passed")[0]

        if test_passed:
            phase_log("testing", "✅ All tests passed")
        else:
            phase_log("testing", f"❌ Tests failed (attempt {dev_attempt}/{max_dev_retries+1})")

    completed.append("development")
    completed.append("testing")

    # ── Phase 5: Code Review ───────────────────────────────────────────────
    write_status(project_id, "review", completed, "reviewer")
    phase_log("review", "Reviewer inspecting code...")

    await call_agent("reviewer",
        f"The project workspace is at {workspace}. "
        f"Read all source files, test-report.txt, and design.md. "
        f"Write review.md as per your skills."
    )

    review = read_workspace(project_id, "review.md")
    approved = "APPROVED" in review
    completed.append("review")
    phase_log("review", "✅ APPROVED" if approved else "⚠️  CHANGES REQUESTED")

    # ── Final status ───────────────────────────────────────────────────────
    write_status(project_id, "complete", completed, "done")

    # Read final artifacts for summary
    stories  = read_workspace(project_id, "user-stories.md")
    design   = read_workspace(project_id, "design.md")
    report   = read_workspace(project_id, "test-report.txt")

    # Count source files
    src_dir = os.path.join(workspace, "src")
    src_files = []
    if os.path.exists(src_dir):
        for root, _, files in os.walk(src_dir):
            for f in files:
                src_files.append(os.path.relpath(os.path.join(root, f), workspace))

    return {
        "project_id":     project_id,
        "workspace":      workspace,
        "phases_completed": completed,
        "source_files":   src_files,
        "tests_passed":   test_passed,
        "review_approved": approved,
        "dev_attempts":   dev_attempt,
        "log":            log,
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "run_devteam",
  "description": "Run the full AI dev team pipeline. PM coordinates analyst, architect, developer, tester, and reviewer to deliver working software from a single requirement string.",
  "type": "object",
  "properties": {
    "requirement": {
      "type": "string",
      "description": "What to build — one or more sentences describing the desired software",
      "minLength": 10
    },
    "project_id": {
      "type": "string",
      "description": "Optional project identifier (auto-generated if omitted)"
    },
    "max_dev_retries": {
      "type": "integer",
      "description": "Max times developer retries after test failures",
      "default": 2,
      "minimum": 0,
      "maximum": 5
    }
  },
  "required": ["requirement"],
  "additionalProperties": false
}
```

Deploy this action to pm-node:

```bash
# Copy to pm-node's actions directory (created during bootstrap)
mkdir -p ~/.gnot-nodes/pm-node/actions
cp run_devteam.py ~/.gnot-nodes/pm-node/actions/
cp run_devteam.schema.json ~/.gnot-nodes/pm-node/actions/

# Restart pm-node to load the new action
kill $(cat ~/.gnot-nodes/pm-node/node.pid 2>/dev/null) 2>/dev/null
nohup python3 /path/to/gnot/gnot/src/node_runtime.py \
    --config ~/.gnot-nodes/pm-node/node.yaml \
    > ~/.gnot-nodes/pm-node/node.log 2>&1 &
```

---

## Part 3 — Run the Team

### Via curl (call pm-node directly):

```bash
curl -s -X POST http://localhost:8090/action \
  -H "Authorization: Bearer pm-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "pm-node",
    "payload": {
      "action": "run_devteam",
      "params": {
        "requirement": "Build a REST API for a simple todo list. Users can create, list, update, and delete todos. Each todo has a title, optional description, and a done flag. No authentication needed. Use Python with FastAPI and SQLite."
      }
    }
  }' | python3 -m json.tool
```

### Via Claude Web (human-in-the-loop, recommended for first run):

Open Claude Web with your GNOT project context and type:

```
I want to build a REST API for a simple todo list app.
Users can create, list, update, and delete todos.
Each todo has a title, description, and a done flag.
Use Python + FastAPI + SQLite. No authentication needed.

Please kick off the AI dev team by calling run_devteam on pm-node.
Show me the output of each phase as it completes.
```

Claude will:
1. Call `run_devteam` on pm-node
2. Show you the `log` field (phase-by-phase progress)
3. Read the final artifacts from the workspace and summarize

### Via single /intent (fully autonomous):

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Use the AI dev team to build: a REST API for a URL shortener. Users POST a long URL and get a short code back. GET /s/{code} redirects to the original URL. Track visit counts. Use Python + FastAPI + SQLite.",
    "session_id": "devteam-run"
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print(r['response'])"
```

---

## Part 4 — Reading the Outputs

After a run, explore the workspace:

```bash
PROJECT_ID="proj-abc12345"   # from the run output
WORKSPACE="/tmp/devteam/$PROJECT_ID"

# Phase outputs
echo "=== USER STORIES ===" && cat $WORKSPACE/user-stories.md
echo "=== DESIGN ===" && cat $WORKSPACE/design.md
echo "=== TEST REPORT ===" && cat $WORKSPACE/test-report.txt
echo "=== CODE REVIEW ===" && cat $WORKSPACE/review.md
echo "=== SOURCE FILES ===" && find $WORKSPACE/src -type f

# Run the code yourself
cd $WORKSPACE
pip install -r src/requirements.txt -q
uvicorn src.main:app --reload --port 9999
# → open http://localhost:9999/docs for Swagger UI
```

Or ask Claude Web to read and summarize:

```
Read all the artifacts in /tmp/devteam/<project_id>/ on deb-0 and give me:
1. A summary of what was built
2. The full list of API endpoints
3. The test pass rate
4. Any review comments I should act on
```

---

## Part 5 — Customizing Agent Personas

Each agent's behavior is entirely defined by its `skills.md`. To change a persona, update the file and restart the node:

```bash
# Give the developer a different coding style
cat >> ~/.gnot-nodes/dev-node/skills.md << 'EOF'

## Additional Rules (added by team lead)
- Always use async/await for all database operations
- Use Pydantic v2 model_validator instead of validator
- Prefer dataclasses over plain dicts for internal structures
EOF

# Restart dev-node to pick up changes
python3 gnot/src/mesh_ctl.py run deb-0 execute_command \
  '{"command": "kill $(cat ~/.gnot-nodes/dev-node/node.pid) && sleep 1 && nohup python3 /path/to/gnot/gnot/src/node_runtime.py --config ~/.gnot-nodes/dev-node/node.yaml > ~/.gnot-nodes/dev-node/node.log 2>&1 & echo $! > ~/.gnot-nodes/dev-node/node.pid"}'
```

---

## Phase Flow Summary

```
User requirement
      │
      ▼
 [analyst-node]  → user-stories.md
      │
      ▼
 [architect-node] → design.md
      │
      ▼
 [dev-node]  ←──────────────────────────┐
      │                                  │  (test failure → retry)
      ▼                                  │
 [test-node] → test-report.txt ─── FAIL ┘
      │
    PASS
      │
      ▼
 [reviewer-node] → review.md
      │
   APPROVED
      │
      ▼
 Final result → project_id, workspace, source_files, test_passed
```

Each retry loop passes the actual pytest output back to the developer — not a summary, but the raw failure messages — so the LLM can fix the exact issue.

---

## Example Output

Running the todo API requirement takes ~3–5 minutes end-to-end. The result:

```json
{
  "project_id": "proj-a1b2c3d4",
  "workspace": "/tmp/devteam/proj-a1b2c3d4",
  "phases_completed": ["analysis", "architecture", "development", "testing", "review"],
  "source_files": [
    "src/main.py",
    "src/models.py",
    "src/database.py",
    "src/requirements.txt",
    "tests/test_todos.py",
    "README.md"
  ],
  "tests_passed": true,
  "review_approved": true,
  "dev_attempts": 1,
  "log": [
    "[ANALYSIS] Analyst reading requirement...",
    "[ANALYSIS] ✅ user-stories.md written",
    "[ARCHITECTURE] Architect reading user stories...",
    "[ARCHITECTURE] ✅ design.md written",
    "[DEVELOPMENT] Developer writing code (attempt 1)...",
    "[DEVELOPMENT] ✅ Code written (attempt 1)",
    "[TESTING] Tester running test suite...",
    "[TESTING] ✅ All tests passed",
    "[REVIEW] Reviewer inspecting code...",
    "[REVIEW] ✅ APPROVED"
  ]
}
```

---

## Summary

You have built a complete AI dev team:

| Node | Role | Key Actions Used |
|------|------|-----------------|
| `pm-node` | Project Manager | `run_devteam` (orchestrates), `llm_chat` |
| `analyst-node` | Requirements Analyst | `llm_chat` → `write_file` |
| `architect-node` | Software Architect | `read_file` → `llm_chat` → `write_file` |
| `dev-node` | Software Developer | `read_file` → `llm_chat` → `write_file` |
| `test-node` | QA Engineer | `write_file` → `execute_command` (pytest) → `write_file` |
| `reviewer-node` | Code Reviewer | `read_file` → `llm_chat` → `write_file` |

The architecture is fully extensible:
- Add a **`devops-node`** that reads the design and writes a `Dockerfile` + `docker-compose.yml`
- Add a **`docs-node`** that generates API documentation from the source code
- Add a **`security-node`** that scans code for vulnerabilities before reviewer approval
- Point each agent at a **different LLM** (e.g., dev-node uses Claude Opus, others use Sonnet)

**Back to:** [Examples Index](../README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
