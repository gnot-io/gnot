# Cloudflare Tunnel Setup — Node-0 (Seed Node)

**Project:** ai-infra-runtime-v2
**Date:** 2026-03-01
**Author:** Claude (AI Coding Agent)

---

## 1. Tổng quan

Node-0 (Seed Node) của Execution Mesh v5.1 được expose ra internet thông qua Cloudflare Tunnel, cho phép Cloud AI Planner và các client bên ngoài gọi API mà không cần mở port trực tiếp trên server.

**Public URL:** `https://local-agent-server-v5.vietml.com`
**Internal:** `http://127.0.0.1:8080`

---

## 2. Kiến trúc mạng

```
Client (Claude / AI Planner / curl)
    │
    │  HTTPS (TLS terminated by Cloudflare)
    ▼
Cloudflare Edge (CDN / DDoS protection)
    │
    │  Cloudflare Tunnel (QUIC, encrypted)
    ▼
cloudflared daemon (server local)
    │
    │  HTTP (localhost, plaintext)
    ▼
Node-0 (FastAPI @ 127.0.0.1:8080)
```

**Ưu điểm:**
- Không cần mở port trên firewall
- TLS certificate tự động bởi Cloudflare
- DDoS protection miễn phí
- Zero-trust access (có thể thêm Cloudflare Access sau)
- Tunnel tự reconnect khi mất kết nối

---

## 3. Thông tin Tunnel

| Thông số | Giá trị |
|----------|---------|
| Tunnel ID | `3b4a5636-4530-4b5f-8e8b-8ad681dc486e` |
| Credentials file | `/etc/cloudflared/3b4a5636-4530-4b5f-8e8b-8ad681dc486e.json` |
| Config file | `/etc/cloudflared/config.yml` |
| Systemd service | `cloudflared.service` |
| Protocol | QUIC |
| Hostname | `local-agent-server-v5.vietml.com` |
| DNS record | CNAME → `3b4a5636-4530-4b5f-8e8b-8ad681dc486e.cfargotunnel.com` |

---

## 4. Cấu hình

### 4.1 Cloudflared config (`/etc/cloudflared/config.yml`)

Đoạn ingress liên quan đến Mesh v5:

```yaml
tunnel: 3b4a5636-4530-4b5f-8e8b-8ad681dc486e
credentials-file: /etc/cloudflared/3b4a5636-4530-4b5f-8e8b-8ad681dc486e.json

ingress:
  # ... (other services) ...

  # AI Infra Runtime v5 (Execution Mesh)
  - hostname: local-agent-server-v5.vietml.com
    service: http://127.0.0.1:8080

  # Fallback rule (required)
  - service: http_status:404
```

### 4.2 Node-0 config (`mesh/node-0/node.yaml`)

```yaml
node_id: node-0
listen: 0.0.0.0:8080

nodes:
  node-0: http://127.0.0.1:8080

default_resolver: node-0
max_hop: 10
cache_ttl_seconds: 300

# Authentication
auth_token: mesh-secret-token-v51

# Job cleanup
job_ttl_seconds: 3600
cleanup_interval_seconds: 60
```

### 4.3 DNS Record (Cloudflare Dashboard)

| Type | Name | Content | Proxy |
|------|------|---------|-------|
| CNAME | `local-agent-server-v5` | `3b4a5636-4530-4b5f-8e8b-8ad681dc486e.cfargotunnel.com` | Proxied ☁️ |

---

## 5. Thiết lập từ đầu (Step-by-step)

Nếu cần thiết lập lại từ scratch trên một server mới:

### 5.1 Cài đặt cloudflared

```bash
# Debian/Ubuntu
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
sudo dpkg -i cloudflared.deb
```

### 5.2 Login và tạo tunnel (chỉ cần làm 1 lần)

```bash
# Login — mở browser để authorize
cloudflared tunnel login

# Tạo tunnel mới
cloudflared tunnel create mesh-v5

# Output sẽ cho tunnel ID, ví dụ: 3b4a5636-4530-4b5f-8e8b-8ad681dc486e
```

### 5.3 Tạo DNS route

```bash
cloudflared tunnel route dns <TUNNEL_ID> local-agent-server-v5.vietml.com
```

### 5.4 Tạo config file

```bash
sudo mkdir -p /etc/cloudflared

sudo tee /etc/cloudflared/config.yml << 'EOF'
tunnel: <TUNNEL_ID>
credentials-file: /etc/cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: local-agent-server-v5.vietml.com
    service: http://127.0.0.1:8080
  - service: http_status:404
EOF
```

### 5.5 Cài đặt systemd service

```bash
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

### 5.6 Start Node-0

```bash
cd /path/to/mesh
pip install -r requirements.txt
nohup python node_runtime.py --config node-0/node.yaml > /tmp/node-0.log 2>&1 &
```

### 5.7 Verify

```bash
# Local
curl -s http://127.0.0.1:8080/health

# Public (qua Cloudflare)
curl -s https://local-agent-server-v5.vietml.com/health
```

---

## 6. Quản lý vận hành

### 6.1 Lệnh thường dùng

```bash
# Trạng thái tunnel
sudo systemctl status cloudflared

# Restart tunnel (sau khi sửa config)
sudo systemctl restart cloudflared

# Xem log tunnel
sudo journalctl -u cloudflared -f

# Trạng thái node-0
curl -s https://local-agent-server-v5.vietml.com/health

# Xem log node-0
tail -f /tmp/node-0.log
```

### 6.2 Thêm node mới vào tunnel

Khi bootstrap node mới (ví dụ node-1 trên port 8081):

```bash
# 1. Tạo DNS record
cloudflared tunnel route dns <TUNNEL_ID> mesh-node-1.vietml.com

# 2. Thêm ingress rule vào /etc/cloudflared/config.yml
#    - hostname: mesh-node-1.vietml.com
#      service: http://127.0.0.1:8081

# 3. Restart cloudflared
sudo systemctl restart cloudflared
```

### 6.3 Xử lý sự cố

| Triệu chứng | Nguyên nhân | Giải pháp |
|-------------|-------------|-----------|
| 502 Bad Gateway | Node-0 chưa chạy hoặc crash | Start lại: `python node_runtime.py --config node-0/node.yaml` |
| 502 tạm thời (~10s) | Cloudflared đang restart | Chờ tunnel reconnect |
| DNS resolution fail | CNAME chưa propagate | Chờ 1-5 phút, check `dig local-agent-server-v5.vietml.com` |
| 401 Unauthorized | Thiếu hoặc sai Bearer token | Kiểm tra header `Authorization: Bearer mesh-secret-token-v51` |
| Connection refused | Port 8080 không listen | Kiểm tra `ss -tlnp | grep 8080` |
| Tunnel offline | cloudflared service stopped | `sudo systemctl start cloudflared` |

---

## 7. Bảo mật

### 7.1 Layers hiện tại

| Layer | Cơ chế | Trạng thái |
|-------|--------|------------|
| Network | Cloudflare Tunnel (không mở port) | ✅ Active |
| TLS | Cloudflare auto-cert (HTTPS) | ✅ Active |
| DDoS | Cloudflare built-in protection | ✅ Active |
| Auth (app-level) | Bearer token (`auth_token` in node.yaml) | ✅ Active |
| Rate limiting | Chưa có | ⚠️ Nên thêm |

### 7.2 Khuyến nghị tăng cường

**Short-term:**
- Thêm Cloudflare Access policy (Zero Trust) để giới hạn IP/email
- Thêm rate limiting trên Cloudflare WAF rules
- Rotate `auth_token` định kỳ

**Mid-term:**
- Mutual TLS giữa các node trong mesh
- API key rotation automation
- Audit log cho mọi action request

---

## 8. Port Map tổng hợp

Bảng tham chiếu tất cả services trên server liên quan đến mesh:

| Service | Port | Hostname | Mô tả |
|---------|------|----------|--------|
| Node-0 (Seed) | 8080 | `local-agent-server-v5.vietml.com` | Mesh seed node, 3 actions |
| Local Agent API v1 | 5000 | `local-agent-server.vietml.com` | Legacy API (read/upload/execute) |
| Local Agent API v2 | 8100 | `local-agent-server-v2.vietml.com` | Legacy v2 |
| MCP Server | 5100 | `mcp-local-agent-server.vietml.com` | MCP protocol bridge |

**Lưu ý:** Node-0 (port 8080) có thể thay thế hoàn toàn Local Agent API v1 (port 5000) cho các tác vụ `read_file`, `write_file`, `execute_command`. Xem `docs/SKILL_LOCAL_AGENT_V5.md` để biết cách migrate.

---

*Document generated: 2026-03-01 | Mesh Runtime v5.1 | Repository: ai-infra-runtime-v2*
