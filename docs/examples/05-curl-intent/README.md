# Guide 05 — Send a Prompt via /intent

**Difficulty:** Beginner  
**Prerequisite:** [Guide 04 — Configure a Node-Local LLM](../04-configure-llm/README.md)  
**Goal:** Use `POST /intent` with progressively complex prompts — from simple queries to multi-step autonomous workflows.

---

## The `/intent` Endpoint

```
POST /intent
Authorization: Bearer <token>
Content-Type: application/json

{
  "prompt":     "Your natural language instruction",
  "session_id": "optional-session-identifier"
}
```

Response:

```json
{
  "session_id": "...",
  "response":   "Natural language answer",
  "turns":      3,
  "actions_called": ["execute_command", "read_file"]
}
```

Set your variables:

```bash
export NODE_URL="http://localhost:8080"
export TOKEN="change-this-to-a-strong-secret"
export SID="session-$(date +%s)"   # unique session per shell
```

---

## Example 1 — Simple Query

```bash
curl -s -X POST "$NODE_URL/intent" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"prompt\": \"What is the current date, hostname, and kernel version?\",
    \"session_id\": \"$SID\"
  }" | python3 -m json.tool
```

The LLM will call `execute_command` with `date`, `hostname`, and `uname -r`, then synthesize the results.

---

## Example 2 — Multi-Step Workflow

A single prompt that requires several sequential actions:

```bash
curl -s -X POST "$NODE_URL/intent" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"prompt\": \"Check disk usage on /. If usage is above 80%, find the top 5 largest directories under /var and report them.\",
    \"session_id\": \"$SID\"
  }" | python3 -m json.tool
```

Internal steps the LLM will take:
1. `execute_command`: `df -h /`
2. Parse the output — if >80%, continue
3. `execute_command`: `du -sh /var/* | sort -rh | head -5`
4. Synthesize a report

---

## Example 3 — File Creation Workflow

```bash
curl -s -X POST "$NODE_URL/intent" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"prompt\": \"Create a file at /tmp/system-report.txt containing: hostname, OS version, total RAM, and number of CPUs. Format it nicely.\",
    \"session_id\": \"$SID\"
  }" | python3 -m json.tool
```

The LLM will gather data, format it, and call `write_file` to write it.

Verify:

```bash
curl -s -X POST "$NODE_URL/intent" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"prompt\": \"Read the file /tmp/system-report.txt and show me its contents.\",
    \"session_id\": \"$SID\"
  }" | python3 -m json.tool
```

---

## Example 4 — Session Memory in Action

This example shows how `session_id` lets you build on previous turns:

```bash
# Turn 1: establish context
curl -s -X POST "$NODE_URL/intent" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"prompt\": \"Scan /var/log for .log files larger than 1MB and remember that list.\",
    \"session_id\": \"log-audit\"
  }" | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"

# Turn 2: act on previous context — no need to repeat the list
curl -s -X POST "$NODE_URL/intent" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"prompt\": \"For each file in that list, show me the last 5 lines.\",
    \"session_id\": \"log-audit\"
  }" | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

---

## Example 5 — Scripting with /intent

`/intent` can be called from any script or application. Here is a Python example:

```python
#!/usr/bin/env python3
import httpx, json, os

NODE_URL = os.environ["NODE_URL"]
TOKEN    = os.environ["TOKEN"]

def ask(prompt: str, session_id: str = "default") -> str:
    r = httpx.post(
        f"{NODE_URL}/intent",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"prompt": prompt, "session_id": session_id},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["response"]

# Usage
print(ask("How many Python processes are currently running?"))
print(ask("What are their PIDs?", session_id="proc-check"))
```

Save as `/tmp/ask_gnot.py` and run:

```bash
python3 /tmp/ask_gnot.py
```

---

## Example 6 — Cron-style Automation

`/intent` pairs naturally with cron for scheduled tasks:

```bash
# Add to crontab: crontab -e
# Run a daily disk-space report and save it
0 8 * * * curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Generate a disk usage summary for / and /home. Save it to /var/log/disk-report.txt with today'\''s date prepended.", "session_id": "daily-report"}' \
  >> /var/log/gnot-cron.log 2>&1
```

---

## Understanding the Response Fields

| Field | Description |
|-------|-------------|
| `session_id` | The session identifier (echoed back) |
| `response` | Natural-language answer from the LLM |
| `turns` | Number of LLM reasoning steps taken |
| `actions_called` | List of actions the LLM invoked |

If `turns` is high (>10) for a simple query, your prompt may be ambiguous. Be more specific.

---

## Prompt Tips

**Be specific about the output format:**
```
"Show disk usage on /. Return just the percentage used as a number, nothing else."
```

**Specify constraints:**
```
"Check if nginx is running. Do NOT start it — just report its status."
```

**Chain reasoning explicitly:**
```
"First check if /opt/app exists. If it does, read /opt/app/version.txt. If it doesn't, report that the app is not installed."
```

**Use conditional logic:**
```
"If free memory is below 500MB, list the top 3 memory-consuming processes. Otherwise just report free memory."
```

---

## Troubleshooting

**Response takes >60 seconds:** Normal for complex multi-step prompts. The default `llm_timeout_seconds: 120` allows up to 2 minutes. Increase if needed.

**`"turns": 20` and incomplete response:** The task exceeded `intent_max_turns`. Break it into smaller prompts or increase the limit in `node.yaml`.

**LLM calls the wrong action:** Add more context to your prompt: *"Use execute_command to run..."*

**Session expired:** After `session_ttl_seconds` of inactivity, history is lost. Start a new session or increase the TTL.

---

## Summary

You can now:
- ✅ Send natural-language prompts to deb-0 via `POST /intent`
- ✅ Chain multi-step workflows in a single prompt
- ✅ Use session memory to build on previous turns
- ✅ Call `/intent` from scripts, cron, bots — any HTTP client

**Next:** [Guide 06 — Create deb-1 with a Custom "hello" Action](../06-create-deb1-hello-action/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
