#!/usr/bin/env bash
# =============================================================================
# Execution Mesh v5 — Universal Node Setup Script
# =============================================================================
#
# Cài đặt và khởi chạy một mesh node trên bất kỳ Linux distro nào.
#
# Sử dụng:
#   curl -sSL https://<gateway>/setup.sh | bash -s -- \
#       --node-id node-X \
#       --port 8080 \
#       --gateway http://10.0.0.1:8080 \
#       [--auth-token <token>] \
#       [--install-dir /opt/mesh] \
#       [--gateway-hostname mesh-gw.example.com] \
#       [--systemd]
#
# Hoặc nếu đã tải về:
#   chmod +x setup.sh
#   ./setup.sh --node-id node-B --port 8080 --gateway http://10.0.0.1:8080
#
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
NODE_ID=""
PORT="8080"
GATEWAY_URL="__GATEWAY_PUBLIC_URL__"
GATEWAY_HOSTNAME=""
AUTH_TOKEN=""
INSTALL_DIR="/opt/mesh"
SETUP_SYSTEMD="false"
PYTHON_CMD=""

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${CYAN}[STEP]${NC}  $*"; }

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
        --node-id)          NODE_ID="$2";           shift 2 ;;
        --port)             PORT="$2";              shift 2 ;;
        --gateway)          GATEWAY_URL="$2";       shift 2 ;;
        --gateway-hostname) GATEWAY_HOSTNAME="$2";  shift 2 ;;
        --auth-token)       AUTH_TOKEN="$2";        shift 2 ;;
        --install-dir)      INSTALL_DIR="$2";       shift 2 ;;
        --systemd)          SETUP_SYSTEMD="true";   shift   ;;
        --help|-h)
            echo "Usage: setup.sh --node-id <id> [--port <port>] [--gateway <url>] [options]"
            echo ""
            echo "Required:"
            echo "  --node-id <id>        Unique node identifier (e.g., node-B)"
            echo ""
            echo "Optional:"
            echo "  --gateway <url>           Gateway URL (auto-detected when piped from gateway)"
            echo "  --port <port>             Listen port (default: 8080)"
            echo "  --auth-token <token>      Bearer token for authentication"
            echo "  --install-dir <path>      Installation directory (default: /opt/mesh)"
            echo "  --gateway-hostname <host> Public gateway hostname for downloading runtime"
            echo "  --systemd                 Create and enable systemd service"
            exit 0
            ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
done

# Validate required params
if [[ -z "$NODE_ID" ]]; then
    log_error "Missing required: --node-id"
    exit 1
fi

# Gateway URL: tự detect nếu script được serve từ gateway
if [[ "$GATEWAY_URL" == "__GATEWAY_PUBLIC_URL__" ]]; then
    # Placeholder chưa được inject → script chạy offline
    GATEWAY_URL=""
fi
if [[ -z "$GATEWAY_URL" ]]; then
    log_error "Missing --gateway. Examples:"
    log_error "  LAN cùng mạng: --gateway http://10.0.0.1:8080"
    log_error "  Khác mạng:     --gateway https://your-gateway.example.com"
    log_error ""
    log_error "Nếu bạn tải script từ gateway, URL sẽ tự inject."
    log_error "Thử: curl -sSL https://your-gateway/setup.sh | bash -s -- --node-id $NODE_ID"
    exit 1
fi
log_info "Gateway: $GATEWAY_URL"

# ---------------------------------------------------------------------------
# OS Detection
# ---------------------------------------------------------------------------
detect_os() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        OS_ID="${ID,,}"
        OS_VERSION="${VERSION_ID%%.*}"
        OS_NAME="${PRETTY_NAME}"
    else
        log_error "Cannot detect OS — /etc/os-release not found"
        exit 1
    fi

    case "$OS_ID" in
        debian|ubuntu|linuxmint|pop)    OS_FAMILY="debian" ;;
        centos|rhel|rocky|almalinux|fedora|ol) OS_FAMILY="rhel" ;;
        arch|manjaro)                   OS_FAMILY="arch" ;;
        alpine)                         OS_FAMILY="alpine" ;;
        *)
            log_warn "Unknown OS: $OS_ID — will try generic approach"
            OS_FAMILY="unknown"
            ;;
    esac

    log_info "Detected OS: $OS_NAME (family=$OS_FAMILY)"
}

# ---------------------------------------------------------------------------
# Python Installation
# ---------------------------------------------------------------------------
find_python() {
    # Tìm python3 >= 3.10 (bao gồm cả /usr/local/bin từ source build)
    for cmd in python3.12 python3.11 python3.10 python3 /usr/local/bin/python3.11 /usr/local/bin/python3.12; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
            local major minor
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [[ "$major" -ge 3 && "$minor" -ge 10 ]]; then
                PYTHON_CMD="$cmd"
                log_info "Found Python: $cmd ($ver)"
                return 0
            fi
        fi
    done
    return 1
}

install_python_from_source() {
    # Compile Python 3.11 từ source — dùng cho CentOS 7 hoặc bất kỳ OS nào
    # thiếu Python 3.10+ trong package manager
    log_step "Compiling Python 3.11 from source (this may take 5-10 minutes)..."

    # Cài build dependencies
    local PKG_MGR="dnf"
    if ! command -v dnf &>/dev/null; then
        PKG_MGR="yum"
    fi

    if [[ "$OS_FAMILY" == "rhel" ]]; then
        sudo $PKG_MGR groupinstall -y "Development Tools" 2>/dev/null || \
            sudo $PKG_MGR install -y gcc make 2>/dev/null || true
        sudo $PKG_MGR install -y \
            openssl-devel bzip2-devel libffi-devel zlib-devel \
            readline-devel sqlite-devel xz-devel 2>/dev/null || true
        # CentOS 7 có openssl 1.0 — Python 3.10+ cần openssl 1.1+
        # Cài openssl11 từ EPEL nếu cần
        if [[ "$OS_VERSION" == "7" ]]; then
            sudo $PKG_MGR install -y openssl11-devel 2>/dev/null || true
        fi
    elif [[ "$OS_FAMILY" == "debian" ]]; then
        sudo apt-get install -y build-essential libssl-dev zlib1g-dev \
            libbz2-dev libreadline-dev libsqlite3-dev libffi-dev \
            liblzma-dev 2>/dev/null || true
    fi

    local PY_VERSION="3.11.9"
    local PY_URL="https://www.python.org/ftp/python/${PY_VERSION}/Python-${PY_VERSION}.tgz"
    local BUILD_DIR="/tmp/python-build-$$"

    mkdir -p "$BUILD_DIR"
    cd "$BUILD_DIR"

    log_info "Downloading Python ${PY_VERSION}..."
    if ! curl -sSfL "$PY_URL" -o "Python-${PY_VERSION}.tgz"; then
        log_error "Failed to download Python source"
        exit 1
    fi

    tar xzf "Python-${PY_VERSION}.tgz"
    cd "Python-${PY_VERSION}"

    log_info "Configuring..."
    # CentOS 7: nếu có openssl11, dùng nó
    if [[ -d "/usr/include/openssl11" ]]; then
        export CFLAGS="-I/usr/include/openssl11"
        export LDFLAGS="-L/usr/lib64/openssl11"
    fi

    ./configure --enable-optimizations --prefix=/usr/local 2>&1 | tail -3
    log_info "Building (using $(nproc) cores)..."
    make -j"$(nproc)" 2>&1 | tail -3

    log_info "Installing..."
    sudo make altinstall 2>&1 | tail -3

    # Cleanup
    cd /
    rm -rf "$BUILD_DIR"

    # Verify
    if /usr/local/bin/python3.11 --version &>/dev/null; then
        PYTHON_CMD="/usr/local/bin/python3.11"
        # Tạo symlink cho tiện
        sudo ln -sf /usr/local/bin/python3.11 /usr/local/bin/python3 2>/dev/null || true
        sudo ln -sf /usr/local/bin/pip3.11 /usr/local/bin/pip3 2>/dev/null || true
        log_info "Python compiled and installed: $($PYTHON_CMD --version)"
    else
        log_error "Python compilation failed"
        exit 1
    fi
}

install_python() {
    log_step "Installing Python 3.11..."

    case "$OS_FAMILY" in
        debian)
            sudo apt-get update -qq
            # Thử python3.11 trước, nếu không có thì dùng python3 mặc định
            if sudo apt-get install -y python3.11 python3.11-venv python3-pip 2>/dev/null; then
                PYTHON_CMD="python3.11"
            elif sudo apt-get install -y python3 python3-venv python3-pip 2>/dev/null; then
                PYTHON_CMD="python3"
            else
                log_warn "No suitable Python in repos — compiling from source..."
                install_python_from_source
            fi
            ;;
        rhel)
            # CentOS / AlmaLinux / Rocky / RHEL
            # Phát hiện package manager: dnf (CentOS 8+) hoặc yum (CentOS 7)
            local PKG_MGR="dnf"
            if ! command -v dnf &>/dev/null; then
                PKG_MGR="yum"
                log_info "Using yum (CentOS 7 / older RHEL detected)"
            fi

            sudo $PKG_MGR install -y epel-release 2>/dev/null || true

            # Thử cài python3.11 từ repo
            if sudo $PKG_MGR install -y python3.11 python3.11-pip 2>/dev/null; then
                PYTHON_CMD="python3.11"
            elif sudo $PKG_MGR install -y python3 python3-pip 2>/dev/null; then
                PYTHON_CMD="python3"
            else
                # Fallback: compile Python 3.11 từ source (CentOS 7 thường cần cách này)
                log_warn "No suitable Python in repos — compiling Python 3.11 from source..."
                install_python_from_source
            fi
            ;;
        arch)
            sudo pacman -Sy --noconfirm python python-pip
            PYTHON_CMD="python3"
            ;;
        alpine)
            sudo apk add python3 py3-pip
            PYTHON_CMD="python3"
            ;;
        *)
            log_error "Cannot auto-install Python for OS family: $OS_FAMILY"
            log_error "Please install Python 3.10+ manually, then re-run this script."
            exit 1
            ;;
    esac

    log_info "Python installed: $PYTHON_CMD"
}

ensure_python() {
    if find_python; then
        return 0
    fi
    install_python
    if ! find_python; then
        log_error "Python 3.10+ not found after installation"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Download Runtime from Gateway
# ---------------------------------------------------------------------------
download_runtime() {
    log_step "Downloading mesh runtime..."

    local download_url="${GATEWAY_HOSTNAME:-$GATEWAY_URL}"
    local dest="$INSTALL_DIR"

    sudo mkdir -p "$dest"
    sudo chown "$(whoami)" "$dest"

    # Thử download bundle từ gateway
    local bundle_url="${download_url}/runtime-bundle"
    local tmp_bundle="/tmp/mesh-runtime-bundle.tar.gz"

    if curl -sSfL "$bundle_url" -o "$tmp_bundle" 2>/dev/null; then
        log_info "Downloaded runtime bundle from gateway"
        tar xzf "$tmp_bundle" -C "$dest"
        rm -f "$tmp_bundle"
    else
        log_warn "Could not download bundle from gateway — creating minimal runtime"
        log_info "You may need to manually copy the runtime/ and seed/ directories"
        create_minimal_runtime "$dest"
    fi
}

create_minimal_runtime() {
    local dest="$1"

    # Tạo requirements.txt tối thiểu
    cat > "$dest/requirements.txt" << 'REQEOF'
fastapi==0.115.6
uvicorn[standard]==0.34.0
httpx==0.28.1
pyyaml==6.0.2
pydantic==2.10.4
cachetools==5.5.1
jsonschema==4.23.0
openai==1.82.0
REQEOF

    log_warn "Minimal runtime created — please copy full runtime from gateway:"
    log_warn "  scp -r <gateway>:/path/to/mesh/{runtime,seed,node_runtime.py,pyproject.toml} $dest/"
}

# ---------------------------------------------------------------------------
# Setup Virtual Environment
# ---------------------------------------------------------------------------
setup_venv() {
    log_step "Setting up virtual environment..."

    cd "$INSTALL_DIR"

    if [[ ! -d "venv" ]]; then
        "$PYTHON_CMD" -m venv venv 2>/dev/null || "$PYTHON_CMD" -m venv --without-pip venv
    fi

    source venv/bin/activate

    # Đảm bảo pip tồn tại
    if ! command -v pip &>/dev/null; then
        curl -sSL https://bootstrap.pypa.io/get-pip.py | python3
    fi

    pip install --quiet -r requirements.txt
    log_info "Dependencies installed"
}

# ---------------------------------------------------------------------------
# Configure Node
# ---------------------------------------------------------------------------
configure_node() {
    log_step "Configuring node: $NODE_ID..."

    local node_dir="$INSTALL_DIR/$NODE_ID"
    mkdir -p "$node_dir/actions"

    # Copy seed actions nếu chưa có actions riêng
    if [[ -d "$INSTALL_DIR/seed/actions" ]]; then
        cp "$INSTALL_DIR/seed/actions/"*.py "$node_dir/actions/" 2>/dev/null || true
        cp "$INSTALL_DIR/seed/actions/"*.schema.json "$node_dir/actions/" 2>/dev/null || true
        log_info "Copied seed actions to $node_dir/actions/"
    fi

    # Tạo node.yaml
    local local_addr="http://127.0.0.1:$PORT"
    local auth_line=""
    if [[ -n "$AUTH_TOKEN" ]]; then
        auth_line="auth_token: $AUTH_TOKEN"
    else
        auth_line="# auth_token: null  # Không cần auth nếu chỉ gateway truy cập"
    fi

    cat > "$node_dir/node.yaml" << YAMLEOF
node_id: $NODE_ID
listen: 0.0.0.0:$PORT

nodes:
  $NODE_ID: $local_addr
  node-gateway: $GATEWAY_URL

default_resolver: node-gateway
max_hop: 10
cache_ttl_seconds: 300

$auth_line

job_ttl_seconds: 3600
cleanup_interval_seconds: 60
YAMLEOF

    # Tạo skills.md
    local os_info
    os_info=$(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d'"' -f2 || echo "Linux")

    cat > "$node_dir/skills.md" << SKILLEOF
# Node: $NODE_ID ($os_info)

## Available Actions
- **read_file**: Đọc file trên máy này
- **write_file**: Ghi file trên máy này
- **execute_command**: Chạy shell command trên máy này

## System Info
- OS: $os_info
- Hostname: $(hostname)
- Install dir: $INSTALL_DIR
SKILLEOF

    log_info "Node configured at $node_dir/"
}

# ---------------------------------------------------------------------------
# Start Node
# ---------------------------------------------------------------------------
start_node() {
    log_step "Starting node: $NODE_ID on port $PORT..."

    cd "$INSTALL_DIR"
    source venv/bin/activate

    local config_path="$INSTALL_DIR/$NODE_ID/node.yaml"
    local log_file="/tmp/$NODE_ID.log"
    local pid_file="/tmp/$NODE_ID.pid"

    # Dừng instance cũ nếu có
    if [[ -f "$pid_file" ]]; then
        local old_pid
        old_pid=$(cat "$pid_file")
        if kill -0 "$old_pid" 2>/dev/null; then
            log_info "Stopping old instance (pid=$old_pid)..."
            kill "$old_pid" 2>/dev/null || true
            sleep 2
        fi
    fi

    nohup "$INSTALL_DIR/venv/bin/python" node_runtime.py --config "$config_path" > "$log_file" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$pid_file"

    sleep 3

    # Verify
    if kill -0 "$new_pid" 2>/dev/null; then
        local health
        health=$(curl -sf "http://127.0.0.1:$PORT/health" 2>/dev/null || echo "")
        if echo "$health" | grep -q '"healthy"'; then
            log_info "Node $NODE_ID started successfully (pid=$new_pid)"
            log_info "Health: $health"
        else
            log_warn "Node started but health check failed — check log: $log_file"
        fi
    else
        log_error "Node failed to start — check log: $log_file"
        tail -20 "$log_file" 2>/dev/null
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Systemd Service
# ---------------------------------------------------------------------------
setup_systemd_service() {
    if [[ "$SETUP_SYSTEMD" != "true" ]]; then
        return 0
    fi

    log_step "Creating systemd service..."

    local service_name="mesh-$NODE_ID"
    local config_path="$INSTALL_DIR/$NODE_ID/node.yaml"

    sudo tee "/etc/systemd/system/${service_name}.service" > /dev/null << SVCEOF
[Unit]
Description=Mesh Node $NODE_ID
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python node_runtime.py --config $config_path
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

    sudo systemctl daemon-reload
    sudo systemctl enable "$service_name"

    # Dừng process thủ công, chuyển sang systemd
    local pid_file="/tmp/$NODE_ID.pid"
    if [[ -f "$pid_file" ]]; then
        kill "$(cat "$pid_file")" 2>/dev/null || true
        rm -f "$pid_file"
    fi

    sudo systemctl start "$service_name"
    sleep 2

    if sudo systemctl is-active --quiet "$service_name"; then
        log_info "Systemd service $service_name is active"
    else
        log_error "Systemd service failed — check: journalctl -u $service_name"
    fi
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
    echo ""
    echo -e "${CYAN}============================================${NC}"
    echo -e "${CYAN}  Mesh Node Setup Complete!${NC}"
    echo -e "${CYAN}============================================${NC}"
    echo ""
    echo -e "  Node ID:      ${GREEN}$NODE_ID${NC}"
    echo -e "  Port:         ${GREEN}$PORT${NC}"
    echo -e "  Install dir:  ${GREEN}$INSTALL_DIR${NC}"
    echo -e "  Config:       ${GREEN}$INSTALL_DIR/$NODE_ID/node.yaml${NC}"
    echo -e "  Gateway:      ${GREEN}$GATEWAY_URL${NC}"
    echo -e "  Log:          ${GREEN}/tmp/$NODE_ID.log${NC}"
    echo ""
    echo "  Test locally:"
    echo "    curl -s http://127.0.0.1:$PORT/health"
    echo ""
    echo "  Test from gateway:"
    echo "    curl -s -X POST <gateway>/action \\"
    echo "      -H 'Content-Type: application/json' \\"
    echo "      -H 'Authorization: Bearer <token>' \\"
    echo "      -d '{\"target_node_id\":\"$NODE_ID\",\"task_id\":\"test\",\"trace\":{\"hop_count\":0,\"route_path\":[]},\"payload\":{\"action\":\"execute_command\",\"params\":{\"command\":\"hostname\"}}}'"
    echo ""

    if [[ "$SETUP_SYSTEMD" == "true" ]]; then
        echo "  Systemd service:"
        echo "    sudo systemctl status mesh-$NODE_ID"
        echo "    sudo journalctl -u mesh-$NODE_ID -f"
        echo ""
    fi

    echo -e "${YELLOW}  IMPORTANT: Đảm bảo gateway đã thêm node này vào node.yaml:${NC}"
    echo "    nodes:"
    echo "      $NODE_ID: http://<this-machine-ip>:$PORT"
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    echo ""
    echo -e "${CYAN}Execution Mesh v5 — Node Setup${NC}"
    echo -e "${CYAN}==============================${NC}"
    echo ""

    detect_os
    ensure_python
    download_runtime
    setup_venv
    configure_node
    start_node
    setup_systemd_service
    print_summary
}

main

