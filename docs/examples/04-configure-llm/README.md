# Guide 04 — Configure a Node-Local LLM

**Difficulty:** Beginner  
**Prerequisite:** [Guide 03 — Call an Action from Claude Web](../03-claude-web-action/README.md)  
**Goal:** Add an LLM provider to deb-0 so it can process free-text prompts autonomously via `POST /intent`, without needing a human to relay commands.

---

## Two Interaction Patterns

GNOT supports two ways to use LLMs:

| Pattern | How it works | When to use |
|---------|-------------|-------------|
| **A — External LLM** (Guide 03) | Claude Web reasons and tells you what to run | Interactive sessions, complex multi-step workflows, best model quality |
| **B — Node-local LLM** (this guide) | deb-0 has its own LLM; callers POST a prompt and get a result | Automation, bots, CI/CD pipelines, any scenario where a human isn't in the loop |

Both patterns use the same node — you are just activating `POST /intent` on deb-0 by adding an `llm_provider` block to the config.

---

## Step 1 — Get a Claude API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an API key (or use an existing one)
3. Note it down — you will add it to `node.yaml`

> **Other providers:** GNOT uses the OpenAI-compatible API format. Any provider with an `/v1/chat/completions` endpoint works: OpenAI, Groq, local Ollama, etc. The Claude-specific configuration is shown below; adapt accordingly.

---

## Step 2 — Update `node.yaml`

Open `~/gnot-nodes/deb-0/node.yaml` and add the LLM section:

```yaml
# ~/gnot-nodes/deb-0/node.yaml

# ── Identity ──────────────────────────────────────────────
node_id: deb-0
listen:  0.0.0.0:8080

# ── Actions ───────────────────────────────────────────────
actions_dir: /path/to/gnot/gnot/src/seed/actions

# ── Authentication ─────────────────────────────────────────
auth_token: change-this-to-a-strong-secret

# ── Gateway mode ──────────────────────────────────────────
trusted_nodes: []

# ── LLM provider (activates POST /intent) ─────────────────
llm_api_key:       "sk-ant-your-key-here"
llm_base_url:      "https://api.anthropic.com/v1"
llm_default_model: "claude-sonnet-4-20250514"
llm_timeout_seconds: 120
llm_extra_headers:
  anthropic-version: "2023-06-01"

# ── Intent / Session settings ──────────────────────────────
intent_max_turns: 20          # max LLM reasoning steps per /intent call
session_ttl_seconds: 3600     # session memory expires after 1 hour

# ── Credential store (optional but recommended) ────────────
# Stores caller-supplied credentials encrypted at rest (AES-256-GCM)
# credential_encryption_key: "a-separate-stable-secret"

# ── Job management ─────────────────────────────────────────
job_ttl_seconds: 3600
cleanup_interval_seconds: 60
```

> **Keep secrets out of version control.** If you commit `node.yaml`, use environment variable substitution or a secrets manager. At minimum, add `node.yaml` to `.gitignore`.

---

## Step 3 — Restart deb-0

```bash
# If running in background, stop it first
kill $(cat ~/gnot-nodes/deb-0/node.pid) 2>/dev/null

# Restart
cd /path/to/gnot
nohup python3 gnot/src/node_runtime.py --config ~/gnot-nodes/deb-0/node.yaml \
    > ~/gnot-nodes/deb-0/node.log 2>&1 &
echo $! > ~/gnot-nodes/deb-0/node.pid
```

Check the startup log to confirm the LLM client initialized:

```bash
tail -20 ~/gnot-nodes/deb-0/node.log
```

Look for a line like:

```
INFO  LLM client initialized: model=claude-sonnet-4-20250514 base_url=https://api.anthropic.com/v1
INFO  IntentHandler ready
```

---

## Step 4 — Verify `POST /intent` is Active

```bash
export NODE_URL="http://localhost:8080"
export TOKEN="change-this-to-a-strong-secret"

curl -s "$NODE_URL/capabilities" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('intent_enabled:', d.get('intent_enabled', False))"
```

Expected: `intent_enabled: True`

---

## Step 5 — Send Your First Intent

```bash
curl -s -X POST "$NODE_URL/intent" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "What is the hostname and current uptime of this machine?",
    "session_id": "my-first-session"
  }' | python3 -m json.tool
```

Expected response:

```json
{
    "session_id": "my-first-session",
    "response": "The hostname of this machine is **deb-0** and it has been running for 2 days, 14 hours and 23 minutes.",
    "turns": 2,
    "actions_called": ["execute_command"]
}
```

What happened internally:
1. deb-0 received the prompt
2. Its LLM (Claude) reasoned that it needs to run `hostname` and `uptime`
3. It called `execute_command` twice via the ReAct loop
4. It synthesized the results into a natural-language answer

---

## How the ReAct Loop Works

`POST /intent` runs a **ReAct (Reason + Act) agent loop**:

```
Prompt received
      │
      ▼
LLM reasons → decides to call an action
      │
      ▼
Action executes on deb-0
      │
      ▼
LLM reads result → reasons → calls another action (or finishes)
      │
      ▼
Final natural-language response returned
```

The loop runs up to `intent_max_turns` times (default: 20). If the LLM has enough information earlier, it stops and responds.

---

## Configuration Reference

### Using OpenAI

```yaml
llm_api_key:       "sk-your-openai-key"
llm_base_url:      "https://api.openai.com/v1"
llm_default_model: "gpt-4o"
```

### Using Groq (fast inference)

```yaml
llm_api_key:       "gsk_your-groq-key"
llm_base_url:      "https://api.groq.com/openai/v1"
llm_default_model: "llama-3.3-70b-versatile"
```

### Using a Local Model (Ollama)

```bash
# First start Ollama on your machine
ollama serve
ollama pull llama3.2
```

```yaml
llm_api_key:       "ollama"   # any non-empty string
llm_base_url:      "http://localhost:11434/v1"
llm_default_model: "llama3.2"
```

---

## Session Memory

The `session_id` parameter maintains conversation history across multiple `/intent` calls:

```bash
# First call — introduce context
curl -s -X POST "$NODE_URL/intent" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "My project files are in /opt/myapp. Keep that in mind.",
    "session_id": "work-session-1"
  }'

# Second call — reference previous context
curl -s -X POST "$NODE_URL/intent" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "List the Python files in my project directory.",
    "session_id": "work-session-1"
  }'
```

The second call remembers `/opt/myapp` from the first — no need to repeat it.

Sessions expire after `session_ttl_seconds` (default: 1 hour) of inactivity.

---

## Troubleshooting

**`intent_enabled: False` in capabilities:**  
The LLM config was not loaded. Check `node.log` for errors like `LLM client initialization failed`.

**`AuthenticationError` in node.log:**  
Your `llm_api_key` is wrong or expired. Double-check it in the Anthropic console.

**Response is slow (30+ seconds):**  
This is normal for multi-step tasks — the LLM may call several actions before responding. For faster responses on simple tasks, try `gpt-4o-mini` or Groq.

**`intent_max_turns exceeded`:**  
The task was too complex to complete in 20 LLM turns. Increase `intent_max_turns` in `node.yaml` or break the task into smaller prompts.

---

## Summary

deb-0 can now:
- ✅ Accept free-text prompts via `POST /intent`
- ✅ Autonomously reason about and execute actions
- ✅ Maintain session memory across multiple calls
- ✅ Return natural-language responses

**Next:** [Guide 05 — Send a Prompt via /intent with curl](../05-curl-intent/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
