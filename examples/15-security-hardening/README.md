# Guide 15 — Security Hardening

**Difficulty:** Advanced  
**Prerequisite:** [Guide 14 — Full Mesh Topology](../14-full-mesh/README.md)  
**Goal:** Apply production-grade security to your GNOT mesh: per-node tokens, action-level authorization policies, credential encryption, non-root process isolation, and Cloudflare Zero Trust access control.

---

## Security Layers in GNOT

| Layer | Mechanism | Default state |
|-------|-----------|---------------|
| Transport | HTTPS via Cloudflare Tunnel | ✅ Active if using Cloudflare |
| Authentication | Bearer token per request | ✅ Active (but shared token) |
| Per-node isolation | Separate token per worker | ⚠️ Needs configuration |
| Action authorization | `caller_policies` | ❌ Open mode by default |
| Credential encryption | AES-256-GCM | ⚠️ Needs `credential_encryption_key` |
| Process isolation | Non-root OS user | ❌ Needs setup |
| Network access control | Cloudflare Access | ❌ Optional but recommended |

---

## Part 1 — Per-Node Tokens

Replace the shared `auth_token` with per-node tokens using `allowed_tokens`.

**deb-0 (`node.yaml`):**

```yaml
node_id: deb-0
listen:  0.0.0.0:8080
actions_dir: /path/to/gnot/mesh/seed/actions

# ── Authentication ──────────────────────────────────────
# Remove single auth_token. Use per-caller token list.
allowed_tokens:
  - "gw-caller-token-abc123"   # for Claude Web / external callers
  - "deb-1-reg-token-xyz789"   # deb-1 registers with this token
  - "cen-0-reg-token-def456"   # cen-0 registers with this token
  - "alm-0-reg-token-ghi012"   # alm-0 registers with this token

trusted_nodes:
  - deb-1
  - cen-0
  - alm-0
```

**deb-1 (`node.yaml`):**

```yaml
node_id: deb-1
...
auth_token: deb-1-local-token    # for direct calls to deb-1 (optional)
gateway_auth_token: deb-1-reg-token-xyz789   # must match deb-0's allowed_tokens
```

With this setup:
- Revoking a worker requires only removing its token from deb-0's `allowed_tokens`
- Compromising one worker's token does not expose the gateway or other workers

---

## Part 2 — Action Authorization Policies

`caller_policies` restricts which tokens can call which actions. Without it, any authenticated caller can call any action — including `execute_command`.

**Example: restrict external callers to read-only actions**

```yaml
# deb-0/node.yaml

caller_policies:
  # Claude Web / external callers: can call everything
  - token: "gw-caller-token-abc123"
    allowed_actions: "*"

  # Read-only monitoring token: can only read state
  - token: "monitoring-token-readonly"
    allowed_actions:
      - "read_file"
      - "execute_command"   # note: still powerful — see Part 4

  # CI/CD token: can deploy files and restart services only
  - token: "cicd-deploy-token"
    allowed_actions:
      - "write_file"
      - "execute_command"
```

> **Important:** `caller_policies` restricts what a token can do on **this node**. It also applies when this node forwards requests to workers — the forwarded request carries the caller's token.

Test that unauthorized calls are rejected:

```bash
# Should succeed (allowed action)
curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer monitoring-token-readonly" \
  -H "Content-Type: application/json" \
  -d '{"target_node_id":"deb-0","payload":{"action":"read_file","params":{"path":"/etc/hostname"}}}' \
  | python3 -m json.tool

# Should fail with 403
curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer monitoring-token-readonly" \
  -H "Content-Type: application/json" \
  -d '{"target_node_id":"deb-0","payload":{"action":"write_file","params":{"path":"/tmp/x","content":"y"}}}' \
  | python3 -m json.tool
```

---

## Part 3 — Credential Encryption

Enable AES-256-GCM encryption for the credential store (used by `POST /intent` sessions):

```yaml
# deb-0/node.yaml

# ── Credential store ─────────────────────────────────────
# Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
credential_encryption_key: "a-64-char-hex-string-here-keep-separate-from-auth-token"

# Optional: persist credentials across restarts
credential_store_path: "/var/lib/gnot/credentials.json"
```

**Why separate from `auth_token`:**  
Rotating `auth_token` (security hygiene) invalidates active sessions if both use the same key. Keeping `credential_encryption_key` separate allows independent rotation.

Create the persistence directory:

```bash
sudo mkdir -p /var/lib/gnot
sudo chown $USER:$USER /var/lib/gnot
```

Verify encryption is active after restart:

```bash
tail -5 ~/gnot-nodes/deb-0/node.log | grep -i "credential"
# Expected: INFO  CredentialStore initialized: encryption_key_fingerprint=a3f7b2c1
```

---

## Part 4 — Non-Root Process Isolation

`execute_command` has full filesystem access as the user running the node. Run nodes as dedicated non-root users to limit blast radius.

```bash
# Create a dedicated user for deb-0 node
sudo useradd -r -m -s /bin/bash gnot-deb0
sudo -u gnot-deb0 mkdir -p /home/gnot-deb0/gnot-nodes/deb-0

# Transfer config and actions
sudo cp ~/gnot-nodes/deb-0/node.yaml /home/gnot-deb0/gnot-nodes/deb-0/
sudo chown -R gnot-deb0:gnot-deb0 /home/gnot-deb0/gnot-nodes

# Restrict: gnot-deb0 can only write to /home/gnot-deb0 and /tmp/gnot-*
# (optional additional restriction via sudoers or AppArmor)

# Run as dedicated user
sudo -u gnot-deb0 nohup python3 /path/to/gnot/mesh/node_runtime.py \
    --config /home/gnot-deb0/gnot-nodes/deb-0/node.yaml \
    > /var/log/gnot-deb0.log 2>&1 &
```

**Or create a systemd service** (recommended for production):

```bash
sudo tee /etc/systemd/system/gnot-deb0.service << 'EOF'
[Unit]
Description=GNOT Node deb-0
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=gnot-deb0
Group=gnot-deb0
WorkingDirectory=/path/to/gnot
ExecStart=/usr/bin/python3 mesh/node_runtime.py --config /home/gnot-deb0/gnot-nodes/deb-0/node.yaml
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/gnot-deb0.log
StandardError=append:/var/log/gnot-deb0.log

# Limits
LimitNOFILE=65535
MemoryLimit=512M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable gnot-deb0
sudo systemctl start gnot-deb0
sudo systemctl status gnot-deb0
```

---

## Part 5 — Cloudflare Access (Zero Trust)

Add an identity layer in front of deb-0 so only authorized users can reach it at all — even before the GNOT auth token is checked.

1. In Cloudflare Zero Trust dashboard → Access → Applications → Add Application
2. **Type:** Self-hosted
3. **Domain:** `deb0.yourdomain.com`
4. **Policy:** Allow only specific email addresses or an entire organization
5. Deploy

With Cloudflare Access enabled, every request to `deb0.yourdomain.com` must first pass through Cloudflare's identity check. Only authenticated users get through to the GNOT bearer token check.

For **service-to-service** calls (workers calling the gateway), use a **Service Auth token** instead:
- In Zero Trust → Access → Service Auth → Create Service Token
- Workers use this token in an additional `CF-Access-Client-Id` / `CF-Access-Client-Secret` header

---

## Security Checklist

Before going to production, verify:

- [ ] Each node has a unique token (`allowed_tokens` on gateway)
- [ ] `caller_policies` restricts what external callers can do
- [ ] `credential_encryption_key` is set (separate from `auth_token`)
- [ ] Nodes run as dedicated non-root OS users
- [ ] deb-0 is behind Cloudflare Tunnel (no direct port exposure)
- [ ] Cloudflare Access policy restricts access by identity (optional)
- [ ] `auth_token` rotation plan is documented
- [ ] Node logs are written to a persistent, monitored location

---

## Summary

Your mesh is now hardened with:
- ✅ Per-node token isolation
- ✅ Action-level authorization policies
- ✅ AES-256-GCM credential encryption
- ✅ Non-root process isolation via systemd
- ✅ Cloudflare Access Zero Trust layer

**Next:** [Guide 16 — Content Automation Pipeline](../16-content-automation/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
