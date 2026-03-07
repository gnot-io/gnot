# Guide 18 — Autonomous Development Workflow

**Difficulty:** Advanced  
**Prerequisite:** [Guide 17 — Telegram Bot + CRM Integration](../17-telegram-crm/README.md)  
**Goal:** Use the GNOT mesh as an autonomous software development agent — the LLM reads source code, writes fixes, runs tests, reads failures, revises, and loops until all tests pass. Zero human intervention within the turn sequence.

---

## How It Works

This is Pattern A (External LLM) at its most powerful. The LLM has access to three seed actions on the target node:

| Action | Used for |
|--------|---------|
| `read_file` | Inspect source code, test files, logs |
| `write_file` | Apply patches and fixes |
| `execute_command` | Run tests, linters, build tools |

With these three primitives, the LLM can autonomously implement features, fix bugs, and refactor code on any node in the mesh — without any additional tooling.

---

## Part 1 — Prepare a Sample Project

Let's set up a small Python project with a bug for the LLM to fix.

```bash
# Create the project on deb-1
python3 gnot/src/mesh_ctl.py run deb-1 execute_command \
  '{"command": "mkdir -p /opt/dev-demo && cat > /opt/dev-demo/calculator.py << '"'"'EOF\ndef add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n\ndef multiply(a, b):\n    return a * b\n\ndef divide(a, b):\n    # BUG: no zero-division check\n    return a / b\n\ndef power(base, exp):\n    # BUG: wrong implementation\n    return base * exp\nEOF"}'
```

```bash
# Create failing tests
python3 gnot/src/mesh_ctl.py run deb-1 execute_command \
  '{"command": "cat > /opt/dev-demo/test_calculator.py << '"'"'EOF\nimport pytest\nfrom calculator import add, subtract, multiply, divide, power\n\ndef test_add():        assert add(2, 3) == 5\ndef test_subtract():   assert subtract(10, 3) == 7\ndef test_multiply():   assert multiply(4, 5) == 20\ndef test_divide():     assert divide(10, 2) == 5.0\ndef test_divide_by_zero():\n    with pytest.raises(ZeroDivisionError):\n        divide(5, 0)\ndef test_power():      assert power(2, 10) == 1024\nEOF"}'
```

```bash
# Verify tests fail (expected)
python3 gnot/src/mesh_ctl.py run deb-1 execute_command \
  '{"command": "cd /opt/dev-demo && python3 -m pytest test_calculator.py -v 2>&1"}'
```

---

## Part 2 — Autonomous Fix via Claude Web

Open Claude Web with your GNOT project configured and paste this prompt:

```
I have a Python project on node deb-1 at /opt/dev-demo/.

Please:
1. Read the source file calculator.py
2. Read the test file test_calculator.py
3. Run the tests with pytest and read the failures carefully
4. Fix the bugs in calculator.py
5. Run the tests again to verify
6. Keep fixing and re-running until ALL tests pass
7. Show me a summary of what you changed and why

Work autonomously — don't wait for me between steps.
```

Claude will:
1. `read_file` → `calculator.py` (sees the bugs)
2. `read_file` → `test_calculator.py` (understands expected behavior)
3. `execute_command` → `pytest -v` (sees which tests fail)
4. `write_file` → fixed `calculator.py`
5. `execute_command` → `pytest -v` (verify)
6. Report what it changed

---

## Part 3 — Autonomous Fix via /intent

For fully autonomous operation (no human in the loop):

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "On node deb-1, the project at /opt/dev-demo has failing tests. Read the source code, identify and fix all bugs, run pytest to verify, and loop until all tests pass. Report what you changed.",
    "session_id": "autodev-demo"
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

---

## Part 4 — Feature Implementation Loop

A more ambitious prompt — implement a new feature from a description:

```
On deb-1, in /opt/dev-demo/calculator.py:

1. Add a new function: `factorial(n)` that computes n! recursively
2. Add tests for it in test_calculator.py:
   - factorial(0) == 1
   - factorial(5) == 120
   - factorial(10) == 3628800
   - factorial(-1) raises ValueError
3. Run pytest on all tests
4. Fix anything that fails
5. Run the linter: python3 -m flake8 calculator.py (install flake8 if needed)
6. Fix any lint errors
7. Report the final state of calculator.py
```

The LLM will write code, write tests, run both, fix issues, and run the linter — all autonomously.

---

## Part 5 — Multi-File Project

Scale up to a real project structure:

```
Perform a code review and quality improvement on the project at /opt/myapp on deb-1:

1. List all Python files in the project
2. Read each file
3. Run the full test suite: python3 -m pytest tests/ -v
4. For each failing test:
   a. Read the relevant source file(s)
   b. Understand why the test fails
   c. Fix the source (NOT the tests)
   d. Verify the fix works
5. Run flake8 on all source files
6. Fix any critical lint errors (E-level only, ignore W-level)
7. Generate a final report: what was broken, what was fixed, current test status
```

---

## Part 6 — Deploying to Another Node

After fixing code, deploy it to production on another node:

```
The code at /opt/dev-demo on deb-1 is now passing all tests.
Please:
1. Read the fixed calculator.py from deb-1
2. Write it to /opt/prod-demo/calculator.py on deb-0
3. Run the tests on deb-0 to confirm deployment:
   cd /opt/prod-demo && python3 -m pytest test_calculator.py -v
4. Report the test results
```

The LLM reads from deb-1, writes to deb-0, and verifies — a complete CI/CD loop.

---

## Workflow Patterns

### "Fix until green" loop

```
Read tests → run tests → if failures: read source → fix → run tests → repeat
```

### "TDD" — write tests first, then implementation

```
Read spec → write failing tests → run tests (confirm red) → write implementation → run tests (confirm green)
```

### "Refactor with safety net"

```
Run tests (establish baseline) → refactor source → run tests (verify no regression) → report diff
```

### "Code review + auto-fix"

```
Read source → analyze → identify issues → fix issues → verify with tests → write review report
```

---

## Important Notes

**LLM context window:** Very large projects with many files may exceed the LLM's context window. Break them into smaller scoped tasks or use `/intent` with `session_id` to work incrementally.

**Infinite loop prevention:** `intent_max_turns` (default 20) prevents runaway loops. For complex projects, increase it:
```yaml
intent_max_turns: 40
```

**Test-only changes:** Always instruct the LLM to fix source files, not tests — unless adding new test coverage is the explicit goal.

**Version control:** The LLM will overwrite files directly. Before starting, commit your working state:
```bash
python3 gnot/src/mesh_ctl.py run deb-1 execute_command \
  '{"command": "cd /opt/dev-demo && git init && git add . && git commit -m \"before-autodev\""}'
```

---

## Summary

You have completed all 18 GNOT guides. You can now:

- ✅ Read, write, and execute on any node in the mesh
- ✅ Build custom actions for any capability
- ✅ Orchestrate multi-node workflows from a single prompt
- ✅ Connect nodes across NAT boundaries automatically
- ✅ Scale the mesh programmatically with `POST /bootstrap`
- ✅ Build production applications: content pipelines, bots, CI/CD agents

---

## What's Next?

The three seed actions (`read_file`, `write_file`, `execute_command`) are sufficient to build anything. Some directions to explore:

- **GPU compute nodes** — Add a node with CUDA and an image/video generation action
- **Database nodes** — Custom actions that query PostgreSQL, MongoDB, Redis
- **Monitoring** — A scheduled action that polls `GET /health` on all nodes and alerts on anomalies
- **Multi-region deployment** — Nodes on cloud VMs in different regions, all connected to deb-0
- **MCP bridge** — Expose the mesh as an MCP server so Claude can call it natively via tool use

---

*Part of the [GNOT Examples](../README.md) series.*  
*[← Back to Index](../README.md)*
