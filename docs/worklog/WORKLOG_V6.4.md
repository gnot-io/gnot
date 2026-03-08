# Worklog — Execution Mesh v6.4
## Multi-Human Participation · Role-Based Routing · Conversation Room

**Project:** ai-infra-runtime-v2
**Base:** v6.3 → v6.4
**Session:** Architecture review, 2026-03-08
**Status:** Analysis complete — design pending

---

## 1. Trigger và clarifications

Product owner đặt yêu cầu:
> "Team phải cho nhiều human tham gia vào để có thể trả lời những câu hỏi mà
> team có thể cần hỏi. Ví dụ: cho phép 1 human PM ở Việt Nam tham gia và 1
> human deployment engineer ở Mỹ tham gia để có thể trả lời những câu hỏi
> có liên quan. Tức có cơ chế cho nhiều human tham gia vào 1 conversation/chat session."

Sau khi phân tích sơ bộ, em đề xuất 6 sub-problems:
- H1: Human participant registry
- H2: Question routing logic
- H3: Shared conversation room
- H4: Multi-human reply semantics
- H5: Cross-timezone async
- H6: Human-to-human tagging

Product owner clarify ba nguyên tắc — những clarifications này **loại bỏ H2 và H5**:

> "1. Không nhất thiết phải target ông A, ông B cụ thể mà chỉ cần target role."

→ H2 (routing logic) simplified: không cần smart routing, chỉ cần role match.

> "2. Không cần aware đến timezone. Chỉ cần hỏi, và nếu câu hỏi được giải đáp
>    bởi người có role tương ứng là OK."

→ H5 (cross-timezone async) eliminated: không cần presence tracking.

> "3. 1 human tham gia có thể có nhiều role."

→ H1 (registry) extended: roles là list, không phải single value.

---

## 2. Key design decisions

### 2.1 Role-based vs. person-based targeting

v6.2 `suspend_and_ask` có `ask_node` field — target một agent cụ thể.
Khi extend cho humans, có thể giữ nguyên pattern: `ask_human: "nguyen-pm"`.

Nhưng product owner explicit: target role, không target person.

**Tại sao role-based đúng hơn:**

1. **Resilient**: Nếu Nguyen nghỉ phép, John có thể assume PM role. Agent không cần biết ai đang cover.

2. **Scalable**: Khi team grow và thêm PM, agent không cần update code.

3. **Natural**: Real teams work this way — "hỏi PM" không phải "hỏi Nguyen".

4. **Simpler for LLM**: LLM biết question type thuộc domain nào (PM, DevOps...) dễ hơn biết tên người.

**Implementation:** `ask_role: "pm"` trong `suspend_and_ask` params.
`ask_node` vẫn giữ cho agent-to-agent questions.

### 2.2 First-valid-answer semantics

Khi nhiều participants có cùng role, ai answer first wins.

**Alternative: consensus model**
Cả hai PMs phải đồng ý. Nhưng:
- Complex state machine
- Can deadlock nếu một PM không response
- Over-engineered cho hầu hết cases

**Alternative: hierarchy/seniority**
Senior PM's answer overrides junior. Nhưng:
- Cần hierarchy model trong ParticipantRegistry
- Adds complexity không yêu cầu
- Product owner không mention hierarchy

**First-wins là đủ.** Comments và tags cho phép second participant add context
mà không block resolution. Natural behavior.

### 2.3 Full room visibility cho all participants

Mọi participant thấy tất cả questions, kể cả những câu hỏi không thuộc role của họ.

Có thể debate: PM có cần thấy infra questions không?

**Yes, full visibility:**
- Context: PM cần biết team đang gặp vấn đề gì để manage priorities
- Collaboration: PM thấy infra question, add comment về business impact
- Trust: humans trust system hơn khi họ thấy toàn bộ activity
- Audit: full history cho post-mortem

The comment và tag mechanisms allow non-expert participants to contribute
context without having "answer authority."

### 2.4 Dynamic role expansion

Scenario: no participant has required role → timeout → assumption.

Có thể accept này vì product owner nói "không cần aware timezone."
Nếu không ai có role "legal", câu hỏi timeout và dùng assumption. Fine.

Nhưng trong /intent session, khi HI-LLM thông báo "không có ai có role legal,"
Nguyen có thể self-assign. LLM offers this option naturally.

Đây không phải forced feature — LLM discovers and offers organically.
Implementation: PATCH /participants/{id} để update roles.

### 2.5 Conversation Room — append-only

Room là append-only. Replies không bị xóa.

Rationale: audit trail. Trong dự án thực tế, "ai đã quyết định gì và khi nào"
là critical information. Một PM có thể answer một câu hỏi, sau đó change mind.
Lịch sử vẫn visible.

Storage: JSONL (consistent với GNOT append-only philosophy).

### 2.6 /intent session bridge là recommended mode

Ba notification modes: webhook, polling, session.

Session là recommended vì:

1. **Natural UX**: Không cần separate UI. Humans đã familiar với chat interfaces.

2. **Contextual**: LLM có thể present question với full context, ask follow-up
   nếu answer không clear, format và submit properly.

3. **Conversational**: Human types naturally, LLM extracts structured answer.
   No form to fill, no JSON to write.

4. **Consistent**: Same UX as using Claude normally — familiar interface.

Webhook và polling serve integration use cases (existing systems, bots).
Session serves direct human usage.

### 2.7 Human-interface node per team

One human-interface node per team, not shared.

Consistency argument: gateway-as-channel (v6.1) means each team is isolated.
Human-interface is part of the team. It registers with team's gateway.
All participants register with team's human-interface.

Cross-team sharing would require human-interface to join multiple gateways
and manage participant registries across teams — complex and breaks isolation.

Workaround for humans who participate in multiple teams: register with each
team's human-interface separately. Same human, different participant records,
different auth tokens. Clean separation.

---

## 3. What v6.4 reveals about previous designs

### 3.1 v6.2 "HumanInterface" was under-designed

v6.2 described HumanInterface as a "node pattern" with three delivery options.
This was appropriate for v6.2's scope (single human) but insufficient for v6.4.

v6.4 transforms HumanInterface from a pattern into a proper subsystem with:
- ParticipantRegistry (new component)
- ConversationRoom (new component)
- QuestionRouter (new component)
- Three formal delivery modes with per-participant configuration

The v6.2 sketch was correct in direction, just not yet detailed enough.

### 3.2 correlation_id design from v6.0 pays off

v6.0 designed correlation_id on Event for "linking events in a workflow."
This now serves as the question_id that connects:
- Agent's checkpoint (CheckpointStore) → via question_id
- ConversationRoom thread → via question_id
- human.answered event → via correlation_id = question_id

One ID threads through the entire suspension → questioning → answering → resumption
lifecycle. The v6.0 design decision was forward-looking.

### 3.3 The v6.4 human model mirrors the v6.1 node model

Interesting parallel:

```
v6.1 nodes:                    v6.4 humans:
  Gateway = channel authority    Human-interface = room authority
  Register with gateway          Register with human-interface
  = join channel                 = join conversation room
  
  Node has roles (actions)       Human has roles (expertise domains)
  Multi-gateway member           Register with multiple teams
  Full member equality           Full room visibility
  
  Events routed by subscription  Questions routed by role match
```

The human participation model naturally mirrors the node participation model.
This is not coincidental — both model "participants with capabilities."

---

## 4. The Slack analogy

The ConversationRoom is best understood as a Slack channel:

```
Real Slack:                        GNOT ConversationRoom:
  @pm: should we delete data?    →  agent asks, required_role: pm
  PM answers in thread           →  participant with pm role answers
  Others can comment             →  comment reply type
  @john can you check this?      →  tag reply type
  Thread history preserved       →  JSONL append-only
  Everyone can read              →  full visibility
  Different channels per project →  separate room per team
```

The key difference: in Slack, humans initiate. In GNOT ConversationRoom,
agents initiate and humans respond. Humans can also add context proactively.

---

## 5. What remains after v6.4

### 5.1 P4 items identified

**Cross-team human escalation:**
If team-A has no DevOps participant, could it escalate to team-B's DevOps?
Technically: human-interface-team-A could forward `human.input_required` to
seed, which routes to team-B's human-interface. But this breaks team isolation.
Better: deploy DevOps participant into team-A directly.

**Group /intent session:**
Multiple humans in one /intent session — like a group chat. PM and DevOps
could discuss in the same session and jointly answer a question.
This requires multi-user session management which ConversationStore doesn't support.

**Answer hierarchy:**
If senior PM and junior PM both answer, senior takes priority.
Requires role hierarchy model and "seniority" metadata on HumanParticipant.

**Shared human-interface:**
One human-interface node that spans multiple teams. More efficient (one node
instead of N) but requires cross-team participant management.

### 5.2 The remaining 10%

After v6.4, human-AI collaboration is substantially complete.
Remaining gaps are in human-to-human collaboration features:
- Group chat (multiple humans in one session)
- Real-time notifications (push vs. poll latency)
- Answer editing (humans can update answers after submission)
- Thread reactions (👍 / 👎 to signal agreement)

These are UX refinements, not architectural gaps.

---

## 6. Evolution summary

```
v6.2: "There should be a way for humans to answer agent questions"
      → Sketch: single user, three delivery options, no routing

v6.4: "Multiple humans, different roles, shared conversation"
      → Full design: ParticipantRegistry, ConversationRoom, QuestionRouter
      → Role-based routing (not person-based)
      → Async-first (no timezone complexity)
      → Three delivery modes per participant
      → Full room visibility for all participants
      → Comments and tags for collaborative context-adding
```

The simplifications from product owner (role-based, no timezone, multi-role)
made the design cleaner and more correct than the initial sketch in v6.2.

---

*Worklog: WORKLOG_V6.4.md | Mesh Runtime v6.4 | Repository: ai-infra-runtime-v2*
*Previous: WORKLOG_V6.3.md*
*Problem statement: product owner review session, 2026-03-08*
