# Guide 19 — Build a React App and Deploy to Another Machine

**Difficulty:** Intermediate  
**Prerequisite:** [Guide 09 — Execute Across Multiple Nodes](../09-multi-node-execution/README.md)  
**Goal:** Build a React/Vite application on a dev node, then deploy the production bundle to a web server on a completely different machine — all orchestrated from a single prompt through the GNOT mesh.

---

## Scenario

```
deb-0  (dev machine — builds React app)
  │
  │  bundle (dist/) via gateway staging
  │
  ▼
cen-0  (prod server — serves via nginx, behind NAT)
```

cen-0 has no public address and no direct connection to deb-0. The `dist/` folder travels through deb-0's staging area as a compressed archive.

---

## Method A — Build on Dev, Deploy via Gateway Staging (Recommended for NAT)

This method works even when the two machines have no direct network path to each other.

### Part 1 — The `react_build` Action (on deb-0)

```python
# ~/gnot-nodes/deb-0/actions/react_build.py
"""
Clone (or pull) a React repo, install dependencies, and run the production build.
Returns the path to the dist/ directory.
"""

import asyncio
import os

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    repo_url   = params.get("repo_url")           # git URL, optional
    repo_path  = params["repo_path"]              # local path to repo root
    build_cmd  = params.get("build_cmd", "npm run build")
    node_env   = params.get("node_env", "production")
    dist_dir   = params.get("dist_dir", "dist")   # relative to repo_path

    # Clone if repo_url provided and path doesn't exist
    if repo_url and not os.path.exists(repo_path):
        clone_proc = await asyncio.create_subprocess_shell(
            f"git clone {repo_url} {repo_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(clone_proc.communicate(), timeout=120)
        if clone_proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {stderr.decode()[-300:]}")
    elif os.path.exists(os.path.join(repo_path, ".git")):
        # Pull latest changes
        pull_proc = await asyncio.create_subprocess_shell(
            "git pull",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(pull_proc.communicate(), timeout=60)

    # Install dependencies
    install_proc = await asyncio.create_subprocess_shell(
        "npm ci --prefer-offline || npm install",
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, install_err = await asyncio.wait_for(install_proc.communicate(), timeout=300)
    if install_proc.returncode != 0:
        raise RuntimeError(f"npm install failed: {install_err.decode()[-400:]}")

    # Build
    env = os.environ.copy()
    env["NODE_ENV"] = node_env

    build_proc = await asyncio.create_subprocess_shell(
        build_cmd,
        cwd=repo_path,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(build_proc.communicate(), timeout=300)
    if build_proc.returncode != 0:
        raise RuntimeError(f"Build failed: {stderr.decode()[-500:]}")

    dist_path = os.path.join(repo_path, dist_dir)
    if not os.path.exists(dist_path):
        raise FileNotFoundError(f"dist dir not found at {dist_path}")

    # Count output files
    file_count = sum(len(files) for _, _, files in os.walk(dist_path))

    # Get total size
    size_bytes = sum(
        os.path.getsize(os.path.join(root, f))
        for root, _, files in os.walk(dist_path)
        for f in files
    )

    return {
        "dist_path": dist_path,
        "repo_path": repo_path,
        "file_count": file_count,
        "size_kb": round(size_bytes / 1024, 1),
        "build_output": stdout.decode()[-500:],
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "react_build",
  "description": "Clone/pull a React repo and build the production bundle. Returns dist/ path.",
  "type": "object",
  "properties": {
    "repo_path": {
      "type": "string",
      "description": "Absolute path to the repository on this node"
    },
    "repo_url": {
      "type": "string",
      "description": "Git URL to clone if repo_path does not exist (optional)"
    },
    "build_cmd": {
      "type": "string",
      "description": "Build command to run",
      "default": "npm run build"
    },
    "dist_dir": {
      "type": "string",
      "description": "Output directory name relative to repo_path",
      "default": "dist"
    },
    "node_env": {
      "type": "string",
      "enum": ["production", "staging"],
      "default": "production"
    }
  },
  "required": ["repo_path"],
  "additionalProperties": false
}
```

### Part 2 — The `deploy_static` Action (on cen-0)

```python
# ~/gnot/cen-0/actions/deploy_static.py
"""
Extract a tar.gz archive into the nginx web root.
Optionally reloads nginx after deploy.
"""

import asyncio
import os
import shutil
import tarfile

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    archive_path = params["archive_path"]       # path to .tar.gz on this node
    web_root     = params.get("web_root", "/usr/share/nginx/html")
    backup       = params.get("backup", True)
    reload_nginx = params.get("reload_nginx", True)

    if not os.path.exists(archive_path):
        raise FileNotFoundError(f"Archive not found: {archive_path}")

    # Backup current web root
    backup_path = None
    if backup and os.path.exists(web_root):
        backup_path = f"{web_root}.bak"
        if os.path.exists(backup_path):
            shutil.rmtree(backup_path)
        shutil.copytree(web_root, backup_path)

    # Clear web root and extract
    if os.path.exists(web_root):
        shutil.rmtree(web_root)
    os.makedirs(web_root, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(web_root)

    # Count deployed files
    file_count = sum(len(files) for _, _, files in os.walk(web_root))

    result = {
        "deployed_to": web_root,
        "file_count": file_count,
        "archive_path": archive_path,
        "backup_path": backup_path,
    }

    # Reload nginx
    if reload_nginx:
        proc = await asyncio.create_subprocess_shell(
            "sudo nginx -t && sudo systemctl reload nginx",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        result["nginx_reloaded"] = proc.returncode == 0
        if proc.returncode != 0:
            result["nginx_error"] = stderr.decode()[-200:]

    return result
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "deploy_static",
  "description": "Extract a tar.gz archive into the nginx web root and reload nginx.",
  "type": "object",
  "properties": {
    "archive_path": {
      "type": "string",
      "description": "Path to the .tar.gz file on this node"
    },
    "web_root": {
      "type": "string",
      "description": "Target directory (nginx web root)",
      "default": "/usr/share/nginx/html"
    },
    "backup": {
      "type": "boolean",
      "description": "Back up the current web root before replacing it",
      "default": true
    },
    "reload_nginx": {
      "type": "boolean",
      "description": "Run nginx reload after deploy",
      "default": true
    }
  },
  "required": ["archive_path"],
  "additionalProperties": false
}
```

### Part 3 — Full Deploy Workflow via Single Prompt

Install both actions, restart both nodes, then trigger:

```bash
export TOKEN="change-this-to-a-strong-secret"

curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Deploy the React app at /opt/my-react-app on deb-0 to the nginx web root on cen-0. Steps: 1) Build the app on deb-0 using react_build, 2) Compress the dist/ folder into a .tar.gz on deb-0, 3) Upload the archive to the gateway staging area, 4) Download it on cen-0, 5) Run deploy_static on cen-0, 6) Verify nginx is serving the new files by checking /usr/share/nginx/html/index.html on cen-0.",
    "session_id": "react-deploy"
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

The LLM orchestrates each step:

```
Step 1: react_build on deb-0          → dist_path: /opt/my-react-app/dist
Step 2: execute_command on deb-0      → tar -czf /tmp/app-dist.tar.gz -C /opt/my-react-app dist/
Step 3: execute_command on deb-0      → curl upload → file_id: "abc123"
Step 4: execute_command on cen-0      → curl download to /tmp/app-dist.tar.gz
Step 5: deploy_static on cen-0        → extracted 42 files, nginx reloaded
Step 6: read_file on cen-0            → index.html content verified ✅
```

---

## Method B — Build on Dev, Deploy via Direct SSH

Use this if deb-0 has SSH access to cen-0 (same network or VPN).

```bash
# Single prompt — no custom actions needed, just execute_command
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "On deb-0: go to /opt/my-react-app, run npm run build, then rsync the dist/ folder to prod-server:/usr/share/nginx/html/ (SSH key is at ~/.ssh/id_rsa, remote user is deploy). After rsync, run ssh deploy@prod-server sudo systemctl reload nginx. Report the file count deployed.",
    "session_id": "deploy-ssh"
  }'
```

No custom actions required — `execute_command` is sufficient when SSH is available.

---

## Method C — Clone and Build on Production Server

Use this when you want the prod server to build its own code (avoids transferring the bundle entirely).

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "On cen-0: check if Node.js >= 18 is installed (install if not). Clone https://github.com/myorg/my-react-app to /opt/my-react-app if it does not exist, or git pull if it does. Run npm ci then npm run build. Copy the dist/ contents to /usr/share/nginx/html/. Reload nginx. Verify by reading /usr/share/nginx/html/index.html.",
    "session_id": "build-on-prod"
  }'
```

---

## nginx Setup on cen-0

If nginx is not installed:

```bash
python3 gnot/src/mesh_ctl.py run cen-0 execute_command \
  '{"command": "sudo dnf install -y nginx && sudo systemctl enable nginx && sudo systemctl start nginx"}'
```

Minimal nginx config for a React SPA (handles client-side routing):

```bash
python3 gnot/src/mesh_ctl.py run cen-0 write_file '{
  "path": "/etc/nginx/conf.d/react-app.conf",
  "content": "server {\n    listen 80;\n    root /usr/share/nginx/html;\n    index index.html;\n    location / {\n        try_files $uri $uri/ /index.html;\n    }\n    gzip on;\n    gzip_types text/plain text/css application/json application/javascript;\n}"
}'
```

---

## Rollback

If the deploy goes wrong, `deploy_static` automatically creates a `.bak` backup. Restore it:

```bash
python3 gnot/src/mesh_ctl.py run cen-0 execute_command \
  '{"command": "rm -rf /usr/share/nginx/html && mv /usr/share/nginx/html.bak /usr/share/nginx/html && sudo systemctl reload nginx && echo rollback-ok"}'
```

---

## Summary

You can now:
- ✅ Build React apps remotely via `react_build` action
- ✅ Deploy bundles across NAT boundaries via gateway staging (Method A)
- ✅ Use SSH-based deploy when direct network access exists (Method B)
- ✅ Build directly on prod when needed (Method C)
- ✅ Rollback to the previous version instantly

**Next:** [Guide 20 — Database Migration Between Private Networks](../20-database-migration/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
