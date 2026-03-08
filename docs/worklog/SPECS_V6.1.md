# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v6.1
### Gateway-as-Channel-Authority · Multi-Gateway Membership · Unified Node Identity

**Base version:** v6.0  
**Target version:** v6.1  
**Status:** Design / Pre-implementation  
**Authors:** Architecture review session, 2026-03-08

---

## 1. Từ v6.0 đến v6.1 — The conceptual shift

### 1.1 v6.0 nhìn lại

v6.0 thiết kế channel và gateway là **hai systems riêng biệt**:

```
v6.0 mental model:
  Gateway  = routing + job dispatch node
  Channel  = separate communication namespace
  
  Node muốn join team → phải làm HAI việc:
    1. gateway_node_id: gateway-A      ← đăng ký routing
    2. channels: [cluster-A, all-hands]   ← đăng ký channel (riêng)
  
  ChannelRegistry = separate module quản lý membership
  EventBus        = separate module quản lý events
  (hai modules không biết về nhau's state trực tiếp)
```

Thiết kế này functional nhưng có redundancy: node phải khai báo thông tin membership
ở hai chỗ. Quan trọng hơn, nó bỏ lỡ một insight cơ bản:

**Gateway node và channel manager là cùng một thứ.**

### 1.2 The key insight: Gateway IS the channel

Trong thực tế vận hành một tổ chức:

```
Cluster A có:  gateway-A (quản lý team)
            analyst-A, dev-A, test-A (thành viên)
            
Cluster B có:  gateway-B (quản lý team)  
            analyst-B, dev-B, test-B (thành viên)

dev-B được assign vào cả hai teams:
  → dev-B register với gateway-A
  → dev-B trở thành full member của cluster-A
  → không cần thêm bước nào khác
```

**Register with gateway = join that gateway's channel.**  
**Join channel = register with gateway.**  
**Chúng là một hành động, không phải hai.**

### 1.3 Nguyên tắc bình đẳng thành viên

v6.1 bỏ hoàn toàn khái niệm "guest" vs "native member" từ analysis trước đó.
Một node đã register với gateway thì là **full member** — có quyền:

- Nhận events của channel (subscribe)
- Phát events lên channel (emit)
- Nhận và execute jobs từ gateway
- Được list trong member directory của gateway

Không có second-class membership. Không có observer-only mode ở channel level
(access control nếu cần thì ở action level, đã có `caller_policies` từ v5.11).

### 1.4 Thay đổi so với v6.0

| Khía cạnh | v6.0 | v6.1 |
|-----------|------|------|
| Mô hình khái niệm | Gateway + Channel là hai systems | Gateway = Channel Authority |
| Join channel | Declare trong `channels:` config | Register với gateway |
| Multi-team membership | `networks:` với channel list | `additional_gateways:` đơn giản |
| ChannelRegistry | Module riêng, complex | Thin view của NodeRegistry |
| node.yaml complexity | Hai sections (gateway + channels) | Một section (gateways) |
| Member equality | Guest/native distinction (được xem xét) | Full equality — không phân biệt |
| New modules cần viết | ChannelRegistry (riêng) | Không cần module mới |

---

## 2. Revised architecture

### 2.1 Core model

```
┌─────────────────────────────────────────────────────────────────┐
│  Gateway Node = Channel Authority                               │
│                                                                 │
│  NodeRegistry  ←──  "who is registered here"                   │
│       │              = "who is in this channel"                 │
│       │                                                         │
│  EventBus      ←──  delivers to all registered nodes           │
│       │              (channel scope = registered node scope)   │
│       │                                                         │
│  JobQueue      ←──  jobs dispatched to registered nodes        │
│                                                                 │
│  Members (all equal):                                           │
│    analyst-A  [home]     dev-B  [multi-member]                  │
│    dev-A      [home]     pm-node [multi-member]                 │
│    test-A     [home]                                            │
└─────────────────────────────────────────────────────────────────┘
```

NodeRegistry không cần phân biệt "home" hay "multi-member" — đó là concern của node
tự biết về mình, không phải concern của gateway.

### 2.2 Multi-gateway node model

```
dev-B registered with:
  gateway-B (primary)  →  home team, job priority, heartbeat primary
  gateway-A (additional)  →  cross-team membership, full rights

From gateway-A's perspective: dev-B là một worker bình thường.
From gateway-B's perspective: dev-B là một worker bình thường.
dev-B tự biết mình có hai connections và manage chúng.
```

Không có concept "primary" hay "additional" ở phía gateway. Chỉ có ở phía node
(để node tự organize connections của mình).

### 2.3 Event scoping — tự nhiên từ registration

```
EventBus của gateway-A emit event:
  → fan-out đến tất cả nodes registered với gateway-A
  → bao gồm: analyst-A, dev-A, test-A, dev-B, pm-node
  → không bao gồm: analyst-B, dev-C (chưa register với gateway-A)

EventBus của gateway-B emit event:  
  → fan-out đến tất cả nodes registered với gateway-B
  → bao gồm: analyst-B, dev-B, test-B (dev-B cũng ở đây)
  → không bao gồm: analyst-A, dev-A
```

Channel isolation đến tự nhiên từ registration model — không cần ChannelRegistry riêng.

---

## 3. Changes to v6.0 specs

### 3.1 Module A: ChannelRegistry — SIMPLIFIED

v6.0 thiết kế ChannelRegistry là một module độc lập với full CRUD, data models,
và separate storage. v6.1 simplify:

**ChannelRegistry không còn là một module riêng.** Nó là một thin interface
trên NodeRegistry:

```python
# runtime/channel_registry.py — v6.1 (simplified)

class ChannelRegistry:
    """
    Thin view of NodeRegistry from a channel membership perspective.
    
    In v6.1, "channel membership" = "registered with this gateway node".
    No separate storage. No separate membership management.
    All operations delegate to NodeRegistry.
    
    Kept as a class for API clarity and potential future extension,
    but contains no independent state.
    """

    def __init__(self, node_registry: NodeRegistry, local_node_id: str) -> None:
        self._registry = node_registry
        self._channel_id = local_node_id   # gateway's node_id IS the channel_id

    def get_members(self) -> list[str]:
        """Return all node_ids registered with this gateway."""
        return self._registry.get_online_node_ids()

    def is_member(self, node_id: str) -> bool:
        """Check if node is registered (= is channel member)."""
        return self._registry.get_status(node_id) is not None

    def get_channel_id(self) -> str:
        """Channel ID = this gateway's node_id."""
        return self._channel_id
```

**Kết quả:** ChannelRegistry từ ~150 LOC module riêng → ~30 LOC thin wrapper.
Không có separate `_channels` dict, không có `Channel` dataclass, không có join/leave
API (thay thế bằng register/unregister của NodeRegistry đã có).

### 3.2 node.yaml — v6.1 schema

Thay thế hai concepts (`gateway_node_id` + `channels`) bằng một concept duy nhất:

```yaml
# ── Core identity (không đổi từ v5.x) ────────────────────────────────────
node_id: dev-B
listen: 0.0.0.0:8083

# ── Primary gateway (không đổi — backward compat với v5.x) ──────────────
gateway_node_id: gateway-B
gateway_address: https://gateway-b.vietml.com
auth_token: tok-dev-b-on-b

# ── v6.1 NEW: Additional gateways (multi-team membership) ────────────────
additional_gateways:
  - address: https://gateway-a.vietml.com
    auth_token: tok-dev-b-on-a
    # Không cần khai báo channels — register = join full channel
    # Không cần khai báo role — full member by default

# ── v6.0 channels: field — REMOVED ──────────────────────────────────────
# KHÔNG CÒN DÙNG:
#   channels:
#     - cluster-A
#     - all-hands
# Thay thế bởi: additional_gateways list ở trên
```

**Gateway node config — không thay đổi:**

```yaml
# gateway-A/node.yaml — không cần field mới

node_id: gateway-A
listen: 0.0.0.0:8090
auth_token: tok-gateway-a

trusted_nodes:
  - analyst-A
  - dev-A
  - test-A
  # dev-B và pm-node KHÔNG cần list ở đây — họ tự register vào
  # trusted_nodes chỉ cần list nodes đã pre-configured

# Không cần:
#   channel_name: cluster-A       ← không cần, node_id là channel identity
#   invited_nodes: [...]       ← không cần, open registration
```

**Lưu ý về `trusted_nodes`:** Hiện tại `trusted_nodes` là whitelist — chỉ nodes
trong list mới được register. Với multi-gateway model, gateway-A cần accept
registrations từ dev-B và pm-node mà không cần pre-configure. Xem Section 3.4
về open registration.

### 3.3 WorkerAgent — multi-gateway refactor

Đây là thay đổi code duy nhất đáng kể trong v6.1.

**Hiện tại (v5.13b):**
```python
class WorkerAgent:
    def __init__(self, config, executor):
        self._gateway_url = config.gateway_address   # single string
        # ... single set of loops
    
    async def start(self):
        await self._register_with_retry()
        asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._poll_loop())
        asyncio.create_task(self._reregister_loop())
```

**v6.1:**
```python
@dataclass
class GatewayConnectionConfig:
    """Config for one gateway connection."""
    address: str
    auth_token: str
    is_primary: bool = False

class GatewayConnection:
    """
    Manages register + heartbeat + poll lifecycle for ONE gateway.
    
    Extracted from WorkerAgent — same logic, parameterized per gateway.
    Primary gateway: full existing behavior.
    Additional gateways: same behavior, different auth token + URL.
    
    No special treatment between primary and additional connections —
    both run identical loops, both can dispatch jobs to this node.
    The "primary" flag is informational only (used in health reporting).
    """

    def __init__(
        self,
        config: GatewayConnectionConfig,
        node_config: NodeConfig,
        executor: ActionExecutor,
        action_registry: dict,
        schema_registry: dict,
    ) -> None:
        self._gateway_url = config.address.rstrip("/")
        self._auth_headers = {"Authorization": f"Bearer {config.auth_token}"}
        self._is_primary = config.is_primary
        self._node_id = node_config.node_id
        self._self_address = node_config.self_address
        self._executor = executor
        self._action_registry = action_registry
        self._schema_registry = schema_registry
        self._heartbeat_interval = node_config.heartbeat_interval_seconds
        self._poll_interval = node_config.poll_interval_seconds
        # v6.0 adaptive polling fields
        self._current_poll_interval = node_config.poll_interval_seconds
        self._poll_max = getattr(node_config, "poll_interval_max_seconds", 60)
        self._poll_backoff = getattr(node_config, "poll_backoff_multiplier", 1.5)

    async def start(self) -> None:
        """Register then start heartbeat + poll + reregister loops."""
        await self._register_with_retry()
        asyncio.gather(
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._poll_loop()),
            asyncio.create_task(self._reregister_loop()),
        )

    async def stop(self) -> None:
        """Cancel all loops."""

    # _register, _heartbeat_loop, _poll_loop, _reregister_loop
    # — identical to current WorkerAgent private methods,
    #   just moved into this class


class WorkerAgent:
    """
    v6.1: Manages multiple GatewayConnection instances.
    
    One connection per gateway — primary + any additional_gateways.
    All connections are equal — same loops, same job handling.
    """

    def __init__(
        self,
        config: NodeConfig,
        executor: ActionExecutor,
        action_registry: dict | None = None,
        schema_registry: dict | None = None,
    ) -> None:
        self._connections: list[GatewayConnection] = []
        
        # Primary gateway (existing config fields — backward compat)
        if config.gateway_address:
            primary_token = getattr(config, "gateway_auth_token", None) or config.auth_token
            self._connections.append(GatewayConnection(
                config=GatewayConnectionConfig(
                    address=config.gateway_address,
                    auth_token=primary_token or "",
                    is_primary=True,
                ),
                node_config=config,
                executor=executor,
                action_registry=action_registry or {},
                schema_registry=schema_registry or {},
            ))
        
        # Additional gateways (v6.1 new)
        for gw in getattr(config, "additional_gateways", []):
            self._connections.append(GatewayConnection(
                config=GatewayConnectionConfig(
                    address=gw["address"],
                    auth_token=gw["auth_token"],
                    is_primary=False,
                ),
                node_config=config,
                executor=executor,
                action_registry=action_registry or {},
                schema_registry=schema_registry or {},
            ))

    async def start(self) -> None:
        """Start all gateway connections concurrently."""
        await asyncio.gather(*[conn.start() for conn in self._connections])

    async def stop(self) -> None:
        await asyncio.gather(*[conn.stop() for conn in self._connections])
    
    # Sub-node advertisement (v5.10) — advertise to ALL connected gateways
    async def advertise_sub_node(self, sub_node_info: SubRouteInfo) -> None:
        """Re-register with all gateways to advertise new sub-node route."""
        await asyncio.gather(*[
            conn.reregister_with_routes(sub_node_info)
            for conn in self._connections
        ])
```

### 3.4 Open registration policy

Với multi-gateway model, gateway-A cần accept registration từ nodes không có
trong `trusted_nodes`. Cần một policy mới:

```yaml
# gateway-A/node.yaml

# v5.x model: strict whitelist
trusted_nodes:
  - analyst-A
  - dev-A
  - test-A

# v6.1: registration policy
registration_policy: open       # open | whitelist | invite_only
                                # "open": any node can register
                                # "whitelist": only trusted_nodes (v5.x behavior, default)
                                # "invite_only": node must present invite_token
```

**`open` policy:** Gateway accept any authenticated node (valid auth token is sufficient).
Nếu gateway có `allowed_tokens` (v5.13b) thì node vẫn phải có valid token —
authentication vẫn required, chỉ authorization policy thay đổi.

**`whitelist` policy (default):** Giữ nguyên v5.x behavior. Backward compat.

**`invite_only` policy (P3):** Gateway cấp invite tokens cho các nodes được mời.
Node register kèm invite token. Gateway validate token và admit node.

```yaml
# invite_only example
registration_policy: invite_only
invite_tokens:
  - token: "inv-dev-b-xyz"
    for_node: dev-B        # optional hint, not enforced
    expires_at: 1741392000
    single_use: true
```

### 3.5 EventBus — minimal change from v6.0

EventBus không thay đổi logic. Thay đổi duy nhất: khi emit event và fan-out,
EventBus hỏi `ChannelRegistry.get_members()` (v6.1: thin wrapper of NodeRegistry)
thay vì maintain separate member list.

```python
# EventBus.emit() — v6.1
async def emit(self, event: Event) -> int:
    self._event_log.append(event)
    
    # Get current channel members from NodeRegistry (via ChannelRegistry)
    channel_members = self._channel_registry.get_members()
    
    # Find matching subscriptions for these members
    matching = [
        sub for sub in self._subscriptions.values()
        if sub.subscriber_node in channel_members
           and self._match_subscription(event, sub)
    ]
    
    for sub in matching:
        await self._delivery_queue.put((event, sub))
    
    return len(matching)
```

Không còn check `channel_id` trên event để match subscription —
scope đã được enforce bởi NodeRegistry membership. Node chỉ nhận events
từ gateways mà nó đã register.

**Implication:** Nếu dev-B muốn nhận events từ gateway-A, dev-B subscribe
trên **gateway-A's EventBus** (thông qua `POST /subscribe` đến gateway-A).
Gateway-A's EventBus check: dev-B có phải là registered member? → Có → accept subscription.

---

## 4. Revised HTTP Endpoints

### Endpoints REMOVED từ v6.0

| v6.0 Endpoint | Lý do remove |
|---------------|-------------|
| `POST /channels/{id}/join` | Thay bởi `POST /nodes/register` (đã có) |
| `POST /channels/{id}/leave` | Thay bởi node de-registration (existing) |
| `GET /channels` | Thay bởi `GET /nodes` (đã có, list registered nodes) |
| `GET /channels/{id}/members` | Thay bởi `GET /nodes` |

### Endpoints KEPT từ v6.0 (không đổi)

Tất cả EventBus và Scheduler endpoints giữ nguyên:
- `POST /emit`
- `POST /channels/{channel_id}/emit` → v6.1: `POST /emit` với `channel_id = gateway's node_id`
- `POST /subscribe`, `DELETE /subscriptions/{id}`, `GET /subscriptions`
- `GET /events`
- `POST /schedule`, `DELETE /schedule/{id}`, `GET /schedule`
- `POST /schedule/{id}/trigger`, `PATCH /schedule/{id}`

### Endpoints MODIFIED

#### `GET /nodes` — extended with channel info

```
Response 200 (v6.1 addition):
{
  "channel_id": "gateway-A",        // NEW: this gateway's channel identity
  "channel_description": "...",     // NEW: from node.yaml
  "registration_policy": "open",    // NEW
  "members": [
    {
      "node_id": "analyst-A",
      "status": "online",
      "actions": [...],
      "registered_at": 1741392000.0,
      "address": "http://analyst-a:8091"
    },
    {
      "node_id": "dev-B",
      "status": "online",
      "actions": [...],
      "registered_at": 1741392500.0  // joined later — multi-team member
    }
  ]
}
```

#### `POST /nodes/register` — unchanged behavior, new semantics

Không thay đổi request/response format. Semantics thay đổi:

> Registering with a gateway = joining that gateway's channel as a full member.

---

## 5. `node.yaml` schema — v6.1 complete

```yaml
# ── Core identity ─────────────────────────────────────────────────────────
node_id: dev-B
listen: 0.0.0.0:8083

# ── Primary gateway (v5.x fields — unchanged) ─────────────────────────────
gateway_node_id: gateway-B
gateway_address: https://gateway-b.vietml.com
auth_token: tok-dev-b-on-b          # incoming + outbound token (v5.x)
gateway_auth_token: tok-dev-b-on-b  # outbound only, overrides auth_token (v5.13b)

# ── v6.1 NEW: Additional gateways ─────────────────────────────────────────
additional_gateways:
  - address: https://gateway-a.vietml.com
    auth_token: tok-dev-b-on-a      # token này do gateway-A cấp

# ── v6.1: Channel identity (gateway nodes only) ───────────────────────────
channel_description: "Frontend dev team"   # optional display info
registration_policy: open                   # open | whitelist | invite_only

# ── v6.0 fields — UNCHANGED ───────────────────────────────────────────────
event_bus:
  enabled: true
  max_log_size: 10000
  delivery_timeout_seconds: 10
  delivery_retry_count: 3
  delivery_retry_backoff: 2.0

scheduler:
  enabled: true

schedule:
  - trigger_type: condition
    run_action: start_analysis
    check_action: analyst_self_check
    check_interval_seconds: 60

poll_interval_seconds: 5
poll_interval_max_seconds: 60
poll_backoff_multiplier: 1.5

# ── REMOVED in v6.1 ───────────────────────────────────────────────────────
# channels: [...]   ← không còn dùng, replaced by additional_gateways
```

---

## 6. `config.py` additions — v6.1

```python
# runtime/config.py — v6.1 additions

@dataclass(frozen=True)
class AdditionalGatewayConfig:
    """Config for one additional gateway connection."""
    address: str
    auth_token: str

@dataclass(frozen=True)
class NodeConfig:
    # ... all existing fields unchanged ...
    
    # v6.1 NEW
    additional_gateways: tuple[AdditionalGatewayConfig, ...] = field(default_factory=tuple)
    channel_description: str = ""
    registration_policy: str = "whitelist"   # "open" | "whitelist" | "invite_only"

# Parse from node.yaml:
additional_gateways=tuple(
    AdditionalGatewayConfig(
        address=gw["address"],
        auth_token=gw["auth_token"],
    )
    for gw in raw.get("additional_gateways", [])
),
channel_description=raw.get("channel_description", ""),
registration_policy=raw.get("registration_policy", "whitelist"),
```

---

## 7. NodeRegistry — registration policy enforcement

```python
# runtime/node_registry.py — v6.1 addition

class NodeRegistry:
    # ... existing code ...

    async def register(
        self,
        node_id: str,
        address: str | None,
        actions: list[str],
        action_specs: dict,
        registration_policy: str = "whitelist",
        invite_token: str | None = None,
    ) -> bool:
        """
        Register a node. Returns True if accepted, False if rejected by policy.
        
        Policy enforcement:
          "whitelist": node_id must be in trusted_nodes (v5.x behavior)
          "open": any node accepted (auth token already validated by AuthMiddleware)
          "invite_only": invite_token must be valid and not expired
        """
        if registration_policy == "whitelist":
            if node_id not in self._trusted_node_ids:
                logger.warning("Registration rejected: %s not in trusted_nodes", node_id)
                return False
        elif registration_policy == "invite_only":
            if not self._validate_invite_token(invite_token, node_id):
                logger.warning("Registration rejected: invalid invite_token for %s", node_id)
                return False
        # "open": always accept (auth already validated)
        
        # ... existing registration logic ...
        return True
```

**Server endpoint update:**

```python
# runtime/server.py — POST /nodes/register v6.1

@app.post("/nodes/register")
async def register_node(req: NodeRegistrationRequest):
    accepted = await node_registry.register(
        node_id=req.node_id,
        address=req.address,
        actions=req.actions,
        action_specs=req.action_specs or {},
        registration_policy=config.registration_policy,   # from node.yaml
        invite_token=req.invite_token,                     # optional
    )
    if not accepted:
        return JSONResponse({"error": "Registration rejected by policy"}, status_code=403)
    # ... rest unchanged
```

---

## 8. AI Dev Team scenario — v6.1

### Setup

```yaml
# gateway-A/node.yaml
node_id: gateway-A
registration_policy: open
channel_description: "Frontend dev team"

# gateway-B/node.yaml  
node_id: gateway-B
registration_policy: open
channel_description: "Backend dev team"

# dev-B/node.yaml — multi-team member
node_id: dev-B
gateway_node_id: gateway-B
gateway_address: https://gateway-b.vietml.com
auth_token: tok-dev-b-on-b
additional_gateways:
  - address: https://gateway-a.vietml.com
    auth_token: tok-dev-b-on-a

# pm-node/node.yaml — watches both teams
node_id: pm-node
gateway_node_id: gateway-A
gateway_address: https://gateway-a.vietml.com
auth_token: tok-pm-on-a
additional_gateways:
  - address: https://gateway-b.vietml.com
    auth_token: tok-pm-on-b
```

### Runtime behavior

```
Startup:
  dev-B registers with gateway-B → member of cluster-B channel
  dev-B registers with gateway-A → member of cluster-A channel
  pm-node registers with gateway-A → member of cluster-A channel
  pm-node registers with gateway-B → member of cluster-B channel

Event flow — cluster-A:
  analyst-A emits "stories.ready" on gateway-A
  → gateway-A's EventBus fans out to: [analyst-A, dev-A, test-A, dev-B, pm-node]
  → architect-A (subscribed to stories.ready) reacts
  → dev-B receives event (cross-team visibility)
  → pm-node receives event (management visibility)

Event flow — cluster-B:
  dev-B emits "code.ready" on gateway-B
  → gateway-B's EventBus fans out to: [analyst-B, dev-B, test-B, pm-node]
  → pm-node receives (cross-team PM watching both)
  → cluster-A nodes do NOT receive (correctly isolated)

Job dispatch:
  gateway-A assigns job to dev-B → dev-B's connection to gateway-A polls it
  gateway-B assigns job to dev-B → dev-B's connection to gateway-B polls it
  dev-B executes both jobs locally (same executor, different source gateways)
```

---

## 9. Implementation plan — v6.1 delta from v6.0

v6.1 giữ nguyên toàn bộ P0→P3 plan từ v6.0. Delta chỉ là:

### P0 delta (simplification)

| v6.0 P0 | v6.1 P0 |
|---------|---------|
| Implement ChannelRegistry module (~300 LOC) | ChannelRegistry = thin wrapper (~30 LOC) |
| 5 new channel endpoints | 0 new channel endpoints (use existing /nodes) |
| `channels:` field in config | Không cần |
| ChannelRegistry unit tests | Minimal (delegates to NodeRegistry) |

**v6.1 P0 replaces with:**
- `registration_policy` field in NodeConfig + NodeRegistry enforcement (~80 LOC)
- `additional_gateways` field in NodeConfig (~20 LOC)
- `GatewayConnection` class extracted from WorkerAgent (~200 LOC refactor)
- WorkerAgent multi-connection support (~50 LOC)

Net code change: **−350 LOC** so với v6.0 P0 (simpler, not more complex).

### New file: `runtime/gateway_connection.py`

```
Extracted from worker_agent.py:
  class GatewayConnectionConfig
  class GatewayConnection
    - _register()
    - _register_with_retry()
    - _heartbeat_loop()
    - _poll_loop()
    - _reregister_loop()
    - _send_heartbeat()
    - _claim_and_execute_job()
    - _report_job_result()
```

WorkerAgent trở thành orchestrator đơn giản: tạo N `GatewayConnection` instances
và call `start()` trên tất cả.

### Files thay đổi — v6.1 delta

| File | v6.0 action | v6.1 action | Note |
|------|------------|------------|------|
| `runtime/channel_registry.py` | NEW (complex) | NEW (thin wrapper, ~30 LOC) | Major simplification |
| `runtime/gateway_connection.py` | N/A | NEW (~250 LOC) | Extracted từ worker_agent |
| `runtime/worker_agent.py` | No change | MODIFY (use GatewayConnection) | Refactor, not new logic |
| `runtime/node_registry.py` | No change | MODIFY (registration_policy) | ~80 LOC addition |
| `runtime/config.py` | MODIFY (channels field) | MODIFY (additional_gateways, policy) | Replace channels với simpler model |
| `runtime/models.py` | MODIFY (Channel models) | MODIFY (remove Channel models, add AdditionalGateway) | Fewer new models |
| `runtime/server.py` | MODIFY (channel endpoints) | MODIFY (remove channel endpoints, update /nodes) | Fewer endpoints |
| `runtime/event_bus.py` | NEW | NEW (same) | No change vs v6.0 |
| `runtime/scheduler.py` | NEW | NEW (same) | No change vs v6.0 |

---

## 10. Backward compatibility

v6.1 là **additive** và **backward compatible** với v5.13b và v6.0:

| Scenario | Behavior |
|----------|----------|
| Node không có `additional_gateways` | Single gateway mode — identical to v5.13b |
| Gateway với `registration_policy` không set | Default `"whitelist"` — identical to v5.13b |
| v6.0 config có `channels:` field | Ignored với deprecation warning; membership now via registration |
| `trusted_nodes` list vẫn hoạt động | Còn dùng khi `registration_policy: whitelist` (default) |
| Node không có `event_bus:` section | EventBus disabled — no change |
| Node không có `scheduler:` section | Scheduler disabled — no change |

---

## 11. Conceptual evolution summary

```
v5.x:   Node → (registers with) → Gateway
         ↑                              ↑
         worker                     routing hub

v6.0:   Node → (registers with) → Gateway
        Node → (joins) → Channel (separate system)
         ↑
         TWO separate things to manage

v6.1:   Node → (registers with) → Gateway = Channel Authority
                                       ↑
                               ONE action, dual meaning
                               register = join channel
                               
        Node can register with MULTIPLE gateways
        → member of multiple channels
        → full rights in all channels
        → no guest/native distinction
```

---

## 12. Trạng thái hệ thống sau v6.1 (full)

Inherits tất cả từ v6.0 Section 13, cộng thêm:

```
Multi-team membership:
  ✅ Node register với nhiều gateways (v6.1)
  ✅ Full member rights tại mọi gateway (v6.1)
  ✅ Receive events từ tất cả registered gateways (v6.1)
  ✅ Emit events tại bất kỳ registered gateway (v6.1)
  ✅ Receive jobs từ tất cả registered gateways (v6.1)
  ✅ Open/whitelist/invite_only registration policies (v6.1)

Channel architecture:
  ✅ Gateway IS the channel authority (v6.1 — conceptual unification)
  ✅ No guest/native distinction (v6.1 — full equality)
  ✅ Channel scope = registered node scope (v6.1 — no separate ChannelRegistry)
  ❌ Cross-gateway event propagation without bridge node (P4 — gateway federation)
  ❌ Invite token system (P3 — invite_only policy)
```

---

*Spec: SPECS_V6.1.md | Mesh Runtime v6.1 | Repository: ai-infra-runtime-v2*
*Builds on: SPECS_V6.0.md*
*Previous version retained at: gnot/docs/worklog/SPECS_V6.0.md*
