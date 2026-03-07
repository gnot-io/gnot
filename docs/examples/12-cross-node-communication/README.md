# Guide 12 — Cross-Node Communication

**Difficulty:** Intermediate  
**Prerequisite:** [Guide 11 — NAT Traversal](../11-nat-traversal/README.md)  
**Goal:** Transfer a file between cen-0 and alm-0 (two nodes behind different NATs) using deb-0 as a staging relay, and build a simple data pipeline orchestrated by a single prompt.

---

## How Cross-NAT File Transfer Works

cen-0 and alm-0 cannot reach each other directly (different private networks). But both can reach the public gateway deb-0. The transfer goes through three steps:

```
cen-0  ──upload──►  deb-0 (staging)  ──download──►  alm-0
                       /upload                          │
                    (file_id)                    /download/{id}
```

The gateway's `UploadManager` stores the file temporarily (default TTL: 1 hour) and serves it by `file_id`. Neither worker needs a direct connection to the other.

---

## Part 1 — Connect alm-0 (AlmaLinux)

Follow the same steps as Guide 11 for the AlmaLinux machine:

On **alm-0**:

```bash
curl -sSL https://deb0.yourdomain.com/setup.sh | bash -s -- \
    --node-id alm-0 \
    --auth-token alm-0-secret-token \
    --gateway-auth-token change-this-to-a-strong-secret \
    --systemd
```

Or manually create `node.yaml`:

```yaml
node_id: alm-0
listen:  0.0.0.0:8080

actions_dir: /home/YOUR_USER/gnot-repo/mesh/seed/actions

auth_token: alm-0-secret-token
gateway_auth_token: change-this-to-a-strong-secret

gateway_node_id: deb-0
gateway_address: https://deb0.yourdomain.com

heartbeat_interval_seconds: 15
poll_interval_seconds: 5
job_ttl_seconds: 3600
cleanup_interval_seconds: 60
```

Add to deb-0's `trusted_nodes` and `allowed_tokens`, then restart deb-0.

---

## Part 2 — Manual File Transfer via curl

This shows the raw mechanics of the staging file system.

### Step 1: Create a test file on cen-0

```bash
python3 mesh/mesh_ctl.py run cen-0 execute_command \
  '{"command": "echo \"Data from CentOS node - $(date)\" > /tmp/transfer-test.txt && cat /tmp/transfer-test.txt"}'
```

### Step 2: Upload the file from cen-0 to the gateway staging area

```bash
# cen-0 calls the gateway upload endpoint
UPLOAD_RESULT=$(python3 mesh/mesh_ctl.py run cen-0 execute_command '{
  "command": "curl -s -X POST https://deb0.yourdomain.com/upload -H \"Authorization: Bearer change-this-to-a-strong-secret\" -H \"X-Node-ID: cen-0\" -F \"file=@/tmp/transfer-test.txt\""
}')

echo $UPLOAD_RESULT | python3 -m json.tool
# output.stdout contains the upload response with file_id
```

Extract the `file_id`:

```bash
FILE_ID=$(echo $UPLOAD_RESULT | python3 -c "
import sys, json
d = json.load(sys.stdin)
import json as j2
upload_resp = j2.loads(d['output']['stdout'])
print(upload_resp['file_id'])
")
echo "File staged with ID: $FILE_ID"
```

### Step 3: Download the file on alm-0

```bash
python3 mesh/mesh_ctl.py run alm-0 execute_command "{
  \"command\": \"curl -sOJ https://deb0.yourdomain.com/download/$FILE_ID -H 'Authorization: Bearer change-this-to-a-strong-secret' && cat transfer-test.txt\"
}"
```

### Step 4: Clean up the staged file

```bash
curl -s -X DELETE "http://localhost:8080/files/$FILE_ID" \
  -H "Authorization: Bearer change-this-to-a-strong-secret"
```

---

## Part 3 — Full Pipeline via /intent

With a single prompt, the LLM can orchestrate the entire pipeline:

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer change-this-to-a-strong-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Transfer the file /etc/os-release from cen-0 to alm-0. Save it as /tmp/cen0-os-release.txt on alm-0. Then read the file on alm-0 to verify it arrived.",
    "session_id": "transfer-pipeline"
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

The LLM will:
1. `execute_command` on cen-0: read `/etc/os-release`
2. `execute_command` on cen-0: `curl` upload to deb-0 staging
3. Parse the `file_id` from the upload response
4. `execute_command` on alm-0: `curl` download from deb-0 staging
5. `read_file` on alm-0: verify the file content
6. Report success

---

## Part 4 — Database Backup Pipeline

A real-world example — backup MySQL from cen-0, restore on alm-0:

```
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Backup the MySQL database named '\''testdb'\'' on cen-0 (MySQL root password is in /etc/mysql/root.pass), transfer it to alm-0 via the gateway, and restore it as '\''testdb_restored'\'' on alm-0. Report row counts before and after to confirm the restore.",
    "session_id": "db-migrate"
  }'
```

This orchestrates: dump → compress → upload → download → restore → verify — entirely from one prompt.

---

## Staging File Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/upload` | POST (multipart) | Upload file → returns `file_id`, `download_url`, `ttl_seconds` |
| `/download/{file_id}` | GET | Download file (streamed) |
| `/files` | GET | List all staged files with metadata |
| `/files/{file_id}` | DELETE | Delete before TTL expiry |

Default TTL: 3600 seconds (1 hour). Configure in deb-0's `node.yaml`:
```yaml
upload_ttl_seconds: 7200   # 2 hours
upload_max_size_mb: 512    # max file size
```

---

## Summary

You now know how to:
- ✅ Connect a third node (alm-0) to the mesh
- ✅ Transfer files between NAT-isolated nodes via gateway staging
- ✅ Build multi-step data pipelines with a single `/intent` prompt

**Next:** [Guide 13 — Auto-Scale Additional Nodes](../13-auto-scale/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
