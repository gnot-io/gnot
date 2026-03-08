# AI-Orchestrated Self-Bootstrapping Execution Mesh
## Architecture Specification v6.6
### Persistent Agent · Session Persistence · Long-Term Memory · Context Management

**Base version:** v6.5
**Target version:** v6.6
**Status:** Analysis complete — design pending
**Authors:** Architecture review session, 2026-03-08

---

## 1. Yêu cầu

### 1.1 Requirement từ product owner

> "Hệ thống có thể hỗ trợ node như là persistent agent, có llm, bộ nhớ, lưu context
> theo session, có thể clear/reset nếu cần."

### 1.2 Decomposition

Yêu cầu này chứa 4 sub-requirements riêng biệt:

| Sub-requirement | Mô tả |
|-----------------|-------|
| **Persistent agent** | Node tồn tại lâu dài, nhớ context giữa các restart |
| **Có LLM** | Node có thể run /intent, reason, take actions |
| **Bộ nhớ** | Nhớ facts về user/project xuyên suốt nhiều sessions |
| **Lưu context theo session** | Conversation history được lưu, không mất khi restart |
| **Clear/reset** | Operator hoặc agent có thể xóa context khi cần |

---

## 2. Gap analysis — v5.13b baseline

### 2.1 Những gì đã có

Đọc source code `conversation_store.py`, `config.py`, `intent_handler.py`,
`server.py` v5.13b:

**✅ Node có LLM — đầy đủ:**
```python
# NodeConfig
llm_api_key: str | None = None
llm_base_url: str = "https://api.openai.com/v1"
llm_default_model: str = "gpt-4o-mini"
llm_timeout_seconds: float = 120.0

# server.py
if config.llm_enabled:
    llm_client = LLMClient(...)
    intent_handler = IntentHandler(...)
    # → POST /intent enabled
```

**✅ Context theo session — đầy đủ (trong session):**
```python
# IntentHandler.handle()
session = await self._store.get_or_create(req.session_id)
session.add_message("user", req.prompt)
messages = list(session.messages)   # full history → LLM
```

**✅ Persona/skills — có:**
`skills_file` trong `node.yaml` → content injected vào system prompt.
Agent biết "who it is" nhưng không thể thay đổi skills dynamically.

**✅ Clear/reset — partial:**
```
DELETE /sessions/{session_id}   → xóa cả session
GET /sessions/{session_id}      → xem history
GET /sessions                   → list all active sessions
```

### 2.2 Những gì chưa có — 4 gaps

---

#### Gap P1: Session không persist qua restart (CRITICAL)

Code comment trong `conversation_store.py` explicit:
```python
# On restart: sessions are lost (in-memory). This is intentional —
# session persistence is a P4 item; stateless retry is cleaner than stale history.
```

`ConversationStore` là **pure in-memory dict**:
```python
class ConversationStore:
    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, Session] = {}   # ← pure in-memory
        self._lock = asyncio.Lock()
```

**Consequence:** Node restart (crash, deploy, update) → tất cả sessions và
conversation history mất hoàn toàn. Persistent agent không thể persistent
nếu memory biến mất mỗi khi process restart.

---

#### Gap P2: Không có long-term memory (cross-session)

`ConversationStore` giữ message history của một session. Khi session expire
(TTL mặc định 3600s = 1 giờ), history mất.

**No mechanism để agent nhớ facts xuyên suốt nhiều sessions:**

```
Day 1, Session A: User says "I'm building a Python FastAPI backend"
                  Agent works on it.
Session A expires (1h TTL).

Day 2, Session B: User returns.
                  Agent: "What are you working on?" ← forgot everything
```

`skills_file` là read-only persona file — agent không thể write facts vào đó.
Không có writable persistent memory store.

So sánh với OpenClaw: agent có `memory.md` file mà LLM có thể read và write
qua actions. Facts persist xuyên suốt mọi sessions và restarts.

---

#### Gap P3: Session TTL không phù hợp cho persistent agent

Default `session_ttl_seconds = 3600` (1 giờ) được thiết kế cho short-lived
transactional sessions (Telegram chat, one-off queries).

Persistent agent cần TTL dài hơn nhiều:
- Một agent working on a project cần nhớ context **days hay weeks**
- Không có option `ttl = 0` (infinite / never expire)
- Không có per-session TTL override (mỗi session có thể có TTL khác nhau)
- Không có "last_active based" vs "created_at based" TTL distinction

---

#### Gap P4: `DELETE /sessions` = xóa hoàn toàn, không có `clear`

Hiện tại có duy nhất một operation để manage session state:
```
DELETE /sessions/{id}  → xóa hoàn toàn session và history
```

Với persistent agent, cần phân biệt:

| Operation | Mô tả | Use case |
|-----------|-------|----------|
| **clear context** | Xóa message history, giữ session alive, **giữ memory** | "Hãy bắt đầu chủ đề mới, nhưng nhớ project context" |
| **reset memory** | Xóa long-term memory entries (selective hoặc full) | "Quên thông tin về project A đi" |
| **delete session** | Xóa hoàn toàn session + memory | Hard reset |

---

## 3. Design — v6.6

### 3.1 Overview: hai new components

```
v5.13b:
  ConversationStore (in-memory) ← gap P1, P3, P4
  skills_file (read-only)       ← gap P2

v6.6 adds:
  PersistentSessionStore        → replaces ConversationStore
                                  file-based, survives restart, configurable TTL
  AgentMemoryStore              → new
                                  writable facts store, cross-session, structured
```

### 3.2 PersistentSessionStore

```python
# runtime/persistent_session_store.py

@dataclass
class PersistedSession:
    """
    A conversation session with full persistence.

    Stored as individual JSONL files:
        {sessions_dir}/{session_id}.jsonl

    Each line is one message (append-only).
    Session metadata stored in {session_id}.meta.json.

    Why JSONL per session (not one big file):
    - Concurrent sessions don't block each other
    - Easy to inspect/delete individual sessions
    - Append-only = crash-safe (no partial write corrupts other sessions)
    - grep-friendly for debugging
    """
    session_id: str
    created_at: float
    last_active: float
    ttl_seconds: int              # per-session TTL, overrides node default
    messages: list[dict]          # loaded into memory when session active
    node_id: str                  # which node owns this session
    tags: dict[str, str] = field(default_factory=dict)  # operator metadata


class PersistentSessionStore:
    """
    Drop-in replacement for ConversationStore with file-based persistence.

    Storage layout:
        {sessions_dir}/
            {session_id}.jsonl       ← messages (append-only)
            {session_id}.meta.json   ← session metadata

    On startup: scans sessions_dir, loads non-expired session metadata.
    Messages loaded lazily on first access (not all at startup).

    TTL:
        ttl_seconds = 0 → session never expires (infinite)
        ttl_seconds > 0 → expires after N seconds of inactivity
        Per-session TTL overrides node default.

    Thread-safety: asyncio.Lock per session (finer-grained than global lock).

    Public API (backward-compatible with ConversationStore):
        get_or_create(session_id?, ttl_seconds?) → PersistedSession
        get(session_id)                          → PersistedSession | None
        delete(session_id)                       → bool
        clear_messages(session_id)               → bool  [NEW]
        list_sessions()                          → list[PersistedSession]
        sweep_expired()                          → int
    """

    def __init__(
        self,
        sessions_dir: str,
        default_ttl_seconds: int = 0,    # 0 = never expire by default
        node_id: str = "",
    ) -> None:
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._default_ttl = default_ttl_seconds
        self._node_id = node_id
        self._sessions: dict[str, PersistedSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def startup_load(self) -> int:
        """
        Called at startup: scan sessions_dir, reload non-expired sessions.
        Returns count of sessions restored.

        This is what makes the store "persistent" — sessions survive restart.
        """
        restored = 0
        for meta_path in self._dir.glob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text())
                session = PersistedSession(**meta, messages=[])
                if not self._is_expired(session):
                    # Load messages from JSONL
                    jsonl_path = self._dir / f"{session.session_id}.jsonl"
                    if jsonl_path.exists():
                        session.messages = [
                            json.loads(line)
                            for line in jsonl_path.read_text().splitlines()
                            if line.strip()
                        ]
                    self._sessions[session.session_id] = session
                    restored += 1
                else:
                    # Expired — clean up files
                    meta_path.unlink(missing_ok=True)
                    (self._dir / f"{session.session_id}.jsonl").unlink(missing_ok=True)
            except Exception as e:
                logger.warning("Failed to restore session from %s: %s", meta_path, e)
        logger.info("PersistentSessionStore: restored %d session(s)", restored)
        return restored

    async def get_or_create(
        self,
        session_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> PersistedSession:
        sid = session_id or _new_session_id()
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl

        if sid in self._sessions:
            session = self._sessions[sid]
            if not self._is_expired(session):
                session.last_active = time.time()
                await self._persist_meta(session)
                return session
            await self.delete(sid)

        now = time.time()
        session = PersistedSession(
            session_id=sid,
            created_at=now,
            last_active=now,
            ttl_seconds=ttl,
            messages=[],
            node_id=self._node_id,
        )
        self._sessions[sid] = session
        await self._persist_meta(session)
        return session

    async def add_message(self, session_id: str, message: dict) -> None:
        """Append message to session — persists immediately."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session not found: {session_id}")
        session.messages.append(message)
        session.last_active = time.time()
        # Append-only write — crash safe
        jsonl_path = self._dir / f"{session_id}.jsonl"
        async with self._get_lock(session_id):
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(message) + "\n")
        await self._persist_meta(session)

    async def clear_messages(self, session_id: str) -> bool:
        """
        Clear conversation history but keep session alive.
        Session metadata (created_at, tags) preserved.
        Long-term memory (AgentMemoryStore) NOT touched.

        Returns True if session existed and was cleared.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session.messages = []
        session.last_active = time.time()
        async with self._get_lock(session_id):
            jsonl_path = self._dir / f"{session_id}.jsonl"
            jsonl_path.write_text("")   # truncate
        await self._persist_meta(session)
        logger.info("Session %s: messages cleared (session kept alive)", session_id)
        return True

    async def delete(self, session_id: str) -> bool:
        """Delete session completely — removes files."""
        existed = session_id in self._sessions
        self._sessions.pop(session_id, None)
        (self._dir / f"{session_id}.jsonl").unlink(missing_ok=True)
        (self._dir / f"{session_id}.meta.json").unlink(missing_ok=True)
        return existed

    async def _persist_meta(self, session: PersistedSession) -> None:
        meta = {
            "session_id": session.session_id,
            "created_at": session.created_at,
            "last_active": session.last_active,
            "ttl_seconds": session.ttl_seconds,
            "node_id": session.node_id,
            "tags": session.tags,
        }
        path = self._dir / f"{session.session_id}.meta.json"
        path.write_text(json.dumps(meta, indent=2))

    def _is_expired(self, session: PersistedSession) -> bool:
        if session.ttl_seconds == 0:
            return False   # infinite TTL
        return (time.time() - session.last_active) > session.ttl_seconds

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]
```

### 3.3 AgentMemoryStore

```python
# runtime/agent_memory_store.py

@dataclass
class MemoryEntry:
    """
    A single memory fact.

    Two types:
      "fact"      — structured key-value: "project_language" = "Python"
      "narrative" — free-form text: "User prefers concise responses without markdown"

    Scope:
      "global"    — remembered across all sessions on this node
      "session"   — remembered only for a specific session (cleared with session)
    """
    entry_id: str               # uuid4
    key: str                    # for "fact" type: lookup key
                                # for "narrative" type: topic label
    value: str                  # the memory content
    entry_type: str             # "fact" | "narrative"
    scope: str                  # "global" | "session:{session_id}"
    created_at: float
    updated_at: float
    source: str                 # "agent" | "user" | "system"
    confidence: float = 1.0     # 0.0–1.0, for agent-inferred memories


class AgentMemoryStore:
    """
    Persistent writable memory for agent nodes.

    Stores facts and narratives that agents learn and remember
    across sessions and restarts.

    Storage: {memory_dir}/memory.jsonl (append-only log)
              {memory_dir}/memory_index.json (current state, rebuilt on startup)

    Why two files:
      memory.jsonl  = audit trail (all changes, never deleted)
      memory_index  = current state (fast lookup, rebuilt from log on startup)

    The LLM can read and write memory via two seed actions:
      agent_remember(key, value, type, scope)  → stores a memory
      agent_recall(query?, scope?)             → retrieves memories
      agent_forget(key?, scope?)               → removes memories

    Public API:
        remember(entry) → MemoryEntry
        recall(query?, scope?, entry_type?) → list[MemoryEntry]
        forget(key?, scope?) → int (count removed)
        recall_for_prompt(session_id) → str  (formatted for system prompt injection)
        load() → int (entries loaded from disk)
    """

    def __init__(self, memory_dir: str, node_id: str) -> None:
        self._dir = Path(memory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._node_id = node_id
        self._entries: dict[str, MemoryEntry] = {}   # entry_id → entry
        self._index: dict[str, str] = {}             # key+scope → entry_id (latest)

    async def load(self) -> int:
        """Load current memory state from index file on startup."""
        index_path = self._dir / "memory_index.json"
        if not index_path.exists():
            return 0
        try:
            data = json.loads(index_path.read_text())
            for raw in data.get("entries", []):
                entry = MemoryEntry(**raw)
                self._entries[entry.entry_id] = entry
                self._index[f"{entry.scope}:{entry.key}"] = entry.entry_id
            logger.info("AgentMemoryStore: loaded %d memory entries", len(self._entries))
            return len(self._entries)
        except Exception as e:
            logger.warning("AgentMemoryStore: failed to load index: %s", e)
            return 0

    async def remember(
        self,
        key: str,
        value: str,
        entry_type: str = "fact",
        scope: str = "global",
        source: str = "agent",
        confidence: float = 1.0,
    ) -> MemoryEntry:
        """
        Store a memory. If key+scope already exists, updates it.
        Appends to audit log. Updates index.
        """
        idx_key = f"{scope}:{key}"
        now = time.time()

        if idx_key in self._index:
            # Update existing
            existing = self._entries[self._index[idx_key]]
            existing.value = value
            existing.updated_at = now
            existing.confidence = confidence
            entry = existing
        else:
            entry = MemoryEntry(
                entry_id=str(uuid.uuid4()),
                key=key,
                value=value,
                entry_type=entry_type,
                scope=scope,
                created_at=now,
                updated_at=now,
                source=source,
                confidence=confidence,
            )
            self._entries[entry.entry_id] = entry
            self._index[idx_key] = entry.entry_id

        await self._append_log({"op": "remember", **asdict(entry)})
        await self._save_index()
        return entry

    async def recall(
        self,
        query: str | None = None,
        scope: str | None = None,
        entry_type: str | None = None,
        session_id: str | None = None,
    ) -> list[MemoryEntry]:
        """
        Retrieve memories matching filters.

        scope=None → return global + session-scoped entries
        query      → simple substring match on key or value
        """
        results = []
        for entry in self._entries.values():
            # Scope filter
            if scope and entry.scope != scope:
                continue
            if scope is None and session_id:
                # Return global + this session's entries
                if entry.scope != "global" and entry.scope != f"session:{session_id}":
                    continue
            # Type filter
            if entry_type and entry.entry_type != entry_type:
                continue
            # Query filter (simple substring)
            if query and query.lower() not in entry.key.lower() \
                     and query.lower() not in entry.value.lower():
                continue
            results.append(entry)

        return sorted(results, key=lambda e: e.updated_at, reverse=True)

    async def forget(
        self,
        key: str | None = None,
        scope: str | None = None,
    ) -> int:
        """
        Remove memories matching key and/or scope.

        forget(key="project_language")        → remove one fact
        forget(scope="session:ses-abc")        → clear session-scoped memories
        forget(scope="global")                 → clear all global memories
        """
        to_remove = []
        for entry in self._entries.values():
            if key and entry.key != key:
                continue
            if scope and entry.scope != scope:
                continue
            to_remove.append(entry.entry_id)

        for eid in to_remove:
            entry = self._entries.pop(eid, None)
            if entry:
                idx_key = f"{entry.scope}:{entry.key}"
                self._index.pop(idx_key, None)
                await self._append_log({"op": "forget", "entry_id": eid, "key": entry.key, "scope": entry.scope})

        if to_remove:
            await self._save_index()
        return len(to_remove)

    def recall_for_prompt(self, session_id: str | None = None) -> str:
        """
        Format memories as text block for injection into LLM system prompt.

        Example output:
          [Memory]
          project_language: Python (FastAPI)
          user_preference: Concise responses, no markdown headers
          last_task: Implementing user authentication module
        """
        entries = []
        for entry in self._entries.values():
            if entry.scope == "global":
                entries.append(entry)
            elif session_id and entry.scope == f"session:{session_id}":
                entries.append(entry)

        if not entries:
            return ""

        lines = ["[Memory]"]
        # Facts first, then narratives
        for e in sorted(entries, key=lambda e: (e.entry_type != "fact", e.key)):
            lines.append(f"{e.key}: {e.value}")
        return "\n".join(lines)

    async def _append_log(self, record: dict) -> None:
        log_path = self._dir / "memory.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps({**record, "timestamp": time.time()}) + "\n")

    async def _save_index(self) -> None:
        index_path = self._dir / "memory_index.json"
        index_path.write_text(json.dumps(
            {"entries": [asdict(e) for e in self._entries.values()]},
            indent=2
        ))
```

### 3.4 Memory injection into LLM system prompt

```python
# runtime/intent_handler.py — v6.6 additions

class IntentHandler:

    def __init__(
        self,
        ...,
        memory_store: "AgentMemoryStore | None" = None,  # NEW v6.6
    ) -> None:
        ...
        self._memory = memory_store

    async def handle(self, req: IntentRequest) -> IntentResponse:
        session = await self._store.get_or_create(req.session_id)
        ...
        system_prompt = (
            self._config.intent_system_prompt
            or self._build_system_prompt(node_hint=req.node_hint)
        )

        # v6.6: inject memory into system prompt
        if self._memory:
            memory_block = self._memory.recall_for_prompt(session_id=session.session_id)
            if memory_block:
                system_prompt = f"{system_prompt}\n\n{memory_block}"

        ...
```

**Memory is invisible to the LLM message history** — it lives in the system
prompt, not in messages. This means:
- Memory is always present at every turn (not forgotten mid-conversation)
- Memory doesn't inflate the message history / context window
- Memory can be updated without modifying past messages

### 3.5 Three new seed actions

```python
# seed/actions/agent_remember.py

"""
Agent can store a fact or narrative to long-term memory.

Usage by LLM:
  mesh_action("self", "agent_remember", {
    "key": "project_language",
    "value": "Python with FastAPI",
    "type": "fact",           # "fact" | "narrative"
    "scope": "global",        # "global" | "session"
  })
"""

SCHEMA = {
    "key":   {"type": "string", "description": "Memory key (lookup label)"},
    "value": {"type": "string", "description": "Memory content"},
    "type":  {"type": "string", "enum": ["fact", "narrative"], "default": "fact"},
    "scope": {"type": "string", "enum": ["global", "session"], "default": "global"},
}

async def run(params: dict, context: dict) -> dict:
    memory_store = context.get("memory_store")
    if not memory_store:
        return {"error": "memory_store not available"}
    entry = await memory_store.remember(
        key=params["key"],
        value=params["value"],
        entry_type=params.get("type", "fact"),
        scope=params.get("scope", "global"),
        source="agent",
    )
    return {"remembered": True, "entry_id": entry.entry_id, "key": entry.key}
```

```python
# seed/actions/agent_recall.py

"""
Agent retrieves memories.

Usage:
  mesh_action("self", "agent_recall", {})
  → returns all global memories

  mesh_action("self", "agent_recall", {"query": "project"})
  → returns memories matching "project"
"""

async def run(params: dict, context: dict) -> dict:
    memory_store = context.get("memory_store")
    if not memory_store:
        return {"error": "memory_store not available"}
    entries = await memory_store.recall(
        query=params.get("query"),
        scope=params.get("scope"),
        session_id=context.get("session_id"),
    )
    return {
        "memories": [
            {"key": e.key, "value": e.value, "type": e.entry_type, "scope": e.scope}
            for e in entries
        ],
        "count": len(entries),
    }
```

```python
# seed/actions/agent_forget.py

"""
Agent removes memories.

Usage:
  mesh_action("self", "agent_forget", {"key": "project_language"})
  mesh_action("self", "agent_forget", {"scope": "session"})
  mesh_action("self", "agent_forget", {})   → clear all global memories
"""

async def run(params: dict, context: dict) -> dict:
    memory_store = context.get("memory_store")
    if not memory_store:
        return {"error": "memory_store not available"}
    removed = await memory_store.forget(
        key=params.get("key"),
        scope=params.get("scope"),
    )
    return {"forgotten": removed}
```

---

## 4. New HTTP endpoints — v6.6

### 4.1 `POST /sessions/{session_id}/clear` (new)

```
POST /sessions/{session_id}/clear
Authorization: Bearer {auth_token}

Response 200:
{
  "session_id": "ses-abc123",
  "cleared": true,
  "messages_removed": 42,
  "memory_preserved": true    ← long-term memory NOT touched
}

Response 404:
{"error": "SESSION_NOT_FOUND"}
```

Clears conversation history. Session stays alive. Memory untouched.
Use case: "start fresh conversation, but keep what I know about you."

### 4.2 `GET /memory` (new)

```
GET /memory?scope=global&query=project
Authorization: Bearer {auth_token}

Response 200:
{
  "memories": [
    {"key": "project_language", "value": "Python with FastAPI",
     "type": "fact", "scope": "global", "updated_at": 1741392000.0},
    {"key": "project_name", "value": "E-commerce backend",
     "type": "fact", "scope": "global", "updated_at": 1741391000.0}
  ],
  "count": 2
}
```

### 4.3 `POST /memory` (new)

```
POST /memory
Authorization: Bearer {auth_token}
{
  "key": "project_language",
  "value": "Python with FastAPI",
  "type": "fact",
  "scope": "global",
  "source": "user"
}

Response 201:
{"entry_id": "mem-uuid", "key": "project_language", "remembered": true}
```

Operator or external system can inject memories (not just agent).

### 4.4 `DELETE /memory` (new)

```
DELETE /memory?key=project_language&scope=global
Authorization: Bearer {auth_token}

Response 200:
{"forgotten": 1}
```

### 4.5 `GET /sessions/{session_id}` — updated response

```
Response 200 (updated):
{
  "session_id": "ses-abc123",
  "created_at": 1741392000.0,
  "last_active": 1741395600.0,
  "age_seconds": 3600.0,
  "ttl_seconds": 0,           ← NEW: 0 = infinite
  "turn_count": 24,
  "messages": [...],
  "memory_count": 5            ← NEW: how many memories exist for this session scope
}
```

---

## 5. node.yaml changes — v6.6

```yaml
# node.yaml — v6.6 additions

node_id: agent-analyst
listen: 0.0.0.0:8092

llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
  api_key: "${ANTHROPIC_API_KEY}"

# v6.6 — Persistent session config
session:
  backend: "persistent"          # "memory" (v5.x default) | "persistent" (v6.6)
  storage_dir: ./sessions        # where to store session files
  default_ttl_seconds: 0         # 0 = infinite, any positive int = expire after N idle seconds
  max_messages_per_session: 0    # 0 = unlimited, N = keep last N messages (context window mgmt)

# v6.6 — Long-term memory config
memory:
  enabled: true
  storage_dir: ./memory
  auto_remember: true            # agent can call agent_remember action
  inject_into_prompt: true       # auto-inject memories into system prompt
  max_entries: 1000              # soft cap (oldest entries pruned when exceeded)
```

**NodeConfig additions (v6.6):**

```python
# runtime/config.py additions

# v6.6 — Session persistence
session_backend: str = "memory"              # "memory" | "persistent"
session_storage_dir: str = "./sessions"
session_default_ttl_seconds: int = 0         # 0 = infinite
session_max_messages: int = 0                # 0 = unlimited

# v6.6 — Long-term memory
memory_enabled: bool = False
memory_storage_dir: str = "./memory"
memory_auto_remember: bool = True
memory_inject_into_prompt: bool = True
memory_max_entries: int = 1000
```

---

## 6. Backward compatibility

### 6.1 ConversationStore preserved as "memory" backend

```python
# server.py — v6.6

if config.session_backend == "persistent":
    session_store = PersistentSessionStore(
        sessions_dir=config.session_storage_dir,
        default_ttl_seconds=config.session_default_ttl_seconds,
        node_id=config.node_id,
    )
    await session_store.startup_load()
else:
    # v5.x behavior — backward compat
    session_store = ConversationStore(ttl_seconds=config.session_ttl_seconds)
```

Nodes without `session.backend: persistent` in `node.yaml` behave exactly
as before. Zero breaking change.

### 6.2 IntentRequest backward compat

`IntentRequest` unchanged. `session_id` field used as-is.
`PersistentSessionStore.get_or_create()` accepts same `session_id` parameter.

---

## 7. BootstrapRequest — v6.6 additions

```python
# runtime/bootstrap.py additions for v6.6

@dataclass
class SessionSpec:
    backend: str = "persistent"
    storage_dir: str = "./sessions"
    default_ttl_seconds: int = 0
    max_messages: int = 0

@dataclass
class MemorySpec:
    enabled: bool = True
    storage_dir: str = "./memory"
    auto_remember: bool = True
    inject_into_prompt: bool = True
    max_entries: int = 1000

@dataclass
class BootstrapRequest:
    # ... (all previous fields)
    session: SessionSpec | None = None    # NEW v6.6
    memory: MemorySpec | None = None      # NEW v6.6
```

TeamWirer (now ClusterOrchestrator) wires `session` and `memory` for each
persistent agent node:

```python
# For persistent agent nodes in ClusterSpec
NodeSpec(
    node_id="analyst-cluster-A",
    role="analyst",
    bootstrap=BootstrapRequest(
        ...
        session=SessionSpec(backend="persistent", default_ttl_seconds=0),
        memory=MemorySpec(enabled=True, auto_remember=True),
    )
)
```

---

## 8. Files thay đổi — v6.6

| File | Type | Description |
|------|------|-------------|
| `runtime/persistent_session_store.py` | NEW | File-based session persistence, survives restart |
| `runtime/agent_memory_store.py` | NEW | Long-term memory (facts + narratives), cross-session |
| `runtime/config.py` | MODIFY | SessionSpec, MemorySpec config fields; `session_backend`, `memory_*` |
| `runtime/server.py` | MODIFY | Session backend selection; `/sessions/{id}/clear`; `/memory` CRUD endpoints |
| `runtime/intent_handler.py` | MODIFY | Memory injection into system prompt; memory_store dependency |
| `runtime/bootstrap.py` | MODIFY | SessionSpec + MemorySpec in BootstrapRequest; `_step_write_config` |
| `runtime/models.py` | MODIFY | SessionInfo + ttl_seconds + memory_count; MemoryEntry response model |
| `seed/actions/agent_remember.py` | NEW | LLM action: store memory |
| `seed/actions/agent_recall.py` | NEW | LLM action: retrieve memories |
| `seed/actions/agent_forget.py` | NEW | LLM action: remove memories |
| `seed/actions/schemas/agent_remember.json` | NEW | Schema for agent_remember |
| `seed/actions/schemas/agent_recall.json` | NEW | Schema for agent_recall |
| `seed/actions/schemas/agent_forget.json` | NEW | Schema for agent_forget |

---

## 9. End-to-end scenario — Persistent Agent

```
═══════════════════════════════════════════════════════════
SETUP
═══════════════════════════════════════════════════════════

analyst-cluster-A node provisioned with:
  session.backend: persistent
  session.default_ttl_seconds: 0        ← never expire
  memory.enabled: true
  memory.inject_into_prompt: true

═══════════════════════════════════════════════════════════
Day 1, Session ses-abc
═══════════════════════════════════════════════════════════

User: POST /intent {session_id: "ses-abc", prompt: "Let's work on user auth module"}

LLM: "Sure! Before we start, tell me about the tech stack..."

User: "Python FastAPI, PostgreSQL, JWT tokens"

LLM decides to remember:
  mesh_action("self", "agent_remember", {
    "key": "project_tech_stack",
    "value": "Python FastAPI, PostgreSQL, JWT tokens",
    "type": "fact", "scope": "global"
  })
  → MemoryStore: {project_tech_stack: "Python FastAPI..."}

LLM: "Got it. I'll keep FastAPI + PostgreSQL + JWT in mind."
     [works on auth module for 10 turns]

[Node is restarted for maintenance]
  → PersistentSessionStore.startup_load():
      Restored session ses-abc (42 messages)
  → AgentMemoryStore.load():
      Loaded 3 memory entries

═══════════════════════════════════════════════════════════
Day 2, Session ses-abc (same session ID, resumed)
═══════════════════════════════════════════════════════════

User: POST /intent {session_id: "ses-abc", prompt: "Continue where we left off"}

System prompt injected by IntentHandler:
  [Memory]
  project_tech_stack: Python FastAPI, PostgreSQL, JWT tokens
  auth_module_status: In progress — login endpoint done, logout pending
  user_preference: Show code diffs, not full files

LLM: "Welcome back! We were working on the auth module.
     Login endpoint is done. Next up: logout endpoint.
     Should I start with the route or the token invalidation logic?"

[Full 42-message history loaded → complete context preserved]

═══════════════════════════════════════════════════════════
Day 2, Session ses-xyz (new session, same node)
═══════════════════════════════════════════════════════════

User: POST /intent {session_id: "ses-xyz", prompt: "What project are we working on?"}

System prompt injected:
  [Memory]
  project_tech_stack: Python FastAPI, PostgreSQL, JWT tokens
  ...  ← global memories shared across sessions

LLM: "We're working on a Python FastAPI backend with PostgreSQL and JWT authentication."

[New session — no message history, but global memory preserved]

═══════════════════════════════════════════════════════════
User wants a fresh start
═══════════════════════════════════════════════════════════

User: POST /sessions/ses-abc/clear
→ {cleared: true, messages_removed: 42, memory_preserved: true}

LLM next call on ses-abc:
  System prompt still has [Memory] block ← memory intact
  session.messages = []                  ← conversation history gone

User: "What do you know about this project?"
LLM: "I know we're using FastAPI + PostgreSQL + JWT, and the auth module
     was in progress. Fresh context — where would you like to start?"

═══════════════════════════════════════════════════════════
Complete memory reset
═══════════════════════════════════════════════════════════

DELETE /memory?scope=global
→ {forgotten: 3}

DELETE /sessions/ses-abc
→ {deleted: true}

LLM on next call: completely fresh, no memory, no history.
```

---

## 10. Context window management

Với `session_max_messages > 0`, PersistentSessionStore enforces a sliding
window on message history:

```python
# When loading session for LLM call:
if self._config.session_max_messages > 0:
    messages = session.messages[-self._config.session_max_messages:]
else:
    messages = list(session.messages)

# All messages still stored on disk (full history preserved)
# Only a window is passed to LLM (avoid context overflow)
```

**Typical configuration for long-running agents:**

```yaml
session:
  backend: persistent
  default_ttl_seconds: 0
  max_messages_per_session: 100    # last 100 messages to LLM
                                   # full history on disk
```

Memory compensates for truncated history:
- Old conversation details → agent_remember → persist as facts
- Summary of completed work → agent_remember as narrative
- LLM sees fresh 100-message window + rich memory block

---

## 11. Open questions — v6.6

**Q1: Memory injection position in system prompt**
Should `[Memory]` block come before or after skills content?
Recommendation: after skills (persona first, then context).
Skills are static identity; memory is dynamic context.

**Q2: Auto-memory extraction**
Should the LLM be prompted to extract and remember facts proactively?
Option A: LLM decides when to call `agent_remember` (manual)
Option B: After each session end, a summarization pass extracts facts
Option C: Both — LLM remembers during, system summarizes after

v6.6 implements Option A. Options B/C are enhancements for v6.7.

**Q3: Memory privacy across clusters**
Analyst node in cluster-A has global memory about project A.
If same node joins cluster-B — should it bring project-A memory?

Recommendation: memory scope should include `cluster_id`:
```
scope: "global"                     ← truly global
scope: "cluster:{cluster_id}"       ← cluster-scoped (default for agent nodes)
scope: "session:{session_id}"       ← session-scoped
```

v6.6 implements global + session. Cluster-scoped memory is v6.7.

**Q4: Memory size limits and pruning**
`max_entries: 1000` — what happens when exceeded?
Recommendation: LRU eviction by `updated_at`. Oldest unused facts pruned.
Confidence score can protect high-confidence entries from eviction.

**Q5: Session file compaction**
JSONL grows unboundedly with conversation history.
For very long sessions (thousands of messages), file I/O becomes slow.
Recommendation: periodic compaction (rewrite JSONL from in-memory state).
Triggered by: `len(messages) > compaction_threshold` (default: 10000).

---

## 12. Readiness after v6.6

```
Persistent agent requirements:
  ✅ Node có LLM                          → v5.9, unchanged
  ✅ Bộ nhớ trong session                 → v5.9, unchanged
  ✅ Lưu context qua restart              → v6.6 PersistentSessionStore
  ✅ Long-term memory cross-session       → v6.6 AgentMemoryStore
  ✅ Session TTL flexible (0 = infinite)  → v6.6 per-session TTL
  ✅ Clear context (keep memory)          → v6.6 POST /sessions/{id}/clear
  ✅ Reset memory (selective)             → v6.6 DELETE /memory
  ✅ Full reset                           → DELETE /sessions + DELETE /memory
  ✅ Agent can remember/recall/forget     → v6.6 seed actions
  ✅ Memory auto-injected into prompt     → v6.6 IntentHandler
  ✅ Backward compatible                  → session.backend: memory = v5.x behavior

Full v6.x persistent agent stack:
  v6.0: Event-driven, scheduler, autonomy
  v6.1: Multi-cluster topology
  v6.2: Task suspension + human-in-the-loop
  v6.3: Self-provisioning clusters
  v6.4: Multi-participant external interaction
  v6.5: Multi-channel transport (Telegram etc.)
  v6.6: Persistent memory, long-term context
```

---

*Spec: SPECS_V6.6.md | Mesh Runtime v6.6 | Repository: ai-infra-runtime-v2*
*Builds on: SPECS_V6.5.md through SPECS_V6.0.md*
*Problem statement: product owner review session, 2026-03-08*
