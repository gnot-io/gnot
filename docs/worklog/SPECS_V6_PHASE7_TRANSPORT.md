# GNOT v6.0 — Phase 7: Multi-Channel Transport (Plugin Architecture)
## Updated Specification
### Replaces §10 Feature Area G + §17 Phase 7 của SPECS_V6_CONSOLIDATED.md

**Date:** 2026-03-08  
**Status:** Final design — ready for implementation  
**Supersedes:** Phase 7 trong SPECS_V6_CONSOLIDATED.md (§10.1, §17 Phase 7)

---

## 1. Thay đổi so với spec gốc

| Dimension | Spec gốc | Spec mới |
|-----------|----------|---------|
| Core model | 1 TelegramTransport node | TransportBridge plugin ABC |
| Bot model | 1 shared bot per cluster | Per-user bot — mỗi user đăng ký bot riêng |
| Registration | Static node.yaml config | Dynamic API: `POST /transports/telegram/bots` |
| Polling mode | Webhook only | Auto-detect: webhook nếu có public URL, fallback long-polling |
| Interaction pattern | Q&A transactional only | **Dual pattern**: Q&A + conversational (session continuity) |
| Session continuity | Không có | `/conversation <session_id>` resume từ Telegram |
| Code location | `runtime/telegram_transport.py` | `transports/telegram/` (zero Telegram code trong `runtime/`) |
| Adding Slack/Discord | Sửa core | Tạo `transports/slack/bridge.py`, không động core |

---

## 2. Mental Model

```
User A dùng Claude Web → conversation C (session_id = "sess-abc")
  [đi ra ngoài]
User A mở Telegram → @alice_gnot_bot (bot do A tự tạo, đã đăng ký với GNOT)
  → /conversation sess-abc
  → "how is the cluster provisioning going?"
  ← agent reply (cùng session, cùng context, cùng memory)
  → "ok provision 2 more workers"
  ← "Done — workers node-7 and node-8 are up"
  [về nhà]
User A mở Claude Web → continue conversation C
  → full history intact
```

GNOT không phân biệt channel — mọi message đều là `POST /intent {prompt, session_id}`. Telegram bridge chỉ là một cái "terminal window" khác cho cùng session.

---

## 3. Architecture

### 3.1 Directory structure

```
runtime/
  transport_bridge.py          # NEW: ABC + TransportBridgeRegistry (~60 LOC)
  
transports/                    # NEW top-level directory — zero imports từ runtime/ ngoài ABC
  README.md                    # How to write a transport bridge
  __init__.py
  telegram/
    __init__.py
    bridge.py                  # TelegramBridge(ChannelTransportBridge) — orchestrator
    bot_instance.py            # TelegramBotInstance — 1 instance per registered bot
    bot_api.py                 # Pure Telegram Bot API client (no GNOT deps)
    bot_registry.py            # BotInstanceRegistry — manages N bot instances (JSONL)
    config.py                  # TelegramConfig + TelegramBotRecord models
    formatters.py              # Message formatters (full/compact, vi/en)
    webhook_router.py          # FastAPI APIRouter: POST /telegram/bots/{bot_id}/update
    tests/
      test_bot_api.py
      test_bot_instance.py
      test_bridge.py
  slack/                       # Future
    README.md                  # "Not yet implemented — see transports/README.md"
  discord/                     # Future
    README.md
```

### 3.2 Core ABC: ChannelTransportBridge

```python
# runtime/transport_bridge.py

class ChannelTransportBridge(ABC):
    """Base class for all external channel transport bridges.

    Core rule: runtime/ NEVER imports from transports/.
    Coupling = only this ABC + runtime.models (ExternalParticipant, InteractionThread).
    """
    transport_id: ClassVar[str]  # "telegram" | "slack" | "discord"

    @abstractmethod
    async def startup(self) -> None:
        """Initialize bridge. Called during node lifespan startup."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Graceful disconnect. Called during node lifespan shutdown."""

    @abstractmethod
    async def notify(
        self,
        participant: ExternalParticipant,
        thread: InteractionThread,
    ) -> None:
        """Push question notification to participant (Phase 6 transactional path).
        Must not raise — catch and log all errors.
        """

    def get_fastapi_router(self) -> APIRouter | None:
        """Return FastAPI router for inbound channel webhooks.
        Auto-mounted at /transports/{transport_id}/...
        None = no inbound HTTP routes needed (polling-only bridge).
        """
        return None

    @property
    def is_ready(self) -> bool:
        return False


class TransportBridgeRegistry:
    """Holds all active transport bridge plugins."""

    def register(self, bridge: ChannelTransportBridge) -> None: ...
    def get(self, transport_id: str) -> ChannelTransportBridge | None: ...
    def all(self) -> list[ChannelTransportBridge]: ...
    def list_ids(self) -> list[str]: ...
```

### 3.3 InteractionRouter refactor

Thay thế `if/elif` chain bằng bridge registry lookup:

```python
# runtime/interaction_router.py — sau refactor

async def _notify_participant(self, participant, thread) -> None:
    if participant.transport == "polling":
        return  # no-op — participant polls /channels/{id}/pending
    
    if participant.transport == "webhook" and participant.transport_target:
        await self._notify_webhook(participant, thread)  # built-in, unchanged
        return
    
    # v6.0 Phase 7: delegate to bridge registry
    if self._bridge_registry is not None:
        bridge = self._bridge_registry.get(participant.transport)
        if bridge and bridge.is_ready:
            await bridge.notify(participant, thread)
            return
    
    logger.warning(
        "No bridge for transport=%s participant=%s",
        participant.transport, participant.participant_id,
    )
```

`InteractionRouter.__init__` thêm optional `bridge_registry=None` parameter.

---

## 4. Per-User Bot Model

### 4.1 Concept

Mỗi user tự tạo Telegram bot qua BotFather, lấy bot token, rồi đăng ký token đó với GNOT. GNOT start một `TelegramBotInstance` cho mỗi token — mỗi instance có polling loop hoặc webhook handler riêng.

```
User A:  bot_token="tok-A" → TelegramBotInstance(id="bot-A", token="tok-A")
User B:  bot_token="tok-B" → TelegramBotInstance(id="bot-B", token="tok-B")
User C:  bot_token="tok-C" → TelegramBotInstance(id="bot-C", token="tok-C")
```

`TelegramBridge` là orchestrator quản lý N instances này.

### 4.2 Bot registration flow

```
POST /transports/telegram/bots
{
  "bot_token": "7123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxx",
  "owner_user_id": "alice",        // GNOT user identifier
  "cluster_id": "cluster-A",       // cluster này bot bridge vào
  "webhook_url": "https://..."     // optional — nếu không có thì long-polling
}

→ Response:
{
  "bot_id": "bot-alice-abc123",
  "bot_username": "@alice_gnot_bot",
  "mode": "polling",               // hoặc "webhook"
  "status": "active",
  "message": "Bot @alice_gnot_bot is now active. Send /start to begin."
}
```

**Auto-detect logic:**

```python
if webhook_url:
    await api.set_webhook(webhook_url + f"/transports/telegram/bots/{bot_id}/update")
    mode = "webhook"
else:
    # Start background long-polling task
    asyncio.create_task(instance.polling_loop())
    mode = "polling"
```

### 4.3 Bot record persistence

`BotInstanceRegistry` persist vào JSONL file — bots survive node restart:

```python
@dataclass
class TelegramBotRecord:
    bot_id: str                    # "bot-alice-abc123"
    bot_token: str                 # encrypted at rest
    bot_username: str              # "@alice_gnot_bot"
    owner_user_id: str             # "alice"
    cluster_id: str
    webhook_url: str | None        # None = long-polling
    mode: str                      # "polling" | "webhook"
    active: bool = True
    registered_at: float
    # Runtime state (not persisted)
    chat_sessions: dict[int, str]  # telegram_chat_id → gnot_session_id
```

### 4.4 HTTP endpoints cho bot management

```
POST   /transports/telegram/bots              # đăng ký bot mới
GET    /transports/telegram/bots              # list all bots
GET    /transports/telegram/bots/{bot_id}     # bot status + active chats
DELETE /transports/telegram/bots/{bot_id}     # stop + deregister bot
POST   /transports/telegram/bots/{bot_id}/update  # inbound webhook từ Telegram
```

---

## 5. Dual Interaction Pattern

### 5.1 Pattern 1 — Transactional Q&A (Phase 6 extension)

Agent suspend → hỏi role "pm" → PM nhận notification qua Telegram → reply trong Telegram → task resume.

Telegram participant registration:

```json
POST /participants/register
{
  "name": "Alice PM",
  "roles": ["pm"],
  "transport": "telegram",
  "transport_target": "987654321",  // Alice's Telegram user_id (= chat_id for DMs)
  "auth_token": "tok-alice",
  "cluster_id": "cluster-A"
}
```

Flow:
```
agent suspend_and_ask(target_role="pm")
→ participant.input_required event
→ InteractionRouter.route()
→ bridge_registry.get("telegram").notify(participant, thread)
→ TelegramBridge tìm bot_instance có cluster_id == "cluster-A"
→ bot_instance.api.send_message(chat_id="987654321", text=formatted_question)
→ Alice reply trong Telegram
→ TelegramBotInstance._handle_message()
→ Nếu là reply-to question message: POST /channels/{id}/interactions/{qid}/respond
→ ChannelLog resolves → TaskPool.resume_task()
```

### 5.2 Pattern 2 — Conversational (Session Continuity)

User dùng bot như một terminal window cho GNOT session.

**Commands được hỗ trợ:**

```
/start                    → Giới thiệu bot, hướng dẫn commands
/conversation <session_id> → Link Telegram chat này vào GNOT session
/session                  → Xem session hiện tại đang linked
/status                   → Xem trạng thái session (active/suspended/last_activity)
/end                      → Unlink session (không xóa session)
```

**Session linking flow:**

```
User: /conversation sess-abc123
→ TelegramBotInstance nhận command
→ GET /sessions/sess-abc123 → validate session exists
→ Lưu: chat_sessions[telegram_chat_id] = "sess-abc123"
→ Lấy trạng thái hiện tại của session
→ Bot reply:
  "✅ Linked to conversation sess-abc123
   📊 Last activity: 5 minutes ago
   💬 Last message: 'provision a standard cluster for project X'
   ⏳ Status: running (cluster provisioning in progress)
   
   You can now continue the conversation here."
```

**Normal message flow (sau khi đã /conversation):**

```
User (Telegram): "how many workers are up so far?"
→ TelegramBotInstance._handle_message()
→ session_id = chat_sessions[chat_id]  // "sess-abc123"
→ spawn background task:
    response = POST /intent {
      prompt: "how many workers are up so far?",
      session_id: "sess-abc123"
    }
→ ACK Telegram (return 200 immediately — không chờ agent)
→ [agent loop runs, có thể mất 10-60 giây]
→ bot.send_message(chat_id, response.reply)
```

**Async response pattern (critical):**

`/intent` là synchronous HTTP call — agent loop chạy rồi mới return. Với Telegram webhook, cần phải return 200 cho Telegram trong <3 giây, nếu không Telegram retry. Do đó TelegramBotInstance phải:

1. ACK ngay (return 200 cho Telegram servers)
2. Spawn `asyncio.create_task(_call_intent_and_reply(...))`
3. Khi agent response về → `bot.send_message()`

```python
# bot_instance.py
async def _handle_message(self, update: dict) -> None:
    text = update["message"]["text"]
    chat_id = update["message"]["chat"]["id"]
    session_id = self._chat_sessions.get(chat_id)
    
    if not session_id:
        await self._api.send_message(chat_id, 
            "No conversation linked. Use /conversation <session_id> first.")
        return
    
    # Show typing indicator
    await self._api.send_chat_action(chat_id, "typing")
    
    # Fire and forget — Telegram already ACKed
    asyncio.create_task(self._call_intent_and_push(chat_id, session_id, text))

async def _call_intent_and_push(self, chat_id: int, session_id: str, prompt: str) -> None:
    try:
        response = await self._gnot_client.post_intent(prompt, session_id)
        # Handle suspended response
        if isinstance(response, TaskSuspendedResponse):
            text = f"⏸ Task suspended — waiting for input\n❓ {response.question}"
        else:
            text = response.reply
        await self._api.send_message(chat_id, text, parse_mode="Markdown")
    except Exception as exc:
        logger.error("TelegramBot: intent call failed: %s", exc)
        await self._api.send_message(chat_id, f"⚠️ Error: {exc}")
```

### 5.3 Edge cases

**Session đang suspended khi /conversation:**

```
User: /conversation sess-abc123
→ GET /sessions/sess-abc123 → OK
→ GET /tasks → tìm suspended task trong session này
→ Bot reply:
  "✅ Linked to conversation sess-abc123
   ⏸ Suspended: Agent is waiting for input from role 'pm'
   ❓ Question: 'Should we use PostgreSQL or MySQL for the main DB?'
   
   Reply to answer this question, or send a new message to continue."
```

**Khi user reply trong context suspended session:**
- Nếu có open thread → treat as answer → POST `/respond`
- Nếu không có thread nhưng session suspended → treat as TaskAnswer → POST `/tasks/{id}/answer`
- Nếu không suspended → treat as normal `/intent` prompt

**Session không tồn tại:**
```
User: /conversation sess-nonexistent
→ GET /sessions/sess-nonexistent → 404
→ "❌ Session 'sess-nonexistent' not found.
   To start a new conversation, use /new
   Or ask your GNOT administrator for the correct session ID."
```

**Concurrent access (Claude Web + Telegram cùng lúc):**
- Accept concurrent — last write wins (PersistentSessionStore append-only)
- Không có lock — use case thực tế không concurrent có vấn đề
- Worst case: 2 messages interleaved trong history, không mất data

---

## 6. GNOTClient — Internal HTTP Client cho Bot Instance

`TelegramBotInstance` cần gọi GNOT endpoints (`/intent`, `/sessions`, `/tasks`, `/channels`, `/participants`). Thay vì hardcode URL:

```python
# transports/telegram/gnot_client.py

class GNOTClient:
    """HTTP client for calling GNOT node endpoints.
    Used by TelegramBotInstance to forward messages and query state.
    Zero knowledge of Telegram — pure GNOT API wrapper.
    """
    def __init__(self, base_url: str, auth_token: str) -> None: ...
    
    async def post_intent(self, prompt: str, session_id: str) -> dict: ...
    async def get_session(self, session_id: str) -> dict | None: ...
    async def get_tasks(self) -> list[dict]: ...
    async def post_channels_respond(self, cluster_id: str, qid: str, 
                                     participant_id: str, auth_token: str,
                                     content: str) -> dict: ...
    async def register_participant(self, payload: dict) -> dict: ...
```

**Self-call pattern:** Nếu bot instance chạy in-process với gateway, `base_url = "http://localhost:{port}"`. Không cần external HTTP — tự call chính mình. Simple và không có network overhead đáng kể.

---

## 7. Message Formatting

```python
# transports/telegram/formatters.py

class TelegramFormatter:
    """Format GNOT events/responses as Telegram messages."""
    
    def format_question(self, thread: InteractionThread, style="full") -> str:
        """Format Q&A notification."""
        # full:
        # 🤔 *Question from agent `dev-A`*
        # Role needed: `pm`
        # 
        # Should we use PostgreSQL or MySQL?
        # 
        # Reply to this message to answer.
        # ⏰ Timeout: 24h | Assumption: PostgreSQL
    
    def format_intent_response(self, response: dict) -> str:
        """Format /intent response. Handles Markdown, code blocks."""
    
    def format_session_status(self, session: dict, tasks: list) -> str:
        """Format status message for /status command."""
    
    def format_suspension_notice(self, suspended_response: dict) -> str:
        """Format notification when task gets suspended mid-conversation."""
```

---

## 8. node.yaml Configuration

```yaml
# Telegram transport là global config của node
# Mỗi user sau đó đăng ký bot riêng qua API

transports:
  telegram:
    enabled: true
    # gnot_base_url: used by bot instances to call back into GNOT
    # Defaults to http://localhost:{listen_port}
    gnot_base_url: "http://localhost:8080"   # optional, auto-detected from listen:
    
    # Bot token encryption key (bots persisted encrypted at rest)
    # Defaults to auth_token if not set
    token_encryption_key: "${GNOT_TOKEN_ENC_KEY}"
    
    # Storage for registered bots
    bots_storage_path: "./telegram-bots"
    
    # Default message settings (overridable per bot)
    default_message_format: "full"   # full | compact
    default_language: "vi"           # vi | en
    
    # Polling settings (when webhook not configured)
    poll_interval_seconds: 1         # how often to poll Telegram API
    poll_timeout_seconds: 30         # long-polling timeout
    
    # Request timeout for Telegram API calls
    api_timeout_seconds: 10
```

Config parsing thêm `transports:` section trong `config.py` → `NodeConfig.transports: dict[str, dict]`.

---

## 9. Phase 7 Deliverables (revised)

### Group A — Core infrastructure (~120 LOC, không phụ thuộc Telegram)

| # | Task | LOC | File | Priority |
|---|------|-----|------|----------|
| 7.1 | `ChannelTransportBridge` ABC + `TransportBridgeRegistry` | ~60 | `runtime/transport_bridge.py` | P0 |
| 7.2 | `InteractionRouter` refactor: bridge registry delegation | ~25 | `runtime/interaction_router.py` | P0 |
| 7.3 | `transports:` config parsing + `NodeConfig.transports` field | ~35 | `runtime/config.py` | P0 |

### Group B — Server wiring (~80 LOC)

| # | Task | LOC | File | Priority |
|---|------|-----|------|----------|
| 7.4 | Transport bridge auto-discovery: load enabled bridges từ `transports:` config | ~40 | `runtime/server.py` | P0 |
| 7.5 | Bridge startup/shutdown trong lifespan | ~20 | `runtime/server.py` | P0 |
| 7.6 | Mount bridge FastAPI routers (`/transports/{id}/...`) | ~20 | `runtime/server.py` | P0 |

### Group C — Telegram: API client + core (~250 LOC)

| # | Task | LOC | File | Priority |
|---|------|-----|------|----------|
| 7.7 | `TelegramBotAPI` client (send_message, get_updates, set_webhook, get_me) | ~150 | `transports/telegram/bot_api.py` | P0 |
| 7.8 | `TelegramConfig` + `TelegramBotRecord` models | ~50 | `transports/telegram/config.py` | P0 |
| 7.9 | `GNOTClient` — HTTP client để bot instances gọi GNOT endpoints | ~80 | `transports/telegram/gnot_client.py` | P0 |

### Group D — Telegram: Bot instance + registry (~280 LOC)

| # | Task | LOC | File | Priority |
|---|------|-----|------|----------|
| 7.10 | `TelegramBotInstance` — per-bot lifecycle (polling loop hoặc webhook mode) | ~150 | `transports/telegram/bot_instance.py` | P0 |
| 7.11 | `_handle_message` + `_call_intent_and_push` (async Pattern 2) | (trong 7.10) | | P0 |
| 7.12 | Commands: `/start`, `/conversation`, `/session`, `/status`, `/end` | (trong 7.10) | | P0 |
| 7.13 | `/register` command (Pattern 1: link Telegram user → ExternalParticipant) | ~30 | `transports/telegram/bot_instance.py` | P1 |
| 7.14 | Reply-to-message → question_id mapping + answer routing | ~30 | `transports/telegram/bot_instance.py` | P1 |
| 7.15 | `BotInstanceRegistry` — JSONL persistence, manage N bots | ~100 | `transports/telegram/bot_registry.py` | P0 |

### Group E — Telegram: Bridge orchestrator + HTTP (~200 LOC)

| # | Task | LOC | File | Priority |
|---|------|-----|------|----------|
| 7.16 | `TelegramBridge` — startup/shutdown tất cả instances, implement `notify()` | ~100 | `transports/telegram/bridge.py` | P0 |
| 7.17 | Webhook FastAPI router (`POST /transports/telegram/bots/{bot_id}/update`) | ~40 | `transports/telegram/webhook_router.py` | P0 |
| 7.18 | Bot management endpoints (`POST/GET/DELETE /transports/telegram/bots`) | ~80 | `transports/telegram/bridge.py` | P0 |

### Group F — Formatting + docs (~150 LOC)

| # | Task | LOC | File | Priority |
|---|------|-----|------|----------|
| 7.19 | `TelegramFormatter` — question, intent response, session status, suspension | ~100 | `transports/telegram/formatters.py` | P0 |
| 7.20 | `transports/README.md` — bridge author guide | ~60 | `transports/README.md` | P1 |

### Group G — Tests (~350 LOC)

| # | Task | LOC | File | Priority |
|---|------|-----|------|----------|
| 7.21 | Unit tests: TelegramBotAPI (mocked HTTP) | ~100 | `transports/telegram/tests/test_bot_api.py` | P0 |
| 7.22 | Unit tests: TelegramBotInstance (commands, message routing, async push) | ~120 | `transports/telegram/tests/test_bot_instance.py` | P0 |
| 7.23 | Unit tests: TelegramBridge.notify() + BotInstanceRegistry | ~80 | `transports/telegram/tests/test_bridge.py` | P0 |
| 7.24 | Integration test: register bot → /conversation → message → response | ~150 | `tests/test_telegram_transport.py` | P0 |

**Tổng ước tính:** ~1,430 LOC (tăng ~50% so với spec gốc do thêm Pattern 2 + per-user bot model)

---

## 10. Acceptance Criteria

| Criterion | Pattern | Test |
|-----------|---------|------|
| User đăng ký bot token → GNOT start bot instance | Setup | 7.24 |
| Bot instance nhận tin nhắn từ Telegram (polling mode) | Both | 7.22 |
| `/conversation <id>` link Telegram chat vào GNOT session | P2 | 7.22 |
| User nhắn Telegram → forward đến `/intent` → response gửi lại | P2 | 7.24 |
| Session state (running/suspended) hiển thị khi link conversation | P2 | 7.22 |
| Agent hỏi role "pm" → notification đến Telegram DM | P1 | 7.23 |
| Human reply trong Telegram → thread resolved → agent resumes | P1 | 7.23, 7.24 |
| `/register pm` command → tạo ExternalParticipant với transport=telegram | P1 | 7.22 |
| Auto-detect: webhook nếu có URL, long-polling nếu không | Setup | 7.21 |
| Thêm `transports/slack/bridge.py` → không cần sửa `runtime/` | Extensibility | manual |
| `runtime/` không có import nào từ `transports/` | Cleanliness | grep check |

---

## 11. Dependency Notes

```
# transports/telegram/ cần thêm vào requirements.txt:
aiohttp>=3.9        # HTTP client cho Telegram API + GNOTClient
# Không cần python-telegram-bot — chúng ta viết minimal API client riêng
# Lý do: tránh dependency nặng (~15MB), control trực tiếp over async model
```

---

## 12. Câu hỏi defer (không block Phase 7)

- **Token encryption at rest:** Bot tokens là sensitive. Phase 7 persist plaintext với note "encrypt in Phase 8". Hoặc reuse `CredentialStore` encryption logic.
- **Rate limiting:** Telegram Bot API có rate limits (30 msg/sec global, 1 msg/sec per chat). Phase 7 chưa handle — add queue/backoff trong Phase 8.
- **Long message truncation:** `/intent` response có thể > 4096 chars (Telegram limit). Phase 7: truncate + note "(truncated)". Phase 8: split into multiple messages.
- **Slack/Discord bridge:** `transports/slack/README.md` + `transports/discord/README.md` placeholder, actual implementation deferred.
