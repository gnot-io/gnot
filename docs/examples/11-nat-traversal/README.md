# Guide 11 — NAT Traversal

**Difficulty:** Intermediate  
**Prerequisite:** [Guide 10 — Sub-Node Registration](../10-sub-node-registration/README.md)  
**Goal:** Connect cen-0 (CentOS machine on a different network) to deb-0 using pull-mode delivery — no inbound ports, no VPN.

---

## The NAT Problem (and GNOT's Solution)

Worker nodes behind NAT cannot receive inbound connections. Traditional RPC systems require opening firewall ports or setting up VPNs. GNOT's pull-mode solves this differently:

```
┌─────────────────────────────────────┐
│  deb-0  (public, via Cloudflare)    │
│                                     │
│  JobQueue:  [ job for cen-0 ]  ◄──── POST /action from caller
│                                     │
│  GET /jobs/poll?node_id=cen-0  ◄─── cen-0 polls outbound every 5s
└─────────────────────────────────────┘

        ▲  outbound HTTPS only
        │  (no inbound required)
┌───────┴──────────────────────────────┐
│  cen-0  (CentOS, behind NAT)         │
│  WorkerAgent polls → claims → runs   │
└──────────────────────────────────────┘
```

cen-0 never receives an inbound connection. It polls deb-0 (outbound HTTPS), claims jobs, executes them locally, and POSTs the result back — also outbound.

---

## Part 1 — Set Up cen-0 (CentOS Machine)

On your **CentOS machine**, install GNOT using the one-liner bootstrap:

### Step 1.1 — Use the setup.sh script

deb-0's `/setup.sh` endpoint is a universal worker installer (OS-aware). Run this on your CentOS machine:

```bash
# On the CentOS machine:
curl -sSL https://deb0.yourdomain.com/setup.sh | bash -s -- \
    --node-id cen-0 \
    --auth-token cen-0-secret-token \
    --gateway-auth-token change-this-to-a-strong-secret \
    --systemd
```

This script:
- Detects the OS (CentOS/RHEL)
- Installs Python 3.11+ if needed
- Downloads the GNOT runtime from deb-0
- Creates `~/gnot/cen-0/node.yaml`
- Creates a systemd service `gnot-cen-0.service`
- Starts the node

> **Alternatively**, install manually (if the setup script is unavailable):

```bash
# Install Python 3.11
sudo dnf install -y python3.11 python3.11-pip git

# Clone GNOT
git clone https://github.com/gnot-io/gnot.git ~/gnot-repo
cd ~/gnot-repo/mesh && pip3.11 install -r requirements.txt

# Create node config
mkdir -p ~/gnot/cen-0
```

Create `~/gnot/cen-0/node.yaml`:

```yaml
node_id: cen-0
listen:  0.0.0.0:8080

actions_dir: /home/YOUR_USER/gnot-repo/gnot/src/seed/actions

auth_token: cen-0-secret-token
gateway_auth_token: change-this-to-a-strong-secret   # deb-0's token

# ── Worker mode: pulls from deb-0 ─────────────────────────
gateway_node_id: deb-0
gateway_address: https://deb0.yourdomain.com

# No self_address = always pull mode (never push)
# This is correct for NAT — cen-0 can't receive inbound connections

heartbeat_interval_seconds: 15
poll_interval_seconds: 5

job_ttl_seconds: 3600
cleanup_interval_seconds: 60
```

```bash
# Start
nohup python3.11 ~/gnot-repo/gnot/src/node_runtime.py \
    --config ~/gnot/cen-0/node.yaml \
    > ~/gnot/cen-0/node.log 2>&1 &
```

---

## Part 2 — Update deb-0 to Trust cen-0

On **deb-0**, edit `~/gnot-nodes/deb-0/node.yaml`:

```yaml
trusted_nodes:
  - deb-1
  - cen-0      # ← add this

allowed_tokens:
  - change-this-to-a-strong-secret
  - deb-1-secret-token
  - cen-0-secret-token    # ← add this
```

Restart deb-0.

---

## Part 3 — Verify Pull-Mode Registration

On **deb-0**, watch the registration:

```bash
tail -f ~/gnot-nodes/deb-0/node.log
```

Expected:

```
INFO  Node cen-0 registered (pull-mode, no self_address)
INFO  NodeRegistry: cen-0 online
```

Check capabilities:

```bash
curl -s http://localhost:8080/capabilities \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  | python3 -c "
import sys,json
caps = json.load(sys.stdin)
cen0 = caps['reachable'].get('cen-0', 'NOT FOUND')
if isinstance(cen0, dict):
    print('cen-0 status:', cen0.get('status'))
    print('cen-0 actions:', cen0.get('actions'))
else:
    print(cen0)
"
```

---

## Part 4 — Test a Job Through the Pull Queue

From **deb-0** (or any machine that can reach deb-0):

```bash
# Submit job — returns job_id immediately (async pull)
RESPONSE=$(curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "cen-0",
    "payload": {
      "action": "execute_command",
      "params": {"command": "cat /etc/centos-release && hostname && uptime"}
    }
  }')
echo $RESPONSE | python3 -m json.tool

JOB_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

# Wait for cen-0 to poll, claim, execute, and report (up to ~10 seconds)
sleep 10

curl -s "http://localhost:8080/result/$JOB_ID" \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  | python3 -m json.tool
```

Expected output includes the CentOS release and hostname of the remote machine — executing entirely through the pull queue with no inbound connection to cen-0.

---

## Pull-Mode Timing

| Event | Timing |
|-------|--------|
| Job submitted to deb-0 | t=0 (immediate response) |
| cen-0 next poll | t≈5s (poll_interval_seconds) |
| cen-0 claims + starts executing | t≈5s |
| Execution completes | t≈5s + command_duration |
| Result visible in GET /result | t≈5s + command_duration |

For time-sensitive workloads, reduce `poll_interval_seconds` to 1–2 seconds.

---

## Troubleshooting

**cen-0 appears as `unreachable` after registration:**  
Heartbeats may not be reaching deb-0. Check that cen-0's outbound HTTPS to `deb0.yourdomain.com` works:
```bash
# On cen-0:
curl -s https://deb0.yourdomain.com/health
```

**Jobs stay in `queued` status indefinitely:**  
cen-0 is not polling. Check cen-0's log:
```bash
tail -f ~/gnot/cen-0/node.log
```

**`gateway_auth_token` rejection:**  
The token cen-0 uses to talk to deb-0 (`gateway_auth_token`) must appear in deb-0's `allowed_tokens` list.

---

## Summary

You now have:
- ✅ cen-0 connected to deb-0 from a different network
- ✅ Pull-mode delivery working with no inbound ports on cen-0
- ✅ Jobs routed to cen-0 and results returned transparently

**Next:** [Guide 12 — Cross-Node Communication](../12-cross-node-communication/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
