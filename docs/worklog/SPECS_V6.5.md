# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v6.5
### Multi-Channel External Participation · Telegram Transport · Channel Push · ChannelLog

**Base version:** v6.4
**Target version:** v6.5
**Status:** Analysis complete — two directions documented, design pending
**Authors:** Architecture review session, 2026-03-08

---

## 1. Bài toán gốc

### 1.1 Yêu cầu từ product owner

> "Thiết kế hiện tại có thể cho phép user dùng Telegram để tương tác với hệ
> thống không em? Cụ thể: human A thông qua Claude web khởi tạo 1 conversation,
> và human B cũng có thể tham gia conversation này và human B có thể dùng
> Telegram để giao tiếp?"

### 1.2 Scenario đầy đủ cần support

```
Human A (Nguyen):
  - Mở Claude.ai → /intent session với external-adapter node
  - Khởi tạo conversation, đăng ký tham gia team
  - Nhận và trả lời agent questions qua chat UI

Human B (John):
  - Dùng Telegram — không có / không muốn mở Claude web
  - Cũng tham gia cùng conversation/room
  - Nhận questions qua Telegram message
  - Reply ngay trong Telegram chat

Kỳ vọng:
  - Cả A và B đều tương tác với cùng một conversation
  - B thấy những gì A đang thảo luận (và ngược lại)
  - B có thể reply, comment, tag qua Telegram
  - A có thể thấy B's activity trong session của mình
```

### 1.3 Mapping lên v6.4

**Những gì v6.4 đã có:**

| Sub-requirement | Status | Note |
|-----------------|--------|------|
| Human A dùng /intent session | ✅ | transport: session mode |
| Outbound notification đến webhook | ✅ | transport: webhook mode |
| Human B reply từ webhook về room | ✅ | POST /room/.../reply |
| Shared ChannelLog data | ✅ | Cả hai write vào cùng store |
| Participants thấy cùng thread history | ✅ | GET /room/{cluster_id} |

**Những gì v6.4 chưa có:**

| Sub-requirement | Status | Gap |
|-----------------|--------|-----|
| Built-in Telegram adapter | ❌ | v6.4 chỉ nói "webhook receiver tự build" |
| Telegram → GNOT reply pipeline | ❌ | Cần inbound webhook handler |
| Session nhận real-time updates | ❌ | Session là request-response, không push |
| A thấy B's activity trong session | ❌ | Không có cross-channel notification |
| B thấy A's discussion trong Telegram | ❌ | Không có Telegram group sync |

---

## 2. Gap analysis chi tiết

### 2.1 Gap 1: Telegram là two-way, v6.4 chỉ thiết kế one-way

v6.4 transport webhook: GNOT fires POST khi có question.
Đây là **outbound only**. Để Telegram hoạt động fully cần:

```
Direction A (outbound): GNOT → Telegram
  GNOT detects question → POST webhook → [adapter] → Telegram sendMessage API
  ✅ Architecturally có, nhưng cần adapter implementation

Direction B (inbound): Telegram → GNOT
  John replies in Telegram → Telegram bot receives update →
  [adapter] → POST /room/{cluster_id}/threads/{qid}/reply → GNOT
  ❌ Không có inbound handler trong v6.4
```

Khoảng cách là `[adapter]` — một component biết cả Telegram Bot API lẫn
GNOT room API. v6.4 không có component này.

### 2.2 Gap 2: Session model là stateless request-response

`/intent` session trong GNOT hiện tại:

```
HTTP request:  POST /intent {session_id, prompt}
HTTP response: {reply, session_id, ...}
```

Session state (messages) được lưu trong ConversationStore. Nhưng HTTP model
là **request-driven**: LLM chỉ respond khi có incoming request. Không có
mechanism để server push message về client.

Consequence: Khi John (Telegram) submits answer → ChannelLog updated →
Nguyen's session **không biết**. Nguyen chỉ biết nếu gõ "show me latest activity."

Đây không phải bug — đây là intentional design của stateless HTTP. Nhưng nó tạo ra
"stale session" problem: Nguyen's conversation lags behind ChannelLog.

### 2.3 Gap 3: "Cùng conversation" vs "shared data store" — semantic gap

v6.4 ChannelLog là shared data store — nhiều channels write vào cùng nơi.
Nhưng user experience khi nhìn vào từ hai phía:

```
Nguyen's /intent session:          John's Telegram:
  [linear chat history]               [Telegram messages]
  [doesn't see Telegram activity]     [doesn't see session dialogue]
  
  "Cùng room" theo GNOT              "Khác nhau hoàn toàn" theo UX
```

Để thực sự "cùng conversation" — cả hai phải thấy nhau's messages in near
real-time, không chỉ share data store mà không biết nhau.

---

## 3. Hai hướng giải quyết

---

# HƯỚNG 1: TelegramTransport Node (MVP)

## H1.1 Overview

Thêm `TelegramTransport` như một optional GNOT node — join team như các nodes
khác, bridge giữa GNOT ChannelLog và Telegram Bot API.

**Philosophy:** Không thay đổi core architecture. Telegram là một delivery channel
như webhook, polling — chỉ là channel này có two-way inbound/outbound built-in.

```
┌─────────────────────────────────────────────────────────────────┐
│  Cluster A                                                         │
│                                                                 │
│  ┌──────────────┐    ┌─────────────────────────────────────┐   │
│  │ agent nodes  │    │ external-adapter-cluster-A              │   │
│  │ (dev, PM...) │    │   ExternalParticipantRegistry               │   │
│  │              │    │   ChannelLog                  │   │
│  │    events    │◄──►│   QuestionRouter                    │   │
│  └──────────────┘    └────────────┬────────────────────────┘   │
│                                   │ events                      │
│                      ┌────────────▼────────────────────────┐   │
│                      │ telegram-transport-cluster-A             │   │
│                      │   Telegram Bot API client           │   │
│                      │   Inbound webhook handler           │   │
│                      │   Message → Room mapping            │   │
│                      └────────────┬────────────────────────┘   │
│                                   │                             │
└───────────────────────────────────┼─────────────────────────────┘
                                    │ Telegram Bot API
                          ┌─────────▼──────────┐
                          │  Telegram Group    │
                          │  John, (others)    │
                          └────────────────────┘
```

## H1.2 TelegramTransport node design

```python
# TelegramTransport node responsibilities:
#
# 1. Subscribe to room events via EventBus:
#    - participant.input_required → format → send to Telegram group
#    - participant.answered → notify group (which question resolved)
#    - participant.commented → forward to group
#    - participant.tagged → notify tagged user
#
# 2. Expose inbound webhook for Telegram Bot:
#    POST /telegram/update  ← Telegram calls this
#    → parse Telegram Update object
#    → map to GNOT room action (answer/comment/tag)
#    → call POST /room/{cluster_id}/threads/{qid}/reply
#
# 3. Manage Telegram ↔ GNOT participant mapping:
#    telegram_user_id: 123456 ↔ participant_id: "p-john"
#    → stored in local JSON file

# node.yaml for telegram-transport
node_id: telegram-transport-cluster-A
listen: 0.0.0.0:8100

gateway_node_id: gateway-cluster-A
gateway_address: http://localhost:8091
gateway_auth_token: tok-tg-on-team-a

# Telegram config (NEW section)
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  group_chat_id: "-1001234567890"   # Telegram group for cluster-A
  webhook_url: "https://your-domain.com/telegram/update"
  # This URL must be reachable from Telegram servers

event_bus:
  enabled: true

scheduler:
  enabled: true

schedule:
  - trigger_type: event
    on_event_type: "participant.input_required"
    run_action: telegram_forward_question
    description: Forward agent questions to Telegram group

  - trigger_type: event
    on_event_type: "participant.answered"
    run_action: telegram_notify_resolved
    description: Notify Telegram when question is resolved

  - trigger_type: event
    on_event_type: "participant.commented"
    run_action: telegram_forward_comment
    description: Forward comments to Telegram

  - trigger_type: event
    on_event_type: "participant.tagged"
    run_action: telegram_notify_tagged
    description: DM tagged participant on Telegram
```

## H1.3 Telegram message format

**Khi agent hỏi (outbound):**

```
📋 Câu hỏi từ dev-A [implement-user-model]

Should deactivated users' data be hidden or deleted?

Required expertise: PM
⏳ Timeout: 24h | Assumption: Hide data with is_active flag

Context: Dev đang implement user model, cần clarify deactivated user behavior.

Reply with:
  /answer <text>       — trả lời (cần role PM)
  /comment <text>      — thêm context (không resolve)
  /tag @username <msg> — tag người khác

Question ID: q-abc123
```

**Khi được giải đáp (outbound):**

```
✅ Đã giải đáp [q-abc123]

Nguyen PM trả lời (as pm):
"Hide data, never delete — audit trail required for compliance"

dev-A đã resume task.
```

**Inbound — John replies:**

```
John types in Telegram group:
/answer q-abc123 AWS, we have existing contract with them

            ↓
TelegramTransport nhận Telegram Update
→ parse: command=/answer, question_id=q-abc123, text="AWS..."
→ lookup: John's telegram_id → participant_id "p-john"
→ POST /room/cluster-A/threads/q-abc123/reply
   {reply_type: "answer", role_used: "devops", text: "AWS..."}
   Authorization: Bearer {john's auth_token}
```

**Alternative: reply via message threading (more natural):**

```
Telegram supports message replies (reply to a specific message).
Instead of /answer command, John simply replies to the question message:

[Question message]: "Deploy on AWS or GCP?"
  └── [John's reply]: "AWS — existing contract"

TelegramTransport detects: message.reply_to_message.message_id
→ lookup question_id from message_id mapping
→ same POST /room/... flow
```

Reply-to-message UX = more natural for Telegram users. No commands to remember.

## H1.4 Participant registration via Telegram

John muốn register từ Telegram (không qua Claude web):

```
John sends to Telegram bot:
  /register pm devops

Bot:
  → TelegramTransport calls POST /participants/register
    {name: "John (Telegram)", roles: ["pm", "devops"],
     transport: "telegram", telegram_user_id: 987654321,
     cluster_id: "cluster-A"}
  → Receives auth_token
  → Stores locally: telegram_id → {participant_id, auth_token}

Bot replies:
  ✅ Đã đăng ký!
  Name: John (Telegram)
  Roles: pm, devops
  Bạn sẽ nhận câu hỏi cho các roles này.
```

## H1.5 Telegram Group = Shared Room cho Telegram users

Khi nhiều humans dùng Telegram (John + Mary + Bob), tất cả vào **cùng một Telegram group**:

```
Telegram Group: "Cluster A — Dev Project"
  Members: John, Mary, Bob (humans) + Bot (TelegramTransport)

Mọi agent question → Bot posts in group → all three see it
John answers → Bob and Mary see John's reply
Mary comments → everyone sees
Bob tags Mary → Mary gets DM notification

Natural group chat UX — no extra coordination needed
```

## H1.6 Hướng 1 — What it enables

```
Human A (Nguyen) — Claude web:
  Nguyen: "Show me current questions"
  LLM: "Có 2 câu hỏi đang open:
        1. [q-abc] 'AWS or GCP?' — for devops (John đang xử lý)
        2. [q-def] 'API versioning?' — for pm (chưa ai trả lời)"
  Nguyen: "Tôi trả lời câu API versioning — v1 only for now"
  LLM: → POST /room/... → resolved → emits event
       "Đã ghi nhận!"

Human B (John) — Telegram:
  [Bot message in Telegram group]:
  📋 Câu hỏi từ architect-A: "Deploy on AWS or GCP?"
  
  John replies to message: "AWS"
  
  [Bot]: ✅ Answer submitted. architect-A sẽ resume.

Convergence: cả hai tác động lên cùng ChannelLog
  → Nguyen thấy lịch sử room khi query
  → Telegram group thấy lịch sử qua bot messages
```

## H1.7 Hướng 1 — Limitations

**L1: A không thấy B's activity real-time (and vice versa)**

Khi John answers via Telegram:
- ChannelLog updated ✅
- Telegram group sees resolved message ✅
- Nguyen's /intent session: **không biết** ❌ (phải hỏi thủ công)

**L2: Two separate conversation experiences**

Nguyen đọc linear chat history trong Claude web.
John đọc Telegram group messages.
Hai UXes khác nhau — không phải "cùng một conversation."

**L3: Telegram command syntax (nếu không dùng reply-to)**

`/answer q-abc123 AWS` không natural cho users không technical.
Reply-to-message approach tốt hơn nhưng cần careful mapping.

**L4: Telegram group = team, not conversation**

Telegram group là room-level (tất cả questions của team), không phải
thread-level. Nếu nhiều questions cùng lúc, Telegram group sẽ noisy.
Cần message threading hoặc separate groups per question (complex).

---

# HƯỚNG 2: Session Push + Unified Conversation Layer (Full Vision)

## H2.1 Overview

Thay đổi `/intent` session model để hỗ trợ **server-push events** — session
có thể nhận real-time notifications từ ChannelLog, không cần human poll.

Đồng thời, introduce **ChannelLog** layer — abstract layer giữa
transport (Telegram, Web, API) và conversation state — đảm bảo tất cả
channels truly share cùng một experience.

```
┌─────────────────────────────────────────────────────────────────┐
│  ChannelLog Layer                                      │
│                                                                 │
│  ChannelLog (shared state)                                │
│       │                                                         │
│       ├── SessionChannelTransport (Claude web /intent sessions)           │
│       │     - SSE or WebSocket push to active sessions          │
│       │     - LLM formats and presents events proactively       │
│       │                                                         │
│       ├── TelegramChannelTransport (Telegram groups/DMs)                  │
│       │     - Same as Hướng 1 TelegramTransport                   │
│       │     - PLUS: receives room-event push, forwards to TG    │
│       │                                                         │
│       └── APIChannelTransport (raw REST, for custom integrations)         │
│             - Webhook push to registered URLs                   │
│             - Polling via GET /room/pending                     │
└─────────────────────────────────────────────────────────────────┘
```

## H2.2 Session Push mechanism

### Option 2a: Server-Sent Events (SSE)

SSE là HTTP/1.1 standard — server streams events to client over long-lived connection.
Compatible với most HTTP clients. One-way (server → client). Simpler than WebSocket.

```
Client opens:
  GET /intent/stream/{session_id}
  Accept: text/event-stream

Server sends events as they occur:
  data: {"type": "room.question_answered", "thread_id": "q-abc",
         "resolved_by": "John DevOps", "answer": "AWS"}

  data: {"type": "room.question_new", "thread_id": "q-xyz",
         "required_role": "pm", "question": "..."}

  data: {"type": "room.comment_added", ...}

  data: {"type": "heartbeat"}   ← every 30s to keep connection alive
```

**How the LLM session receives this:**

Claude web / chat client:
- Maintains SSE connection alongside normal /intent calls
- When SSE event received → automatically inject as message into session context
- LLM "sees" the event as if user typed it → LLM responds proactively

```
[SSE event arrives: room.question_answered by John]
           ↓
Client synthesizes: POST /intent
  {session_id: "...", prompt: "[SYSTEM] Room update: John DevOps answered q-abc123:
   'AWS us-east-1'. architect-A has resumed. [END SYSTEM]"}
           ↓
LLM generates response:
  "John vừa trả lời câu hỏi của architect-A — chọn AWS. Architect đã resume rồi nhé.
   Hiện còn 1 câu hỏi đang mở cần role pm của bạn."
           ↓
Nguyen thấy proactive update, không cần hỏi
```

### Option 2b: WebSocket

Bidirectional — cả send và receive qua một connection.
Richer but more complex. Required nếu cần human → server push (ví dụ: typing indicators).

```
ws://gnot.local/intent/ws/{session_id}

Server → Client messages:
  {"type": "room.update", "data": {...}}
  {"type": "llm.thinking"}
  {"type": "llm.response", "text": "..."}

Client → Server messages:
  {"type": "user.message", "text": "..."}
  {"type": "user.typing"}
```

**Recommendation: SSE for v6.5, WebSocket for later.**

SSE đủ cho use case (server push room events). WebSocket adds complexity
không cần thiết cho v6.5. Claude web sử dụng HTTP streaming — SSE là consistent.

## H2.3 ChannelLog — conversation state shared across channels

```python
# runtime/unified_conversation.py

@dataclass
class ChannelLogEntry:
    """
    A single message in the unified conversation.
    Visible to ALL channels.
    """
    message_id: str
    channel: str              # "session:ses-abc" | "telegram:chat-xyz" | "api"
    author_type: str          # "human" | "agent" | "system"
    author_name: str          # "Nguyen PM" | "dev-A" | "GNOT"
    content: str
    message_type: str         # "question" | "answer" | "comment" | "tag" | "status"
    created_at: float
    related_question_id: str | None = None
    metadata: dict = field(default_factory=dict)


class ChannelLog:
    """
    Unified conversation state — single source of truth.

    All bridges (Session, Telegram, API) read from and write to here.
    Each bridge receives push notifications when new messages arrive.

    Room ID = cluster_id (consistent with ChannelLog in v6.4).
    Messages append-only (JSONL).

    Bridges register themselves to receive push:
        register_bridge(bridge_id, callback: Callable[[ChannelLogEntry], Awaitable])
        → callback is called for every new message

    Replaces v6.4 ChannelLog as primary state store.
    v6.4 InteractionThread model is preserved as a view/index into messages.
    """

    async def post_message(self, message: ChannelLogEntry) -> None:
        """Append message and notify all bridges."""
        self._messages.append(message)
        await self._persist(message)
        await self._notify_bridges(message)

    async def register_bridge(
        self,
        bridge_id: str,
        callback: Callable[[ChannelLogEntry], Awaitable[None]]
    ) -> None:
        """Register a bridge to receive real-time message push."""
        self._bridges[bridge_id] = callback

    def get_messages(
        self,
        since: float | None = None,
        message_types: list[str] | None = None,
    ) -> list[ChannelLogEntry]:
        """Get messages with optional filters."""
```

## H2.4 SessionChannelTransport — Web session receives room updates

```python
# runtime/session_channel_transport.py

class SessionChannelTransport:
    """
    Bridges ChannelLog to /intent HTTP sessions.

    For each active SSE connection (/intent/stream/{session_id}):
    - Registered as callback in ChannelLog
    - When new message arrives → format → send via SSE stream
    - LLM session context updated automatically

    When session is inactive (no SSE connection):
    - Messages buffered (in-memory, max 100)
    - Delivered when session reconnects
    """

    async def on_conversation_message(self, message: ChannelLogEntry) -> None:
        """Called by ChannelLog for every new message."""
        session_id = self._active_sessions.get(self._participant_id)
        if session_id and session_id in self._sse_streams:
            await self._send_sse(session_id, {
                "type": f"room.{message.message_type}",
                "data": {
                    "author": message.author_name,
                    "content": message.content,
                    "created_at": message.created_at,
                    "related_question": message.related_question_id,
                }
            })
        else:
            self._buffer[self._participant_id].append(message)
```

## H2.5 TelegramChannelTransport — Full two-way bridge

```python
# runtime/telegram_channel_transport.py

class TelegramChannelTransport:
    """
    Full two-way bridge between ChannelLog and Telegram.

    Outbound (Conversation → Telegram):
      Registered as callback in ChannelLog.
      Formats messages and sends to Telegram group/DM.

    Inbound (Telegram → Conversation):
      Telegram webhook → parse → post_message to ChannelLog.
      ChannelLog notifies all bridges (including SessionChannelTransport).

    Result: Nguyen's session sees John's Telegram message in real-time.
    """

    async def on_conversation_message(self, message: ChannelLogEntry) -> None:
        """Forward new conversation messages to Telegram."""
        if message.channel.startswith("telegram:"):
            return  # Don't echo Telegram messages back to Telegram

        formatted = self._format_for_telegram(message)
        await self._send_to_telegram(formatted)

    async def handle_telegram_webhook(self, update: dict) -> None:
        """
        Receive incoming Telegram update (user message/reply).
        Convert to ChannelLogEntry and post to ChannelLog.
        → Triggers SessionChannelTransport → Nguyen sees John's reply in real-time.
        """
        telegram_user_id = update["message"]["from"]["id"]
        participant = self._participant_map.get(telegram_user_id)
        if not participant:
            await self._send_telegram_message("Bạn chưa đăng ký. Gửi /register để tham gia.")
            return

        text = update["message"]["text"]
        question_id = self._extract_question_id(update)  # from reply-to or command

        message = ChannelLogEntry(
            message_id=str(uuid.uuid4()),
            channel=f"telegram:{update['message']['chat']['id']}",
            author_type="human",
            author_name=participant.name,
            content=text,
            message_type="answer" if question_id else "comment",
            related_question_id=question_id,
        )
        await self._conversation.post_message(message)
        # → notifies SessionChannelTransport → Nguyen sees "John (Telegram): AWS"
        # → notifies QuestionRouter → if answer, resolve thread + emit participant.answered
```

## H2.6 Unified experience — what it looks like

**Nguyen (Claude web) and John (Telegram) both active:**

```
═══════════════════════════════════════════════════════
From Nguyen's perspective (Claude web /intent session):
═══════════════════════════════════════════════════════

[Previous messages...]

LLM: "📋 Câu hỏi mới từ architect-A:
     'Deploy trên AWS hay GCP?'
     Required: devops — John sẽ trả lời được.
     Timeout: 8h | Assumption: AWS us-east-1"

[5 minutes later — SSE push from TelegramChannelTransport]

LLM: "John vừa trả lời qua Telegram:
     'AWS — chúng ta đã có existing contract với AWS'
     Architect đã resume rồi. 👍"

Nguyen: "Good. Còn câu hỏi nào đang open không?"

LLM: "Không còn câu hỏi nào. Cả 2 teams đang chạy tốt."

═══════════════════════════════════════════════════════
Cùng lúc, John's Telegram group:
═══════════════════════════════════════════════════════

[Bot]: 📋 Câu hỏi từ architect-A: "Deploy trên AWS hay GCP?"
       Required: devops | Timeout: 8h

John: [replies to bot message] AWS — chúng ta đã có existing contract

[Bot]: ✅ Đã ghi nhận. architect-A sẽ resume.
       💬 Nguyen đã xem câu trả lời của bạn.

═══════════════════════════════════════════════════════
Backend flow:
═══════════════════════════════════════════════════════

John's reply → TelegramChannelTransport.handle_webhook()
             → ChannelLog.post_message()
             → SessionChannelTransport.on_conversation_message() → SSE → Nguyen's session
             → QuestionRouter.resolve_thread() → participant.answered event
             → dev-A resumes
```

## H2.7 New endpoint: `GET /intent/stream/{session_id}`

```
GET /intent/stream/{session_id}
Accept: text/event-stream
Authorization: Bearer {participant_auth_token}

Response: 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache

data: {"type": "connected", "session_id": "ses-abc", "cluster_id": "cluster-A"}

data: {"type": "heartbeat"}

data: {"type": "room.answer", "data": {"author": "John (Telegram)",
       "content": "AWS — existing contract", "question_id": "q-xyz"}}

data: {"type": "room.question_new", "data": {"required_role": "pm",
       "question": "API versioning strategy?", "question_id": "q-def"}}
```

## H2.8 Updated /intent flow với SSE

```
Traditional /intent (unchanged):
  Client: POST /intent {session_id, prompt}
  Server: 200 OK {reply, ...}
  (works without SSE — backward compat)

Enhanced /intent with SSE (new):
  Client opens: GET /intent/stream/{session_id}
  Client sends: POST /intent {session_id, prompt}
  Server: 200 OK {reply, ...}
  Server also: pushes room events via SSE stream when they occur

Client implementation:
  - Open SSE stream once per session
  - For each SSE event received:
    → Synthesize as POST /intent with system-prefixed prompt
    → LLM responds proactively to the room update
  - For user messages: normal POST /intent
```

---

## 4. Comparison: Hướng 1 vs Hướng 2

| Dimension | Hướng 1: TelegramTransport | Hướng 2: Session Push |
|-----------|--------------------------|----------------------|
| **Architecture change** | Additive (new node) | Significant (new session model) |
| **Core system change** | None | /intent + ConversationStore |
| **Implementation effort** | Low–Medium | High |
| **Real-time sync** | ❌ | ✅ |
| **Cross-channel visibility** | Partial (room state, not live) | ✅ Full real-time |
| **Telegram support** | ✅ | ✅ |
| **Backward compat** | Full | Full (SSE is opt-in) |
| **New primitives** | TelegramTransport node | SSE endpoint, ChannelLog, Bridges |
| **Complexity** | Low | High |
| **"Group chat" feel** | Telegram group (among TG users) | Yes, cross-channel |
| **Future extensibility** | Add more adapters (Slack, etc.) | All channels unified |

---

## 5. Phân tích: Khi nào dùng Hướng nào

### Hướng 1 phù hợp khi:

- **Telegram users are the minority** — một vài humans dùng Telegram, phần lớn dùng Claude web
- **Low latency requirement** — async team (PM answer at their own pace)
- **Quick to ship** — cần Telegram integration trong thời gian ngắn
- **Team prefers Telegram** — Telegram group là primary communication already
- **V6.4 is not yet implemented** — build adapter on top of v6.4 spec

### Hướng 2 phù hợp khi:

- **Real-time collaboration** — team members need to see each other's activity immediately
- **Multiple channels** — Telegram + Slack + web UI + API, all unified
- **Long-running projects** — months-long projects where missed updates are costly
- **High human activity** — humans actively involved, not just occasional answerers
- **Building a product** — platform play where extensibility matters

### Recommendation: Hướng 1 first, Hướng 2 later

```
Phase 1 (v6.5 MVP): Implement TelegramTransport node
  → Telegram integration working within v6.4 architecture
  → No core changes
  → Ship quickly

Phase 2 (v6.6): Add SSE push to /intent sessions
  → Nguyen's session receives John's Telegram replies
  → Still no ChannelLog layer
  → Incremental improvement

Phase 3 (v6.7): Full ChannelLog
  → All channels truly unified
  → Slack adapter, API adapter added using same Bridge pattern
  → Full cross-channel real-time experience
```

---

## 6. New components — both directions

### Hướng 1: TelegramTransport node

| Component | Type | Description |
|-----------|------|-------------|
| `telegram-transport-node` | New node type | Deployed per team |
| `runtime/telegram_transport.py` | NEW | Telegram Bot API client, message formatting |
| `runtime/telegram_webhook_handler.py` | NEW | Inbound Telegram update handler |
| `runtime/telegram_participant_map.py` | NEW | telegram_user_id ↔ participant_id mapping |
| `blueprints/roles/telegram-transport.md` | NEW | Skills for TelegramTransport node |
| `seed/actions/telegram_forward_question.py` | NEW | Event handler: question → Telegram |
| `seed/actions/telegram_notify_resolved.py` | NEW | Event handler: resolved → Telegram |

### Hướng 2: Session Push + Unified Conversation

| Component | Type | Description |
|-----------|------|-------------|
| `runtime/unified_conversation.py` | NEW | Central message store, bridge orchestration |
| `runtime/session_channel_transport.py` | NEW | Web session SSE push |
| `runtime/telegram_channel_transport.py` | NEW | Two-way Telegram bridge (extends Hướng 1) |
| `runtime/api_channel_transport.py` | NEW | Raw API bridge (webhook + polling) |
| `runtime/server.py` | MODIFY | Add `GET /intent/stream/{session_id}` SSE endpoint |
| `runtime/intent_handler.py` | MODIFY | Receive room events via session bridge |
| `runtime/conversation_store.py` | MODIFY | Integration with ChannelLog |

---

## 7. node.yaml — v6.5 additions

### Hướng 1: TelegramTransport config section

```yaml
# telegram-transport-cluster-A/node.yaml

node_id: telegram-transport-cluster-A
listen: 0.0.0.0:8100

gateway_node_id: gateway-cluster-A
gateway_address: http://localhost:8091
gateway_auth_token: tok-tg-on-team-a

# NEW: Telegram section
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  group_chat_id: "-1001234567890"
  webhook_url: "https://your-domain.com/telegram/update"
  # Optional: separate thread per question (Telegram Topics)
  use_topics: false
  # Optional: DM individual participants instead of group
  use_dm: false
  # Message format: "minimal" | "full" | "compact"
  message_format: "full"
  # Language for bot messages
  language: "vi"   # "vi" | "en"

# Participant map storage
telegram_participant_map:
  storage_path: ./telegram-participants.json

event_bus:
  enabled: true
scheduler:
  enabled: true
```

### Hướng 2: SSE config on external-adapter node

```yaml
# external-adapter-cluster-A/node.yaml additions

# NEW: Session push via SSE
session_push:
  enabled: true
  sse_keepalive_seconds: 30
  buffer_size: 100             # messages buffered for offline sessions
  buffer_ttl_seconds: 86400    # 24h buffer retention

# NEW: Unified conversation (Hướng 2 only)
unified_conversation:
  enabled: true
  storage_path: ./unified-room.jsonl
  bridges:
    - type: session              # web /intent sessions
    - type: telegram             # requires telegram section
    - type: api                  # raw webhook + polling
```

---

## 8. End-to-end scenario — Hướng 2 full vision

```
Setup:
  Cluster A running. external-adapter-cluster-A has SSE + ChannelLog.
  telegram-transport-cluster-A running and connected.

  Nguyen registers (session mode):
    Nguyen: "Tôi là PM, muốn join cluster A"
    → registered, session bridge active, SSE stream open

  John registers (Telegram):
    John sends to @TeamABot: /register devops cloud-architect
    → TelegramChannelTransport registers John, participant_id: p-john

═══════════════════════════════════════════════════
t=1: architect-A asks "AWS or GCP?"
     emit participant.input_required {required_role: "devops"}
═══════════════════════════════════════════════════

     QuestionRouter → open thread → notify John (Telegram)
     TelegramChannelTransport.on_conversation_message():
       → POST to Telegram: "📋 Câu hỏi từ architect-A..."
     SessionChannelTransport.on_conversation_message():
       → SSE push → Nguyen's session

     Nguyen's session receives SSE:
       LLM proactively: "Architect vừa hỏi về AWS/GCP. John đang nhận câu hỏi."
       Nguyen: "Hệ thống của chúng ta đang dùng AWS nhé"
       LLM: → post ChannelLogEntry (comment, Nguyen, "Our system uses AWS")
            → TelegramChannelTransport receives → post to Telegram group

     Telegram group:
       [Bot]: 💬 Nguyen PM: "Our system uses AWS"

═══════════════════════════════════════════════════
t=2: John sees both the question and Nguyen's comment
     John replies in Telegram: "AWS us-east-1, same as our other services"

     TelegramChannelTransport.handle_webhook():
       → parse reply → ChannelLogEntry (answer, John, "AWS us-east-1...")
       → ChannelLog.post_message()
       → SessionChannelTransport → SSE → Nguyen's session
       → QuestionRouter → resolve → participant.answered → architect-A resumes

     Nguyen's session (SSE push):
       LLM: "John vừa trả lời từ Telegram: 'AWS us-east-1, same as other services'
             Architect đã resume. (Bạn vừa add context có ích đó nhé 👍)"

     Telegram group:
       [Bot]: ✅ John đã giải đáp. architect-A resume.
              💬 Nguyen đã confirm AWS trước đó.
```

---

## 9. Open questions — v6.5

**Q1: Telegram Topics (Supergroup feature)**
Telegram Supergroups support "Topics" — separate threads within one group.
Each question could be a separate topic.
Pros: organized, no noise from unrelated questions.
Cons: requires Supergroup (not basic group), more complex mapping.
→ Config flag: `use_topics: true/false`

**Q2: SSE vs WebSocket for session push**
SSE: one-way (server → client), simpler, HTTP/1.1 compatible.
WebSocket: two-way, richer, requires upgrade.
For v6.5 (room events → session), SSE is sufficient.
WebSocket needed if we want typing indicators, live co-editing.
→ Ship SSE first.

**Q3: Telegram message language**
Bot messages should match team's language (VN or EN).
Config flag: `language: "vi"` for Vietnamese bot messages.
→ Include from v6.5.

**Q4: Multiple Telegram groups per team**
Current design: one Telegram group per team.
Future: separate groups per project sub-scope?
E.g.: infra team has their own group, product team has theirs.
→ P4 — not in v6.5 scope.

**Q5: Slack adapter**
Same Bridge pattern as TelegramChannelTransport.
Slack incoming webhooks + Slack bot for inbound.
→ Natural extension of Hướng 2 Bridge pattern.
→ Not in scope, but design supports it.

**Q6: Authentication for SSE stream**
SSE connection needs auth: `?token=participant_token` or header.
If SSE stream is unauthenticated, anyone with session_id can listen.
→ Require same auth_token used for /room endpoints.

---

## 10. Readiness after v6.5

```
Hướng 1 (TelegramTransport):
  ✅ Telegram users can receive agent questions
  ✅ Telegram users can answer, comment, tag
  ✅ Telegram group = shared room for Telegram-side humans
  ✅ Human registration via Telegram
  ❌ Claude web session doesn't see Telegram activity real-time
  ❌ No cross-channel unified experience

Hướng 2 (Session Push + Unified):
  ✅ Everything in Hướng 1
  ✅ Claude web session receives Telegram activity in real-time
  ✅ True unified conversation across channels
  ✅ Extensible: add Slack, API, other channels via Bridge pattern
  ✅ "Group chat" feel despite different UX clients
  ❌ Higher implementation complexity
  ❌ SSE requires client-side implementation (Claude web needs to open stream)
```

### Full v6.x stack after v6.5 (Hướng 2):

```
One prompt → teams provisioned (v6.3)
Teams work autonomously (v6.0–v6.2)
Humans join with roles (v6.4)
Humans use any channel — web or Telegram — unified experience (v6.5)
Real-time cross-channel visibility (v6.5 Hướng 2)
```

---

*Spec: SPECS_V6.5.md | Mesh Runtime v6.5 | Repository: ai-infra-runtime-v2*
*Builds on: SPECS_V6.4.md through SPECS_V6.0.md*
*Problem statement: product owner review session, 2026-03-08*

---

## 11. Terminology alignment — v6.3 through v6.5

This section documents the canonical GNOT terminology applied consistently
across all v6.x specs. Earlier drafts used domain-specific terms that were
corrected to preserve GNOT's generative orchestration identity.

### 11.1 Structural terms

| Draft term | Canonical GNOT term | Rationale |
|------------|---------------------|-----------|
| `Team`, `TeamSpec` | `Cluster`, `ClusterSpec` | A "team" is just a cluster of nodes provisioned for a shared purpose |
| `TeamWirer` | `ClusterOrchestrator` | Orchestrates node cluster lifecycle, not team operations |
| `MemberSpec` | `NodeSpec` | Members are nodes |
| `ProvisionPlan` | `OrchestrationPlan` | Planning is orchestration, not team management |
| `RolePlanSpec` | `NodeRolePlanSpec` | Role describes a node's function within the cluster |
| `provision_team` / `teardown_team` | `provision_cluster` / `teardown_cluster` | Cluster lifecycle verbs |
| `team_id` | `cluster_id` | Identifier for the cluster |
| `standard-dev-team.yaml` | `standard-cluster.yaml` | Blueprint names should be cluster-topology-centric |
| `project.started` | `cluster.started` | Events should reflect mesh topology, not application domain |
| `GET /teams` | `GET /clusters` | HTTP endpoints reflect GNOT primitives |

### 11.2 External participant terms

| Draft term | Canonical GNOT term | Rationale |
|------------|---------------------|-----------|
| `HumanParticipant` | `ExternalParticipant` | Participant is external to the mesh — not necessarily human. Could be another system. |
| `HumanInterface node` | `ExternalAdapter node` | Adapts external channels to GNOT mesh events |
| `ConversationRoom` | `ChannelLog` | Log of interactions in a channel — not a "room" (too chat-specific) |
| `QuestionThread` | `InteractionThread` | A thread of interaction — not necessarily a question |
| `QuestionAnswer` / `QuestionReply` | `InteractionAnswer` / `InteractionReply` | Interaction semantics |
| `ParticipantRegistry` | `ExternalParticipantRegistry` | Explicit scoping |
| `notify_via` | `transport` | Transport is the generic term for delivery mechanism |
| `notify_target` | `transport_target` | Target for the transport |
| `ask_role` | `target_role` | Target role for the interaction request |
| `human.input_required` | `participant.input_required` | Event namespace reflects participant type |
| `human.answered` | `participant.answered` | Consistent event namespace |
| `human.commented` | `participant.commented` | Consistent event namespace |
| `human.tagged` | `participant.tagged` | Consistent event namespace |

### 11.3 Channel transport terms (v6.5)

| Draft term | Canonical GNOT term | Rationale |
|------------|---------------------|-----------|
| `TelegramAdapter` | `TelegramTransport` | "Transport" is the generic abstraction. "Adapter" implies single-direction. |
| `TelegramBridge` | `TelegramChannelTransport` | Full two-way channel transport |
| `SessionBridge` | `SessionChannelTransport` | Consistent naming with other transports |
| `UnifiedConversation` | `ChannelLog` | Same concept as ConversationRoom — merge into one term |
| `ConversationMessage` | `ChannelLogEntry` | Entry in the channel log |
| `APIBridge` | `APIChannelTransport` | Consistent transport naming |

### 11.4 What this reveals about GNOT's design identity

The terminology drift toward "team," "room," and "human" in v6.3–v6.4 happened
because the primary use case being discussed was a dev team. But GNOT's core
abstraction is **generative node orchestration** — the dev team is one instance
of a cluster, not the defining concept.

Correct framing:
- A **cluster** is a set of nodes provisioned to achieve a shared goal
- A **channel** (gateway) connects cluster members
- The **channel log** records interactions in that channel
- **External participants** (human or otherwise) join a channel through an **external adapter**
- **Transport** is how events reach participants (session, Telegram, webhook, etc.)

This framing allows GNOT to orchestrate any type of cluster — dev teams,
data pipelines, monitoring networks, customer support operations — without
the core specs being polluted by domain-specific vocabulary.

