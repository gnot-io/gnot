# Guide 03 — Call an Action from Claude Web

**Difficulty:** Beginner  
**Prerequisite:** [Guide 02 — Call an Action via curl](../02-curl-action/README.md)  
**Goal:** Expose deb-0 to the internet via Cloudflare Tunnel, then use Claude Web as the LLM orchestrator to call actions on your node through natural language.

---

## Architecture

```
You (natural language)
       │
       ▼
Claude Web (claude.ai)   ← acts as the LLM orchestrator
       │
       │  POST /action  (HTTPS)
       ▼
Cloudflare Tunnel
       │
       ▼
deb-0  (localhost:8080)
```

Claude Web never runs code itself — it reasons about what to do and gives you `curl` commands (or `mesh_ctl.py` commands) to run on your machine. You paste the output back, and Claude reads the result to decide the next step. This is **Pattern A** — the primary GNOT interaction model.

---

## Part 1 — Expose deb-0 via Cloudflare Tunnel

Worker nodes behind NAT will call home to deb-0 later. deb-0 must be publicly reachable over HTTPS with a stable URL. Cloudflare Tunnel provides this without opening any firewall ports or needing a static IP.

### Step 1.1 — Install `cloudflared`

**Debian/Ubuntu:**

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
  -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb
cloudflared --version
```

**CentOS/RHEL/AlmaLinux:**

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.rpm \
  -o /tmp/cloudflared.rpm
sudo rpm -ivh /tmp/cloudflared.rpm
```

### Step 1.2 — Authenticate with Cloudflare

```bash
cloudflared tunnel login
```

This opens a browser window. Log in with your Cloudflare account and authorize the tunnel for your domain.

> **No domain yet?** You can use the quick-tunnel option (temporary URL, no account needed):
> ```bash
> cloudflared tunnel --url http://localhost:8080
> ```
> This gives a temporary `*.trycloudflare.com` URL — useful for testing but not stable. For production, use a named tunnel with your own domain.

### Step 1.3 — Create a Named Tunnel

```bash
cloudflared tunnel create gnot-deb0
```

Note the **Tunnel ID** from the output (e.g., `3b4a5636-4530-4b5f-8e8b-8ad681dc486e`).

### Step 1.4 — Create a DNS Route

```bash
cloudflared tunnel route dns gnot-deb0 deb0.yourdomain.com
```

Replace `deb0.yourdomain.com` with your subdomain. This creates a CNAME record pointing to the tunnel.

### Step 1.5 — Create the Cloudflared Config

```bash
sudo mkdir -p /etc/cloudflared

sudo tee /etc/cloudflared/config.yml << 'EOF'
tunnel: <TUNNEL_ID>
credentials-file: /etc/cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: deb0.yourdomain.com
    service: http://127.0.0.1:8080
  - service: http_status:404
EOF
```

Replace `<TUNNEL_ID>` and `deb0.yourdomain.com` with your values.

### Step 1.6 — Start the Tunnel as a System Service

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared

# Verify
sudo systemctl status cloudflared
```

### Step 1.7 — Verify Public Access

```bash
# Wait ~30 seconds for DNS to propagate, then:
curl -s https://deb0.yourdomain.com/health
```

Expected:

```json
{"status": "ok", "node_id": "deb-0", "jobs_active": 0, "queue_depth": {}}
```

> **If you used the quick-tunnel** (step 1.2 alternative), your URL looks like:
> `https://random-words.trycloudflare.com`
> Use that URL everywhere below instead of `deb0.yourdomain.com`.

---

## Part 2 — Configure Claude Web as Orchestrator

### Step 2.1 — Get the Capability Description

```bash
export GATEWAY_URL="https://deb0.yourdomain.com"
export TOKEN="change-this-to-a-strong-secret"

curl -s "$GATEWAY_URL/capabilities" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

Copy the entire JSON output — you will paste it into Claude.

### Step 2.2 — Open Claude Web and Create a Project

1. Go to [claude.ai](https://claude.ai) and open a new **Project**
2. In the project's **Instructions** (system prompt), paste the following:

```
You are an orchestrator for a GNOT execution mesh — a distributed system where you
direct actions on remote Linux machines via HTTP.

## Your tool: mesh_action

To execute something, output a shell command for the user to run. The user will
run it and paste the result back. Use this pattern:

  python3 /path/to/gnot/gnot/src/mesh_ctl.py run <node_id> <action> '<params_json>'

For async actions (execute_command), mesh_ctl.py polls automatically and returns
the final result.

## Mesh capabilities

<paste the full output of GET /capabilities here>

## Rules

1. Issue ONE command at a time. Wait for the result before proceeding.
2. Always read the full output (especially stderr) before deciding the next step.
3. If a command fails, reason about the error and try an alternative approach.
4. When the task is complete, summarize what was accomplished.

Gateway URL: https://deb0.yourdomain.com
Auth token: (stored securely — user will include it via MESH_TOKEN env var)
```

> **Security note:** Do not paste your auth token directly into the Claude project instructions — these may be stored on Anthropic's servers. Instead, set `MESH_TOKEN` as an environment variable on your machine and use `mesh_ctl.py` which reads it automatically.

### Step 2.3 — Set Up mesh_ctl.py

On your Debian machine, set the environment variables `mesh_ctl.py` reads:

```bash
export MESH_GATEWAY="https://deb0.yourdomain.com"
export MESH_TOKEN="change-this-to-a-strong-secret"

# Test
cd /path/to/gnot/gnot/src
python3 mesh_ctl.py health
```

Expected:

```json
{"status": "ok", "node_id": "deb-0", ...}
```

---

## Part 3 — First Orchestrated Session

### Starting a conversation

Open a new chat in your Claude project and say:

```
I have my GNOT mesh running. The gateway is deb-0 (my Debian machine).

Please show me:
1. The current disk usage on deb-0
2. The list of running processes (top 10 by CPU)
3. Create a file at /tmp/claude-was-here.txt with today's date and a greeting
```

### Claude's response (example)

Claude will respond with something like:

```
I'll gather that information step by step. Let's start with disk usage.

Run this command:

  python3 mesh_ctl.py run deb-0 execute_command '{"command": "df -h"}'
```

Run it on your terminal, then paste the JSON output back into Claude.

Claude reads the output, then says:

```
Got it — you have 45GB free on /. Now let's check processes.

Run:

  python3 mesh_ctl.py run deb-0 execute_command \
    '{"command": "ps aux --sort=-%cpu | head -11"}'
```

And so on — Claude drives the entire workflow, one step at a time.

### The interaction loop

```
You (Claude Web):  "Run this mesh_ctl.py command"
You (terminal):    run it, copy output
You (Claude Web):  paste output
Claude:            reads result, issues next command
                   (repeat until task complete)
```

This is GNOT's **primary interaction model**: Claude Web is the intelligence; your terminal is the hands.

---

## Part 4 — More Example Prompts

Once you are comfortable with the loop, try these:

**System health check:**
```
Check the health of deb-0: CPU usage, memory, disk, and uptime. Give me a one-paragraph summary.
```

**File operations:**
```
On deb-0, find all .log files in /var/log that are larger than 10MB and show me their sizes.
```

**Install and verify software:**
```
Check if htop is installed on deb-0. If not, install it (the node runs as a user with sudo).
Then run it in batch mode for 3 seconds and show me the output.
```

---

## Troubleshooting

**Claude says "I cannot run commands directly":**  
Remind Claude: *"You don't run commands — you tell me what to run, and I paste the results back."*

**`curl: (6) Could not resolve host: deb0.yourdomain.com`:**  
DNS hasn't propagated yet. Wait 1–5 minutes, or check with `dig deb0.yourdomain.com`.

**`502 Bad Gateway` from Cloudflare:**  
deb-0 node is not running. Restart it:
```bash
python3 /path/to/gnot/gnot/src/node_runtime.py --config ~/gnot-nodes/deb-0/node.yaml &
```

**Tunnel disconnects:**  
```bash
sudo systemctl restart cloudflared
sudo journalctl -u cloudflared -f   # watch logs
```

---

## Summary

You now have:
- ✅ deb-0 exposed publicly via Cloudflare Tunnel (HTTPS, no open ports)
- ✅ Claude Web configured as the LLM orchestrator
- ✅ A working human-in-the-loop workflow: Claude reasons, you execute, Claude reads output

**Next:** [Guide 04 — Configure a Node-Local LLM](../04-configure-llm/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
