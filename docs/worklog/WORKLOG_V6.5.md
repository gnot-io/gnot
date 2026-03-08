# Worklog — Execution Mesh v6.5
## Multi-Channel Human Participation · Telegram · Session Push

**Project:** ai-infra-runtime-v2
**Base:** v6.4 → v6.5
**Session:** Architecture review, 2026-03-08
**Status:** Two directions documented — pending decision

---

## 1. Trigger

Product owner hỏi:

> "Thiết kế hiện tại có thể cho phép user dùng Telegram để tương tác với hệ
> thống không em? Cụ thể: human A thông qua Claude web khởi tạo 1 conversation,
> và human B cũng có thể tham gia conversation này và human B có thể dùng
> Telegram để giao tiếp?"

Câu hỏi này chứa hai requirements:
1. Telegram support
2. "Cùng conversation" — A và B share cùng một experience

---

## 2. Analysis approach

### 2.1 Tách hai requirements

**Requirement 1: Telegram support**
→ Outbound: GNOT gửi questions đến Telegram
→ Inbound: John replies từ Telegram về GNOT
→ v6.4 webhook mode đã có outbound. Cần inbound + Telegram-specific UX.

**Requirement 2: "Cùng conversation"**
→ Câu này ambiguous — hai interpretations:
→ (a) Shared data: cả hai tác động lên cùng ConversationRoom → đã có v6.4
→ (b) Shared experience: A thấy B's messages real-time → CHƯA có

Nếu chỉ cần (a): Hướng 1 đủ.
Nếu cần (b): Hướng 2 cần thiết.

Product owner dùng từ "tham gia conversation này" — gợi ý (b).
Nhưng không explicit. Cả hai hướng được document để có cơ sở quyết định.

### 2.2 Đọc lại v6.4 trước khi conclude

Tránh over-design. Check xem v6.4 đã cover gì:

- `notify_via: webhook` với `notify_target` → GNOT fires POST khi có question ✅
- `POST /room/{team_id}/threads/{qid}/reply` → nhận answers từ bất kỳ HTTP client ✅
- ConversationRoom là shared state ✅

**Gap thực sự:** GNOT fire POST đến webhook là one-way. Telegram là two-way app.
Cần adapter handle inbound Telegram messages.

**Gap thực sự 2:** /intent session là HTTP request-response. Server không push
proactively. Nguyen phải manually query room để biết John đã answer.

---

## 3. Design decisions

### 3.1 TelegramAdapter là GNOT node (không phải external service)

Có thể build Telegram adapter như một completely separate service,
không liên quan đến GNOT mesh.

Nhưng: làm nó như một GNOT node có nhiều advantages:
- Consistent với mesh philosophy (everything is a node)
- Nhận events từ EventBus (không cần polling gateway)
- Tự provision bởi TeamWirer (v6.3) như các nodes khác
- Health-check, heartbeat, rollback theo cùng mechanism
- Skills.md describes its behavior → LLM-tunable

**Quyết định: TelegramAdapter là GNOT node.**

### 3.2 Telegram Group = Team Room cho Telegram-side users

Hai options cho Telegram UX:

**Option A: DM each participant separately**
- Bot DMs John khi có question targeting devops
- Private, không ai khác thấy
- Pros: clean, no noise
- Cons: no cross-visibility, no group discussion

**Option B: Telegram Group (chosen)**
- Tất cả Telegram-using participants trong cùng group
- Bot posts questions to group, everyone sees
- Group discussion visible to all
- Pros: natural group chat, cross-visibility, @mentions work
- Cons: potential noise if many simultaneous questions

**Quyết định: Option B — Telegram Group.**

Rationale: product owner's intent is "nhiều human tham gia 1 conversation."
Group chat directly realizes this — all Telegram users share one conversation.
Private DMs would fragment the conversation.

Option A có thể là config: `use_dm: true` nếu operator muốn privacy.

### 3.3 Reply-to-message vs /command syntax

Telegram supports message threading (reply to specific message).
This is more natural than `/answer q-abc123 text`.

**Quyết định: reply-to-message là primary UX, /command là fallback.**

```
Primary: John sees question message, replies directly to it
→ TelegramBridge detects reply_to_message.message_id
→ Lookup: message_id → question_id (via message_id_map)
→ Submit answer

Fallback: /answer <text> (for cases where reply-to context is lost)
```

### 3.4 SSE over WebSocket cho session push

Hướng 2 cần session push. SSE vs WebSocket:

SSE advantages:
- HTTP/1.1 native — no protocol upgrade needed
- Claude web likely uses HTTP streaming already (for LLM token streaming)
- Simpler server implementation (FastAPI natively supports)
- One-way push is all we need (room events → session)
- Auto-reconnect built into browser EventSource API

WebSocket advantages:
- Bidirectional
- Lower overhead for frequent messages
- Better for real-time games, live collaboration

For GNOT use case (room events push, not high-frequency), SSE is sufficient.
WebSocket when/if needed later (typing indicators, etc.).

**Quyết định: SSE for v6.5.**

### 3.5 UnifiedConversation replaces ConversationRoom

Hướng 2 introduces UnifiedConversation as the central state store.
This replaces (or wraps) v6.4's ConversationRoom.

Design question: should UnifiedConversation be a new class that
wraps ConversationRoom, or a full replacement?

**Quyết định: UnifiedConversation extends/replaces ConversationRoom.**

Rationale:
- ConversationRoom in v6.4 is newly designed — not yet implemented
- Better to get the design right than maintain two parallel stores
- UnifiedConversation is a superset: has all ConversationRoom functionality
  plus bridge management and message push

v6.4 ConversationRoom data model (QuestionThread, QuestionReply) is preserved
as an index/view within UnifiedConversation. API compatibility maintained.

### 3.6 Authentication for SSE stream

SSE stream is a long-lived HTTP connection. Authentication needed.

Options:
(a) Bearer token in Authorization header (same as /room endpoints)
(b) token query param: `/intent/stream/{session_id}?token=...`
(c) Cookies (session-based)

SSE với Authorization header: works in most clients but not browser EventSource
(EventSource doesn't support custom headers).

**Quyết định: Option (b) — token in query param.**

Not ideal for security (token in URL logs), but necessary for browser EventSource.
Mitigate: short-lived SSE tokens (separate from auth_token), expire in 1h.

---

## 4. Key insight: transport abstraction

Hướng 2's Bridge pattern reveals an important abstraction:

```
Before v6.5: "notify_via" is a participant property
  → Nguyen: notify_via=session
  → John: notify_via=webhook/telegram

After v6.5 (Hướng 2): channels are first-class
  → SessionBridge: manages all web session participants
  → TelegramBridge: manages all Telegram participants
  → APIBridge: manages all webhook/polling participants

Each Bridge is an independent component that:
  - Receives push from UnifiedConversation
  - Handles inbound from its channel
  - Formats messages for its channel's UX
```

This separation means adding Slack is: build SlackBridge, register with
UnifiedConversation. No changes to core GNOT nodes or agents.

Compare to v6.4 design where each participant's notify_via is processed
by QuestionRouter — adding new channels requires modifying QuestionRouter.

Bridge pattern scales better.

---

## 5. Phân tích: v6.4 → v6.5 evolution

### 5.1 v6.4 was designed for async-first

v6.4 nguyên tắc: "Async-first, không cần timezone awareness."
This was correct for the single-channel case (each participant in their own channel).

But when A and B are in "cùng conversation," async-first creates a disconnect:
- A doesn't know what B is doing
- B doesn't know what A is doing
- "Same conversation" feels like separate conversations

v6.5 Hướng 2 moves toward **sync-capable** (real-time optional, not mandatory).
Participants who need real-time can use SSE. Participants who are fine with async
can still use polling. Both work.

### 5.2 The fundamental tension: channels vs conversation

v6.4's model: conversation is the room, channels are delivery mechanisms.
The room is the source of truth. Channels are views into the room.

This is correct but incomplete: views are read-only. Channels need to be
able to write to the room (inbound). v6.4 only has webhook as inbound,
and Telegram is not a webhook (it's a push-based service with polling).

v6.5 resolves this by making channels full participants in the conversation
(Bridge pattern), not just delivery mechanisms.

### 5.3 The "same conversation" question

"Human A và B tham gia cùng conversation" — what does this mean exactly?

Level 1: Same data store. ✅ v6.4.
Level 2: See each other's contributions. ✅ v6.4 (via GET /room).
Level 3: See each other in real-time. ✅ v6.5 Hướng 2.
Level 4: Feel like they're in the same room together. → Requires UX work beyond GNOT.

v6.5 covers Levels 1-3. Level 4 depends on client UX (Claude web, Telegram client, etc.).

---

## 6. Recommendation rationale

"Implement Hướng 1 first, Hướng 2 later" — why:

1. **Hướng 1 delivers most of the value.** Telegram users can participate.
   Questions get answered. ConversationRoom is shared. The core use case works.

2. **Real-time cross-channel visibility (Hướng 2) is a nice-to-have.**
   In practice, async teams don't need real-time sync. PM answers when they see it.
   DevOps answers when they see it. The result is the same for the agent.

3. **SSE changes the /intent protocol.** This is a significant change to a
   core component. Should be done carefully, with proper testing.

4. **UnifiedConversation is an architectural refactor.** v6.4 ConversationRoom
   is not yet implemented. Replacing it with UnifiedConversation before it's
   built is easier than after. But it still requires careful design.

5. **Bridge pattern is extensible.** When Slack is needed, adding SlackBridge
   is straightforward. No need to redo the design.

Phase roadmap:
  v6.5a: TelegramAdapter node (Hướng 1) — ship fast
  v6.5b: SSE endpoint, partial push (sessions get room updates)
  v6.6: Full UnifiedConversation + all Bridges

---

## 7. Summary

```
v6.4: "Multiple humans, different roles, shared room data"
      → Each human in their own channel, async interaction
      → Room is shared but channels are isolated

v6.5: "Telegram support + unified conversation experience"
      → Hướng 1: TelegramAdapter node (MVP, Hướng 1-way sync)
      → Hướng 2: Session Push + Bridge pattern (full real-time, all channels)
      → Both directions documented for future analysis
```

---

*Worklog: WORKLOG_V6.5.md | Mesh Runtime v6.5 | Repository: ai-infra-runtime-v2*
*Previous: WORKLOG_V6.4.md*
*Problem statement: product owner review session, 2026-03-08*
