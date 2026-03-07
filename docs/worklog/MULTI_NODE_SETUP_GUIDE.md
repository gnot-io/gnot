# Multi-Node Setup Guide — Execution Mesh v5.1

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-01
**Use case:** Claude agent điều khiển 3 máy, copy file giữa các máy bằng prompt

---

## 1. Mục tiêu

Thiết lập hệ thống 3 node trên 3 máy vật lý khác nhau, cho phép Claude agent gửi một prompt đơn giản như:

> *"Copy thư mục /data/reports từ máy B sang máy C tại /backup/reports"*

Claude sẽ tự động orchestrate toàn bộ workflow: tar trên B → transfer qua gateway A → untar trên C.

---

## 2. Kiến trúc

```
                    ┌─────────────────────────────────┐
                    │         Claude Agent             │
                    │  (gửi prompt bằng ngôn ngữ tự   │
                    │   nhiên, Claude orchestrate)     │
                    └──────────────┬──────────────────┘
                                   │ HTTPS
                                   ▼
                    ┌──────────────────────────────────┐
                    │     Cloudflare Edge (CDN/TLS)    │
                    └──────────────┬───────────────────┘
                                   │ Tunnel (QUIC)
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│  Máy 1 — Debian (Gateway)                                       │
│                                                                  │
│  ┌─────────────┐     ┌─────────────────────────────────────┐    │
│  │ cloudflared  │────▶│  Node A (node-gateway)               │    │
│  │  daemon      │     │  Port 8080 — AUTH ENABLED            │    │
│  └─────────────┘     │  Role: Gateway + Seed                │    │
│                       │  Actions: read_file, write_file,     │    │
│                       │           execute_command             │    │
│                       │  Knows: node-B @ 10.0.0.2:8080       │    │
│                       │         node-C @ 10.0.0.3:8080       │    │
│                       └──────┬─────────────┬────────────────┘    │
│                              │             │                     │
└──────────────────────────────┼─────────────┼─────────────────────┘
                               │ HTTP proxy  │ HTTP proxy
                    ┌──────────┘             └──────────┐
                    ▼                                    ▼
┌───────────────────────────────┐  ┌───────────────────────────────┐
│  Máy 2 — CentOS              │  │  Máy 3 — AlmaLinux            │
│                               │  │                               │
│  ┌──────────────────────────┐ │  │  ┌──────────────────────────┐ │
│  │ Node B (node-B)          │ │  │  │ Node C (node-C)          │ │
│  │ Port 8080 — NO AUTH      │ │  │  │ Port 8080 — NO AUTH      │ │
│  │ Actions: read_file,      │ │  │  │ Actions: read_file,      │ │
│  │   write_file,            │ │  │  │   write_file,            │ │
│  │   execute_command         │ │  │  │   execute_command         │ │
│  └──────────────────────────┘ │  │  └──────────────────────────┘ │
│  IP: 10.0.0.2                │  │  IP: 10.0.0.3                │
└───────────────────────────────┘  └───────────────────────────────┘
```

**Thiết kế bảo mật:**

- Chỉ Node A được expose ra internet (qua Cloudflare Tunnel)
- Node B, C chạy trên mạng nội bộ, không cần auth (chỉ Node A truy cập được)
- Claude gọi Node A → Node A proxy sang B hoặc C khi `target_node_id` không phải chính nó
- Hop count + loop detection chống routing vòng lặp

---

## 3. Yêu cầu mạng

Node A phải có thể kết nối HTTP tới Node B và Node C. Có 3 cách:

| Phương án | Khi nào dùng | Cách hoạt động |
|-----------|-------------|---------------|
| **LAN trực tiếp** | Cùng mạng nội bộ | Dùng IP nội bộ (192.168.x.x hoặc 10.x.x.x) |
| **Tailscale** | Khác mạng, đơn giản nhất | Cài Tailscale trên cả 3 máy, dùng Tailscale IP (100.x.x.x) |
| **WireGuard** | Khác mạng, cần tự quản lý | Thiết lập VPN thủ công giữa các máy |

**Tài liệu này dùng IP ví dụ:**

| Máy | OS | Hostname | IP nội bộ |
|-----|----|----------|-----------|
| Máy 1 | Debian 12 | gateway | 10.0.0.1 |
| Máy 2 | CentOS 9 | worker-b | 10.0.0.2 |
| Máy 3 | AlmaLinux 9 | worker-c | 10.0.0.3 |

*Thay bằng IP thực tế hoặc Tailscale IP của bạn.*

---

## 4. Setup Node A — Gateway (Debian)

### 4.1 Cài đặt Python và dependencies

```bash
# Cập nhật hệ thống
sudo apt update && sudo apt upgrade -y

# Cài Python 3.11+
sudo apt install -y python3 python3-pip python3-venv git

# Kiểm tra version
python3 --version   # cần >= 3.11
```

### 4.2 Clone repo và cài dependencies

```bash
mkdir -p ~/mesh && cd ~/mesh

# Clone hoặc copy codebase vào đây
# Giả sử codebase đã có tại ~/mesh/

# Tạo virtual environment
python3 -m venv venv
source venv/bin/activate

# Cài dependencies
pip install -r requirements.txt
```

### 4.3 Tạo config cho Node A

```bash
cat > node-gateway/node.yaml << 'EOF'
node_id: node-gateway
listen: 0.0.0.0:8080

# === NODE REGISTRY ===
# Node A biết địa chỉ của tất cả nodes trong mesh
nodes:
  node-gateway: http://127.0.0.1:8080
  node-B: http://10.0.0.2:8080
  node-C: http://10.0.0.3:8080

default_resolver: node-gateway
max_hop: 10
cache_ttl_seconds: 300

# === AUTH ===
# Chỉ Node A cần auth vì expose ra internet
auth_token: mesh-secret-token-change-me

# === JOB CLEANUP ===
job_ttl_seconds: 3600
cleanup_interval_seconds: 60
EOF
```

### 4.4 Tạo skills.md cho Node A

```bash
mkdir -p node-gateway
cat > node-gateway/skills.md << 'EOF'
# Node: node-gateway (Gateway + Seed)

## Role
Gateway node — entry point cho Claude agent.
Routes requests tới node-B và node-C.
Cũng có thể thực thi action trực tiếp trên máy gateway.

## Available Actions
- **read_file**: Đọc file trên máy gateway
- **write_file**: Ghi file trên máy gateway
- **execute_command**: Chạy shell command trên máy gateway

## Known Nodes
- **node-B** (10.0.0.2): Worker trên CentOS — quản lý data/reports
- **node-C** (10.0.0.3): Worker trên AlmaLinux — backup storage
EOF
```

### 4.5 Start Node A

```bash
cd ~/mesh
source venv/bin/activate

# Start (foreground để test)
python node_runtime.py --config node-gateway/node.yaml

# Hoặc chạy nền
nohup python node_runtime.py --config node-gateway/node.yaml > /tmp/node-gateway.log 2>&1 &
echo $! > /tmp/node-gateway.pid
```

### 4.6 Setup Cloudflare Tunnel

```bash
# Cài cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
  -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb

# Login (mở browser)
cloudflared tunnel login

# Tạo tunnel
cloudflared tunnel create mesh-gateway
# → ghi nhớ TUNNEL_ID output ra

# Tạo DNS record
cloudflared tunnel route dns <TUNNEL_ID> mesh-gateway.your-domain.com

# Tạo config
sudo mkdir -p /etc/cloudflared
sudo tee /etc/cloudflared/config.yml << EOF
tunnel: <TUNNEL_ID>
credentials-file: /etc/cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: mesh-gateway.your-domain.com
    service: http://127.0.0.1:8080
  - service: http_status:404
EOF

# Cài systemd service
sudo tee /etc/systemd/system/cloudflared.service << 'EOF'
[Unit]
Description=cloudflared
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/cloudflared --no-autoupdate --config /etc/cloudflared/config.yml tunnel run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

### 4.7 Verify Node A

```bash
# Local
curl -s http://127.0.0.1:8080/health

# Public (qua Cloudflare)
curl -s https://mesh-gateway.your-domain.com/health
```

---

## 5. Setup Node B — Worker (CentOS)

### 5.1 Cài đặt Python

```bash
# CentOS 9 / RHEL 9
sudo dnf update -y
sudo dnf install -y python3.11 python3.11-pip git

# Nếu CentOS 9 chỉ có python3.9, dùng EPEL:
sudo dnf install -y epel-release
sudo dnf install -y python3.11

# Kiểm tra
python3.11 --version
```

**Nếu chỉ có Python 3.9** (CentOS 9 mặc định):

```bash
# Python 3.9 vẫn hoạt động, chỉ cần sửa type hints
# Hoặc compile Python 3.11 từ source:
sudo dnf install -y gcc openssl-devel bzip2-devel libffi-devel
curl -O https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
tar xzf Python-3.11.9.tgz && cd Python-3.11.9
./configure --enable-optimizations
make -j$(nproc) && sudo make altinstall
```

### 5.2 Clone repo và cài dependencies

```bash
mkdir -p ~/mesh && cd ~/mesh

# Copy codebase (scp từ gateway hoặc git clone)
scp -r user@10.0.0.1:~/mesh/{runtime,seed,requirements.txt,node_runtime.py,pyproject.toml} ~/mesh/

# Tạo venv
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 5.3 Tạo config cho Node B

```bash
mkdir -p node-B
cat > node-B/node.yaml << 'EOF'
node_id: node-B
listen: 0.0.0.0:8080

nodes:
  node-B: http://127.0.0.1:8080
  node-gateway: http://10.0.0.1:8080

default_resolver: node-gateway
max_hop: 10
cache_ttl_seconds: 300

# KHÔNG cần auth — chỉ Node A truy cập được qua mạng nội bộ
# auth_token: null

job_ttl_seconds: 3600
cleanup_interval_seconds: 60
EOF
```

### 5.4 Tạo skills.md cho Node B

```bash
cat > node-B/skills.md << 'EOF'
# Node: node-B (CentOS Worker)

## Role
Worker node trên CentOS. Quản lý data và reports.

## Available Actions
- **read_file**: Đọc file trên máy B
- **write_file**: Ghi file trên máy B
- **execute_command**: Chạy shell command trên máy B

## Notable Directories
- /data/reports — báo cáo dữ liệu
- /home/worker/projects — mã nguồn
EOF
```

### 5.5 Mở firewall cho Node A

```bash
# Cho phép port 8080 từ gateway (10.0.0.1)
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.0.0.1" port protocol="tcp" port="8080" accept'
sudo firewall-cmd --reload

# Verify
sudo firewall-cmd --list-rich-rules
```

### 5.6 Start Node B

```bash
cd ~/mesh
source venv/bin/activate

# Start
nohup python node_runtime.py --config node-B/node.yaml > /tmp/node-B.log 2>&1 &
echo $! > /tmp/node-B.pid
```

### 5.7 Tạo systemd service (production)

```bash
sudo tee /etc/systemd/system/mesh-node.service << EOF
[Unit]
Description=Mesh Node B
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$HOME/mesh
ExecStart=$HOME/mesh/venv/bin/python node_runtime.py --config node-B/node.yaml
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mesh-node
sudo systemctl start mesh-node
```

---

## 6. Setup Node C — Worker (AlmaLinux)

### 6.1 Cài đặt Python

```bash
# AlmaLinux 9 (tương tự CentOS 9)
sudo dnf update -y
sudo dnf install -y python3.11 python3.11-pip git

# Hoặc cài từ EPEL
sudo dnf install -y epel-release
sudo dnf install -y python3.11
```

### 6.2 Clone repo và cài dependencies

```bash
mkdir -p ~/mesh && cd ~/mesh

# Copy codebase
scp -r user@10.0.0.1:~/mesh/{runtime,seed,requirements.txt,node_runtime.py,pyproject.toml} ~/mesh/

python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 6.3 Tạo config cho Node C

```bash
mkdir -p node-C
cat > node-C/node.yaml << 'EOF'
node_id: node-C
listen: 0.0.0.0:8080

nodes:
  node-C: http://127.0.0.1:8080
  node-gateway: http://10.0.0.1:8080

default_resolver: node-gateway
max_hop: 10
cache_ttl_seconds: 300

# KHÔNG cần auth — chỉ Node A truy cập được qua mạng nội bộ
# auth_token: null

job_ttl_seconds: 3600
cleanup_interval_seconds: 60
EOF
```

### 6.4 Tạo skills.md cho Node C

```bash
cat > node-C/skills.md << 'EOF'
# Node: node-C (AlmaLinux Worker)

## Role
Backup/storage worker trên AlmaLinux.

## Available Actions
- **read_file**: Đọc file trên máy C
- **write_file**: Ghi file trên máy C
- **execute_command**: Chạy shell command trên máy C

## Notable Directories
- /backup — thư mục backup chính
- /data — data storage
EOF
```

### 6.5 Mở firewall

```bash
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.0.0.1" port protocol="tcp" port="8080" accept'
sudo firewall-cmd --reload
```

### 6.6 Start Node C

```bash
cd ~/mesh
source venv/bin/activate
nohup python node_runtime.py --config node-C/node.yaml > /tmp/node-C.log 2>&1 &
echo $! > /tmp/node-C.pid
```

### 6.7 Tạo systemd service (giống Node B)

```bash
sudo tee /etc/systemd/system/mesh-node.service << EOF
[Unit]
Description=Mesh Node C
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$HOME/mesh
ExecStart=$HOME/mesh/venv/bin/python node_runtime.py --config node-C/node.yaml
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mesh-node
sudo systemctl start mesh-node
```

---

## 7. Verify toàn bộ Mesh

Chạy từ máy bất kỳ có internet:

### 7.1 Health check tất cả nodes

```bash
BASE=https://mesh-gateway.your-domain.com
TOKEN="mesh-secret-token-change-me"

# Node A (gateway) — direct
curl -s $BASE/health | python3 -m json.tool

# Node B — resolve qua gateway
curl -s $BASE/resolve/node-B \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# → {"node_id": "node-B", "address": "http://10.0.0.2:8080"}

# Node C — resolve qua gateway
curl -s $BASE/resolve/node-C \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# → {"node_id": "node-C", "address": "http://10.0.0.3:8080"}
```

### 7.2 Test action trên từng node

```bash
# Chạy command trên Node B (qua proxy từ gateway)
curl -s -X POST $BASE/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "target_node_id": "node-B",
    "task_id": "test-B",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {"action": "execute_command", "params": {"command": "hostname && cat /etc/os-release | head -3"}}
  }'
# → status: accepted, job_id: node-B-job-xxxxx

# Poll kết quả
sleep 3
curl -s $BASE/result/node-B-job-xxxxx \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# → stdout: "worker-b\nNAME=\"CentOS Stream\"\n..."

# Tương tự cho Node C
curl -s -X POST $BASE/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "target_node_id": "node-C",
    "task_id": "test-C",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {"action": "execute_command", "params": {"command": "hostname && cat /etc/os-release | head -3"}}
  }'
```

### 7.3 Test cross-node routing flow

```bash
# Ghi file trên Node B
curl -s -X POST $BASE/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "target_node_id": "node-B",
    "task_id": "cross-test-1",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {"action": "write_file", "params": {"path": "/tmp/mesh-test.txt", "content": "Hello from mesh!"}}
  }'
# → status: completed (sync — proxy tới B, B thực thi, trả kết quả về qua A)

# Đọc file từ Node B
curl -s -X POST $BASE/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "target_node_id": "node-B",
    "task_id": "cross-test-2",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {"action": "read_file", "params": {"path": "/tmp/mesh-test.txt"}}
  }'
# → output: {"content": "Hello from mesh!", "size_bytes": 16}
```

---

## 8. Use Case: "Copy thư mục X từ máy B sang máy C"

### 8.1 Luồng xử lý

Khi user gửi prompt:

> *"Copy thư mục /data/reports từ máy B sang máy C tại /backup/reports"*

Claude agent sẽ tự orchestrate 4 bước:

```
Bước 1: Kiểm tra thư mục nguồn trên Node B
         → target: node-B, action: execute_command
         → command: "ls -la /data/reports && du -sh /data/reports"

Bước 2: Đóng gói trên Node B
         → target: node-B, action: execute_command
         → command: "tar czf /tmp/transfer-<uuid>.tar.gz -C /data reports"

Bước 3: Chuyển file (B → C qua base64 relay)

         3a. Đọc base64 từ B:
             → target: node-B, action: execute_command
             → command: "base64 -w0 /tmp/transfer-<uuid>.tar.gz"
             → stdout chứa base64 encoded content

         3b. Ghi + giải nén trên C:
             → target: node-C, action: execute_command
             → command: "echo '<base64_content>' | base64 -d | tar xzf - -C /backup/"

Bước 4: Verify và cleanup
         → target: node-C, action: execute_command
         → command: "ls -la /backup/reports && du -sh /backup/reports"

         → target: node-B, action: execute_command
         → command: "rm /tmp/transfer-<uuid>.tar.gz"
```

### 8.2 Claude sẽ gọi API như thế nào

Dưới đây mô phỏng chính xác các API calls mà Claude sẽ tạo:

**Bước 1 — Kiểm tra nguồn:**

```bash
curl -s --retry 5 --retry-delay 3 -X POST https://mesh-gateway.your-domain.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-change-me" \
  -d '{
    "target_node_id": "node-B",
    "task_id": "copy-step1-check",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {
        "action": "execute_command",
        "params": {"command": "ls -la /data/reports && du -sh /data/reports"}
    }
  }'
# Poll job → xem danh sách files và total size
```

**Bước 2 — Tar trên B:**

```bash
curl -s --retry 5 --retry-delay 3 -X POST https://mesh-gateway.your-domain.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-change-me" \
  -d '{
    "target_node_id": "node-B",
    "task_id": "copy-step2-tar",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {
        "action": "execute_command",
        "params": {"command": "tar czf /tmp/transfer-abc123.tar.gz -C /data reports && ls -la /tmp/transfer-abc123.tar.gz"}
    }
  }'
# Poll job → verify tar file created
```

**Bước 3a — Base64 encode trên B:**

```bash
curl -s --retry 5 --retry-delay 3 -X POST https://mesh-gateway.your-domain.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-change-me" \
  -d '{
    "target_node_id": "node-B",
    "task_id": "copy-step3a-encode",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {
        "action": "execute_command",
        "params": {"command": "base64 -w0 /tmp/transfer-abc123.tar.gz", "timeout_seconds": 300}
    }
  }'
# Poll job → stdout chứa chuỗi base64 (tất cả trên 1 dòng)
```

**Bước 3b — Decode + untar trên C:**

```bash
curl -s --retry 5 --retry-delay 3 -X POST https://mesh-gateway.your-domain.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-change-me" \
  -d '{
    "target_node_id": "node-C",
    "task_id": "copy-step3b-decode",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {
        "action": "execute_command",
        "params": {
            "command": "mkdir -p /backup && echo '\''<BASE64_CONTENT_HERE>'\'' | base64 -d | tar xzf - -C /backup/",
            "timeout_seconds": 300
        }
    }
  }'
```

**Bước 4 — Verify + cleanup:**

```bash
# Verify trên C
curl -s --retry 5 --retry-delay 3 -X POST https://mesh-gateway.your-domain.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-change-me" \
  -d '{
    "target_node_id": "node-C",
    "task_id": "copy-step4-verify",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {"action": "execute_command", "params": {"command": "ls -la /backup/reports && du -sh /backup/reports"}}
  }'

# Cleanup trên B
curl -s --retry 5 --retry-delay 3 -X POST https://mesh-gateway.your-domain.com/action \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-change-me" \
  -d '{
    "target_node_id": "node-B",
    "task_id": "copy-step5-cleanup",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {"action": "execute_command", "params": {"command": "rm -f /tmp/transfer-abc123.tar.gz"}}
  }'
```

### 8.3 Giới hạn kích thước

Phương pháp base64 relay phù hợp cho:

| Kích thước thư mục | Kích thước base64 | Phương pháp |
|--------------------|--------------------|-------------|
| < 10 MB | < 14 MB | ✅ Base64 relay (như trên) |
| 10 – 100 MB | 14 – 140 MB | ⚠️ Base64 relay (tăng timeout, có thể chậm) |
| > 100 MB | > 140 MB | ❌ Dùng rsync/scp trực tiếp (xem mục 8.4) |

### 8.4 Phương pháp cho file lớn (>100 MB)

Nếu Node B có thể SSH tới Node C (đã setup SSH key):

```
Claude gọi Node B:
  → execute_command: "rsync -avz /data/reports/ 10.0.0.3:/backup/reports/"

Claude gọi Node C:
  → execute_command: "ls -la /backup/reports && du -sh /backup/reports"
```

Nếu không có SSH, dùng HTTP transfer qua Node B tự serve:

```
Claude gọi Node B:
  → execute_command: "cd /data && python3 -m http.server 9999 &"

Claude gọi Node C:
  → execute_command: "cd /backup && curl http://10.0.0.2:9999/reports.tar.gz | tar xzf -"

Claude gọi Node B:
  → execute_command: "kill $(lsof -t -i:9999)"    # cleanup
```

---

## 9. SKILL.md cho Claude Agent

Để Claude biết cách sử dụng mesh 3-node, thêm đoạn sau vào SKILL.md:

```markdown
## Multi-Node Mesh

Hệ thống có 3 node:

| Node ID | Máy | Vai trò |
|---------|-----|---------|
| node-gateway | Máy 1 (Debian) | Gateway — tất cả request đi qua đây |
| node-B | Máy 2 (CentOS) | Worker — quản lý data/reports |
| node-C | Máy 3 (AlmaLinux) | Worker — backup storage |

### Routing
- Tất cả request gửi qua: `https://mesh-gateway.your-domain.com/action`
- Muốn chạy trên máy nào → set `target_node_id` tương ứng
- Gateway tự proxy request tới node đích

### Cross-Node File Copy
Để copy thư mục từ node X sang node Y:
1. Tar + base64 encode trên node nguồn (execute_command)
2. Lấy base64 content từ job result (poll)
3. Base64 decode + untar trên node đích (execute_command)
4. Verify + cleanup
```

---

## 10. Vận hành

### 10.1 Thêm node mới

Khi cần thêm Node D (ví dụ trên Ubuntu):

1. Setup runtime trên máy mới (giống section 5/6)
2. Tạo `node-D/node.yaml` với `node_id: node-D`
3. Start node trên máy mới
4. **Cập nhật Node A**: thêm `node-D: http://<IP>:8080` vào `nodes` trong `node-gateway/node.yaml`
5. Restart Node A

Hoặc dùng Bootstrap API:

```bash
curl -s -X POST https://mesh-gateway.your-domain.com/bootstrap \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mesh-secret-token-change-me" \
  -d '{
    "node_id": "node-D",
    "listen": "0.0.0.0:8080",
    "base_dir": "/home/user/mesh",
    "default_resolver": "node-gateway",
    "extra_nodes": {"node-gateway": "http://10.0.0.1:8080"}
  }'
```

### 10.2 Monitoring

```bash
# Health tất cả nodes (từ máy bất kỳ)
for node in node-gateway node-B node-C; do
  echo "=== $node ==="
  curl -s -X POST https://mesh-gateway.your-domain.com/action \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer mesh-secret-token-change-me" \
    -d "{
      \"target_node_id\": \"$node\",
      \"task_id\": \"health-$node\",
      \"trace\": {\"hop_count\": 0, \"route_path\": []},
      \"payload\": {\"action\": \"execute_command\", \"params\": {\"command\": \"uptime && free -h | head -3 && df -h / | tail -1\"}}
    }" 2>/dev/null
  echo
done
```

### 10.3 Troubleshooting

| Vấn đề | Kiểm tra | Giải pháp |
|--------|---------|-----------|
| Node B/C unreachable | Từ máy A: `curl -s http://10.0.0.2:8080/health` | Check firewall, network, process |
| Proxy timeout | Node A log: `Proxy failed: ...` | Tăng `PROXY_TIMEOUT_SECONDS` trong `router.py` |
| Job stuck "accepted" | Action crash trước khi complete | Check node log: `tail -100 /tmp/node-B.log` |
| 502 Cloudflare | Node A không chạy | Restart: `systemctl start mesh-node` |
| DNS not resolving | `dig mesh-gateway.your-domain.com` | Chờ propagation hoặc check Cloudflare dashboard |
| Permission denied | Action cần quyền root | Chạy node bằng user có quyền, hoặc dùng sudo trong command |

---

## 11. Checklist tổng hợp

### Trước khi go-live

- [ ] Máy 1 (Debian): Node A running, Cloudflare tunnel active
- [ ] Máy 2 (CentOS): Node B running, firewall cho phép từ IP máy 1
- [ ] Máy 3 (AlmaLinux): Node C running, firewall cho phép từ IP máy 1
- [ ] `node-gateway/node.yaml` có đầy đủ IP của node-B và node-C
- [ ] Test từ internet: `curl https://mesh-gateway.your-domain.com/health`
- [ ] Test proxy: target node-B qua gateway → response từ CentOS
- [ ] Test proxy: target node-C qua gateway → response từ AlmaLinux
- [ ] Cross-node: write file trên B, read file trên B qua gateway
- [ ] Cross-node: write file trên C, read file trên C qua gateway
- [ ] SKILL.md đã được cập nhật với thông tin mesh
- [ ] Claude agent có thể gọi API thành công

### Bảo mật

- [ ] Chỉ Node A có `auth_token` (public-facing)
- [ ] Node B, C chỉ accept connection từ IP Node A (firewall)
- [ ] Cloudflare Tunnel (không mở port trên router)
- [ ] Token đã đổi khỏi giá trị mặc định
- [ ] SSH key giữa B↔C nếu dùng rsync cho file lớn (optional)

---

*Document generated: 2026-03-01 | Mesh Runtime v5.1 | Repository: ai-infra-runtime-v2*
