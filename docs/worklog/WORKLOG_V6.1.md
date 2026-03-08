# Worklog — Execution Mesh v6.1
## Gateway-as-Channel-Authority · Multi-Gateway Membership · Unified Node Identity

**Project:** ai-infra-runtime-v2  
**Base:** v6.0 → v6.1  
**Session:** Architecture review, 2026-03-08  
**Status:** Design complete, pre-implementation

---

## 1. Trigger

Câu hỏi trong review session:

> "Trong thực tế, một developer/manager có thể tham gia nhiều hơn một team,
> và do đó sẽ có access được đến nhiều communication channel. Tức một node
> có thể tham gia nhiều hơn một network."

> "Anh nghĩ không nên quá phân biệt 'khách' hay 'người nhà'. Một thành viên
> tham gia vào nhóm thì phải có quyền công bằng, vẫn có thể emit event bình thường."

Hai yêu cầu rõ ràng:
1. Node có thể join nhiều channels/networks
2. Full membership equality — không có guest/observer distinction

---

## 2. Analysis của v6.0 model

### 2.1 Vấn đề được phát hiện

Đọc lại v6.0 spec với lens của hai yêu cầu trên, phát hiện một **structural issue**:

v6.0 thiết kế channel và gateway là hai khái niệm tách nhau:
- `ChannelRegistry` module riêng với ~150 LOC, own data structures
- Node phải declare channels trong `channels:` config (separate từ gateway config)
- EventBus check channel_id trên event để scope delivery

Nhưng trong thực tế vận hành, khi một dev team muốn isolate communication của mình,
họ không cần hai things — họ chỉ cần **một gateway riêng**. Gateway chính là
channel boundary tự nhiên.

### 2.2 The insight

Thử hỏi: "Làm thế nào để dev-B join team-A's channel?"

Trong v6.0:
```
1. dev-B thêm "team-A" vào channels: list trong node.yaml
2. dev-B register với gateway-A
```

Nhưng bước 1 và bước 2 là redundant. Nếu dev-B đã register với gateway-A,
nó hiển nhiên là thành viên của team-A. Tại sao phải khai báo lại?

**Kết luận:** `register with gateway` và `join channel` là cùng một hành động.
Không nên model chúng là hai operations riêng.

---

## 3. Design decisions

### 3.1 "Gateway IS the channel" — không phải "gateway HAS a channel"

**Option A:** Gateway có một channel object được associate với nó
```python
class Gateway:
    channel: Channel   # gateway "has" a channel
```

**Option B:** Gateway là channel authority, không cần separate Channel object
```python
# gateway's node_id IS the channel_id
# NodeRegistry membership IS channel membership
# No separate Channel object needed
```

**Quyết định: Option B.**

Lý do: Option A vẫn duy trì sự tách biệt khái niệm không cần thiết. Option B
là complete unification — đơn giản hơn và không có redundancy.

Practical implication: `ChannelRegistry` từ independent module → thin wrapper của NodeRegistry.
Tất cả "channel membership" queries delegate đến `NodeRegistry.get_online_node_ids()`.

### 3.2 Full membership equality

Review session confirm: không có guest/observer distinction.

Trong analysis trước, em đã propose `mode: observer` cho additional gateways.
Anh reject điều này. Reasoning của anh đúng:

- Observer mode tạo ra second-class citizenship không cần thiết
- Access control ở channel level là wrong abstraction — nếu cần granular control,
  đó là concern của `caller_policies` (v5.11, action level), không phải channel level
- Real teams không có "read-only member" concept — nếu join team thì có quyền ngang nhau

**Kết quả:** `additional_gateways` config không có `mode` field. Register = full member.

### 3.3 Multi-gateway: node.yaml design

**Option A: Flat list**
```yaml
gateways:
  - address: https://gateway-a.vietml.com
    auth_token: tok-on-a
    primary: true
  - address: https://gateway-b.vietml.com  
    auth_token: tok-on-b
```

**Option B: Primary + additional (chosen)**
```yaml
# Primary — existing v5.x fields
gateway_node_id: gateway-B
gateway_address: https://gateway-b.vietml.com
auth_token: tok-on-b

# Additional — new
additional_gateways:
  - address: https://gateway-a.vietml.com
    auth_token: tok-on-a
```

**Quyết định: Option B.**

Lý do: Backward compatibility. Toàn bộ v5.x configs vẫn hoạt động không đổi.
`additional_gateways` là purely additive. Với Option A, mọi existing config
phải được migrate sang format mới.

Note quan trọng: "primary" vs "additional" là **node-side concept** để manage
connections — không có meaning ở phía gateway. Gateway-A không biết dev-B
consider nó là "primary" hay "additional". Tất cả connections equal ở phía gateway.

### 3.4 GatewayConnection extraction

Hiện tại `WorkerAgent` chứa tất cả gateway communication logic:
`_register`, `_heartbeat_loop`, `_poll_loop`, `_reregister_loop`, etc.

Với multi-gateway, cần chạy các loops này cho mỗi gateway. Hai approaches:

**Option A: Parameterize WorkerAgent methods**
```python
async def _heartbeat_loop(self, gateway_url: str, auth_headers: dict):
    while True:
        await self._send_heartbeat(gateway_url, auth_headers)
        await asyncio.sleep(self._heartbeat_interval)
```

**Option B: Extract GatewayConnection class (chosen)**
```python
class GatewayConnection:
    def __init__(self, config, ...): ...
    async def start(self): ...  # runs all loops
    async def stop(self): ...
```

**Quyết định: Option B.**

Lý do: Single Responsibility Principle. WorkerAgent hiện tại đã khá large.
Extracting GatewayConnection:
- Tách rõ connection lifecycle management
- Testable independently
- WorkerAgent trở thành simple orchestrator: `[conn.start() for conn in connections]`
- Code reuse: primary và additional connections dùng cùng class, không duplicate

### 3.5 Registration policy

Khi `registration_policy: open`, bất kỳ node authenticated nào đều được register.
Điều này raise câu hỏi: **open registration có an toàn không?**

Authentication vẫn required — `AuthMiddleware` (v5.13b) enforce Bearer token.
"Open" policy chỉ có nghĩa là node không cần có trong `trusted_nodes` whitelist.
Node vẫn phải có valid token trong gateway's `allowed_tokens`.

Workflow thực tế:
```
1. Operator gateway-A tạo auth token cho dev-B: "tok-dev-b-on-a"
2. Operator add "tok-dev-b-on-a" vào gateway-A's allowed_tokens
3. dev-B config: additional_gateways với auth_token: tok-dev-b-on-a
4. dev-B register → AuthMiddleware pass → registration_policy check pass
```

Token provisioning là access control mechanism. Registration policy chỉ là
secondary check về whether node_id must be in whitelist.

**Default là "whitelist"** để backward compat với mọi existing deployment.
Operators phải explicitly set `registration_policy: open` để allow cross-team joins.

### 3.6 ChannelRegistry: kill or keep?

Với gateway = channel authority, có thể argue không cần `ChannelRegistry` class
nào cả — EventBus có thể gọi trực tiếp vào NodeRegistry.

**Option A: Remove ChannelRegistry entirely**
```python
# EventBus gọi trực tiếp
members = node_registry.get_online_node_ids()
```

**Option B: Keep as thin wrapper (chosen)**
```python
class ChannelRegistry:
    def __init__(self, node_registry): ...
    def get_members(self): return self._registry.get_online_node_ids()
```

**Quyết định: Option B.**

Lý do:
- API clarity — EventBus explicitly depends on "channel" concept, not "node registry"
- Future extension point — nếu sau này cần channel-specific logic (e.g. channel metadata,
  per-channel event filtering), có chỗ để add mà không break EventBus
- Documentation value — `ChannelRegistry` tên rõ ràng về purpose

Nhưng implementation minimal: ~30 LOC, no own state, pure delegation.

---

## 4. What v6.1 reveals about v6.0

v6.0 thiết kế ChannelRegistry như một independent system vì lúc đó chưa có
insight "gateway IS channel". Đây là natural design evolution:

```
v6.0 thinking: "Channel là một communication abstraction.
                Gateway là một routing abstraction.
                Chúng khác nhau về nature → model riêng."

v6.1 thinking: "Channel scope = who is registered with this gateway.
                Channel membership = gateway registration.
                Chúng là cùng một thing → unify."
```

Đây không phải v6.0 "sai" — đó là progressive refinement. v6.0 established
event-driven foundation. v6.1 tìm ra simplification sau khi có use case cụ thể
(multi-team membership).

Đây là lý do tại sao giữ cả hai specs: v6.0 document the journey, v6.1 document
the destination. Future readers có thể thấy tại sao decisions được đưa ra.

---

## 5. Edge cases và open questions

### 5.1 Node unregister từ một gateway

Khi node leave một gateway (graceful shutdown hoặc explicit leave):
- NodeRegistry mark node UNREACHABLE sau heartbeat timeout
- EventBus deliveries đến node đó fail → retry → dead-letter
- Subscriptions của node đó vẫn còn trong EventBus

**Question:** Khi node re-register, subscriptions cũ có được restore không?

**Recommendation:** Subscriptions không auto-restore. Node phải re-subscribe.
Rationale: node có thể re-register với different capabilities hoặc different
subscription needs. Auto-restore có thể deliver stale events.

Nhưng: event log còn đó (`GET /events`). Node có thể replay missed events
khi reconnect. Đây là explicit pull, không phải automatic delivery.

### 5.2 Job dispute: hai gateways assign cùng job type cho cùng node

dev-B có thể nhận "write_code" job từ cả gateway-A và gateway-B simultaneously.
Không có conflict vì:
- Mỗi job có unique job_id
- JobQueue của mỗi gateway độc lập
- dev-B's executor handle từng job independently
- Không có shared mutable state giữa jobs

Risk: node bị overload. Giải pháp: `max_concurrent` trong Scheduler (v6.0) và
job throttling trong action executor. Separate concern, không cần address ở routing layer.

### 5.3 Event emitted trên gateway-A có reach gateway-B subscribers không?

**Không.** Đây là intentional isolation.

```
dev-B emit event trên gateway-A:
  → gateway-A's EventBus fans out to gateway-A members
  → [analyst-A, dev-A, test-A, dev-B, pm-node] nếu subscribed
  → gateway-B members không nhận (analyst-B, test-B)
  → pm-node nhận (nó là member của cả hai)

dev-B emit event trên gateway-B:
  → gateway-B's EventBus fans out to gateway-B members
  → [analyst-B, dev-B, test-B, pm-node] nếu subscribed
  → gateway-A members không nhận
```

Nếu muốn cross-gateway event propagation, cần bridge node (một node là member
của cả hai gateways, subscribe event ở A và re-emit ở B). PM-node có thể đóng
vai này. Đây là explicit, controlled bridge — không phải automatic federation.

### 5.4 Auth token management cho additional_gateways

Mỗi `additional_gateways` entry cần `auth_token` riêng. Đây là operator concern:
- gateway-A operator cấp token cho dev-B
- Token được add vào gateway-A's `allowed_tokens`
- dev-B config token này trong `additional_gateways`

Không có automated token provisioning trong v6.1. P3 `invite_only` policy sẽ
provide structured invite flow, nhưng manual token management là acceptable cho v6.1.

---

## 6. Comparison: v6.0 vs v6.1 complexity

| Metric | v6.0 | v6.1 | Delta |
|--------|------|------|-------|
| New modules (P0) | 1 (ChannelRegistry) | 1 (GatewayConnection extracted) | Same count, different nature |
| ChannelRegistry LOC | ~150 | ~30 | -120 LOC |
| New HTTP endpoints (channel) | 5 | 0 | -5 endpoints |
| New config fields | `channels:` list | `additional_gateways:` + policy | Simpler |
| New Pydantic models | 6 Channel models | 1 AdditionalGatewayConfig | -5 models |
| Conceptual concepts | Gateway + Channel (2) | Gateway = Channel (1) | Unified |
| WorkerAgent refactor | None | Extract GatewayConnection | Positive refactor |

v6.1 là **less code, less complexity, more capability**.

---

## 7. Implementation notes

### WorkerAgent task management

Khi WorkerAgent có N GatewayConnection instances, mỗi connection có 3 asyncio tasks
(heartbeat, poll, reregister). Total tasks = 3N.

Quan trọng: nếu một connection fail (gateway unreachable), các connections khác
tiếp tục hoạt động. Node vẫn connected đến các gateways còn lại.

```python
# Graceful handling nếu một gateway down
async def start(self):
    tasks = []
    for conn in self._connections:
        try:
            task = asyncio.create_task(conn.start())
            tasks.append(task)
        except Exception as e:
            logger.warning("Failed to start connection to %s: %s", conn.gateway_url, e)
            # Continue with other connections
    
    if not tasks:
        raise RuntimeError("No gateway connections could be started")
    
    await asyncio.gather(*tasks, return_exceptions=True)
```

### Subscription routing

Khi dev-B subscribe trên gateway-A's EventBus:

```
POST https://gateway-a.vietml.com/subscribe
{
  "subscriber_node": "dev-B",
  "callback_action": "on_story_updated",
  "event_type_pattern": "artifact.written",
  ...
}
```

EventBus của gateway-A sẽ deliver event bằng cách gọi:
```
POST http://dev-b:8083/action
{
  "target_node_id": "dev-B",
  "payload": {"action": "on_story_updated", "params": {...event data...}}
}
```

dev-B's address (`http://dev-b:8083`) đã được register khi dev-B register với gateway-A.
Gateway-A biết address của dev-B → EventBus có thể deliver trực tiếp.

Nếu dev-B là behind NAT (no self_address), gateway-A không có địa chỉ để push.
Fallback: event queued trong gateway-A's JobQueue, dev-B polls và claims.
Đây là consistent với existing push/pull model cho jobs.

### Test cases quan trọng

```python
# test_v61_multi_gateway.py

async def test_member_equality():
    """dev-B registered with gateway-A has same rights as native members."""
    # dev-B emit event on gateway-A
    # All gateway-A subscribers receive it
    # dev-B receives events emitted by analyst-A
    
async def test_channel_isolation():
    """Events on gateway-A do not reach gateway-B non-members."""
    # analyst-A emits on gateway-A
    # analyst-B (only on gateway-B) does NOT receive
    # pm-node (member of both) DOES receive
    
async def test_job_from_multiple_gateways():
    """dev-B can receive and execute jobs from both gateway-A and gateway-B."""
    # Enqueue job for dev-B on gateway-A
    # Enqueue different job for dev-B on gateway-B
    # Both jobs get executed
    
async def test_backward_compat():
    """Node without additional_gateways works identically to v5.13b."""
    # Node with only gateway_node_id + gateway_address
    # Behavior identical to v5.13b
    
async def test_open_registration_policy():
    """Gateway with open policy accepts any authenticated node."""
    # gateway-A has registration_policy: open
    # dev-B (not in trusted_nodes) can register
    
async def test_whitelist_policy_default():
    """Gateway without explicit policy defaults to whitelist."""
    # gateway-B has no registration_policy set
    # dev-X (not in trusted_nodes) cannot register
```

---

## 8. Guide implications

Sau v6.1, cần viết:

**Guide 24 — Event-Driven AI Dev Team v2:**
- Two separate teams với hai gateways riêng
- PM-node tham gia cả hai (multi-gateway member)
- Cross-team visibility tự nhiên từ registration
- No ChannelRegistry config — just additional_gateways

**Guide 25 — Multi-Team Node:**
- Config cho multi-gateway node
- Token provisioning workflow
- Subscription management across gateways
- Event isolation demo

---

*Worklog: WORKLOG_V6.1.md | Mesh Runtime v6.1 | Repository: ai-infra-runtime-v2*
*Previous version: WORKLOG_V6.0.md*
