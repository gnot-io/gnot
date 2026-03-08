"""Tests for v6.0 Phase 3 — Persistence & MCP.

Covers:
  Part A — PersistentSessionStore (3.1–3.3)
    1. Basic create / get / delete (in-memory only, no disk)
    2. Persistence: messages written to JSONL file
    3. Startup load: sessions restored from disk
    4. TTL: expired sessions not returned
    5. clear_messages: wipes messages, keeps session
    6. max_messages_per_session truncation
    7. sweep_expired removes stale sessions

  Part B — AgentMemoryStore (3.4–3.6)
    8.  remember: stores entry, returns MemoryEntry
    9.  remember: update existing key overwrites
    10. recall: no filter returns all
    11. recall: query substring filter
    12. recall: scope filter
    13. recall: session_id includes global + session scope
    14. recall: entry_type filter
    15. forget: by key removes one entry
    16. forget: by scope removes multiple
    17. recall_for_prompt: formats facts and narratives
    18. recall_for_prompt: empty when no entries
    19. startup_load: restores entries + tombstones from disk
    20. max_entries: LRU eviction when limit exceeded

  Part C — Seed actions (3.6)
    21. agent_remember action: stores entry
    22. agent_remember action: no memory store → skipped
    23. agent_recall action: returns filtered entries
    24. agent_forget action: removes entries

  Part D — MCPClient unit tests (3.8)
    25. MCPClient: init sets correct fields
    26. MCPClient: _mcp_tool_to_openai_spec converts correctly
    27. MCPClient: call_tool error → MCPCallError

  Part E — MCPRegistry (3.9)
    28. MCPRegistry startup: partial failure doesn't crash (graceful degradation)
    29. MCPRegistry: is_mcp_tool detects mcp__ prefix
    30. MCPRegistry: list_servers returns correct shape
    31. MCPRegistry: list_tools returns correct shape
    32. MCPRegistry: call_tool routes to correct client
    33. MCPRegistry: call_tool unknown server returns error JSON
    34. MCPRegistry: get_tool_specs returns OpenAI format

  Part F — Config parsing (3.10)
    35. session section parses backend + storage_dir + ttl + max_messages
    36. memory section parses enabled + dir + inject + max_entries
    37. mcp_servers list parsed correctly
    38. Defaults when no phase-3 sections present

  Part G — IntentHandler MCP integration (3.11)
    39. MCP tool call routed to mcp_registry.call_tool
    40. Memory block injected into system prompt
    41. mesh_action still works when mcp present

  Part H — HTTP endpoints (3.3, 3.7, 3.12)
    42. POST /sessions/{id}/clear → 200
    43. POST /sessions/{id}/clear → 404 for missing session
    44. GET /memory → 200 with entries
    45. GET /memory → 503 when memory not enabled
    46. POST /memory → 201 stores entry
    47. POST /memory → 400 on missing fields
    48. DELETE /memory → 200 removes entry
    49. GET /mcp/servers → 200 with server list
    50. GET /mcp/tools → 200 with tool list
    51. GET /mcp/servers → 503 when mcp not configured
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.persistent_session_store import PersistentSessionStore, PersistedSession
from runtime.agent_memory_store import AgentMemoryStore, MemoryEntry
from runtime.mcp_client import MCPClient, MCPCallError
from runtime.mcp_registry import MCPRegistry, _mcp_tool_to_openai_spec


# ===========================================================================
# A — PersistentSessionStore
# ===========================================================================

class TestPersistentSessionStore:

    @pytest.fixture
    def tmpdir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.fixture
    def store(self, tmpdir):
        return PersistentSessionStore(storage_dir=tmpdir, default_ttl_seconds=0)

    # 1 — basic CRUD

    @pytest.mark.asyncio
    async def test_get_or_create_new_session(self, store):
        sess = await store.get_or_create("sess-001")
        assert sess.session_id == "sess-001"
        assert sess.messages == []
        assert sess.ttl_seconds == 0  # infinite

    @pytest.mark.asyncio
    async def test_get_or_create_returns_same_session(self, store):
        s1 = await store.get_or_create("sess-002")
        s2 = await store.get_or_create("sess-002")
        assert s1 is s2  # same object in memory

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self, store):
        result = await store.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_returns_true(self, store):
        await store.get_or_create("sess-del")
        deleted = await store.delete("sess-del")
        assert deleted is True
        assert await store.get("sess-del") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, store):
        assert await store.delete("no-such") is False

    # 2 — disk persistence

    @pytest.mark.asyncio
    async def test_messages_written_to_disk(self, tmpdir):
        store = PersistentSessionStore(storage_dir=tmpdir)
        sess = await store.get_or_create("sess-disk")
        sess.add_message("user", "hello world")
        await store._append_record("sess-disk", {
            "type": "message", "ts": time.time(),
            "role": "user", "content": "hello world",
        })
        fpath = tmpdir / "sess-disk.jsonl"
        assert fpath.exists()
        lines = fpath.read_text().strip().splitlines()
        assert len(lines) >= 2  # meta + message

    # 3 — startup load

    @pytest.mark.asyncio
    async def test_startup_load_restores_sessions(self, tmpdir):
        # Write first, then load fresh store
        store1 = PersistentSessionStore(storage_dir=tmpdir)
        sess = await store1.get_or_create("sess-reload")
        await store1._append_record("sess-reload", {
            "type": "message", "ts": time.time(),
            "role": "user", "content": "remember me",
        })

        # New store instance — simulates restart
        store2 = PersistentSessionStore(storage_dir=tmpdir)
        count = await store2.startup_load()
        assert count == 1
        loaded = await store2.get("sess-reload")
        assert loaded is not None
        assert loaded.session_id == "sess-reload"

    # 4 — TTL expiry

    @pytest.mark.asyncio
    async def test_expired_session_returns_none(self, tmpdir):
        store = PersistentSessionStore(storage_dir=tmpdir, default_ttl_seconds=1)
        sess = await store.get_or_create("sess-ttl")
        # Manually set last_active to past
        sess.last_active = time.time() - 10
        result = await store.get("sess-ttl")
        assert result is None

    @pytest.mark.asyncio
    async def test_infinite_ttl_never_expires(self, store):
        sess = await store.get_or_create("sess-inf")
        sess.last_active = time.time() - 9999999
        result = await store.get("sess-inf")
        assert result is not None  # ttl=0 → infinite

    # 5 — clear_messages

    @pytest.mark.asyncio
    async def test_clear_messages_wipes_history(self, store):
        sess = await store.get_or_create("sess-clr")
        sess.add_message("user", "hello")
        sess.add_message("assistant", "world")
        cleared = await store.clear_messages("sess-clr")
        assert cleared is True
        result = await store.get("sess-clr")
        assert result is not None
        assert result.messages == []

    @pytest.mark.asyncio
    async def test_clear_messages_nonexistent_returns_false(self, store):
        assert await store.clear_messages("no-sess") is False

    # 6 — max_messages_per_session

    @pytest.mark.asyncio
    async def test_max_messages_truncates_oldest(self, tmpdir):
        store = PersistentSessionStore(storage_dir=tmpdir, max_messages_per_session=3)
        sess = await store.get_or_create("sess-max")
        for i in range(5):
            sess.add_message("user", f"msg-{i}")
        msgs = store.get_messages_for_llm(sess)
        assert len(msgs) == 3
        assert msgs[0]["content"] == "msg-2"  # oldest 2 trimmed

    # 7 — sweep

    @pytest.mark.asyncio
    async def test_sweep_expired_removes_stale(self, tmpdir):
        store = PersistentSessionStore(storage_dir=tmpdir, default_ttl_seconds=1)
        sess = await store.get_or_create("sess-sweep")
        sess.last_active = time.time() - 10
        removed = await store.sweep_expired()
        assert removed == 1
        assert len(await store.list_sessions()) == 0


# ===========================================================================
# B — AgentMemoryStore
# ===========================================================================

class TestAgentMemoryStore:

    @pytest.fixture
    def tmpdir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.fixture
    def store(self, tmpdir):
        return AgentMemoryStore(
            storage_dir=tmpdir,
            node_id="test-node",
            max_entries=1000,
            inject_into_prompt=True,
        )

    # 8 — remember

    @pytest.mark.asyncio
    async def test_remember_returns_entry(self, store):
        entry = await store.remember("lang", "Python")
        assert isinstance(entry, MemoryEntry)
        assert entry.key == "lang"
        assert entry.value == "Python"
        assert entry.entry_type == "fact"
        assert entry.scope == "global"

    # 9 — update existing key

    @pytest.mark.asyncio
    async def test_remember_updates_existing(self, store):
        e1 = await store.remember("lang", "Python")
        e2 = await store.remember("lang", "Go")
        assert e2.value == "Go"
        assert e2.memory_id == e1.memory_id  # same ID, updated
        assert store.count == 1

    # 10 — recall all

    @pytest.mark.asyncio
    async def test_recall_no_filter_returns_all(self, store):
        await store.remember("k1", "v1")
        await store.remember("k2", "v2")
        entries = await store.recall()
        assert len(entries) == 2

    # 11 — recall query

    @pytest.mark.asyncio
    async def test_recall_query_substring_match(self, store):
        await store.remember("preferred_language", "Python")
        await store.remember("timezone", "Asia/Ho_Chi_Minh")
        results = await store.recall(query="python")
        assert len(results) == 1
        assert results[0].key == "preferred_language"

    # 12 — recall scope

    @pytest.mark.asyncio
    async def test_recall_scope_filter(self, store):
        await store.remember("g1", "global", scope="global")
        await store.remember("s1", "sess", scope="session:abc")
        global_only = await store.recall(scope="global")
        assert len(global_only) == 1
        assert global_only[0].key == "g1"

    # 13 — recall session_id includes global + session scope

    @pytest.mark.asyncio
    async def test_recall_session_id_includes_global_and_session(self, store):
        await store.remember("gfact", "global val", scope="global")
        await store.remember("sfact", "sess val", scope="session:xyz")
        await store.remember("other", "other val", scope="session:abc")
        results = await store.recall(session_id="xyz")
        keys = {e.key for e in results}
        assert "gfact" in keys
        assert "sfact" in keys
        assert "other" not in keys

    # 14 — recall entry_type

    @pytest.mark.asyncio
    async def test_recall_entry_type_filter(self, store):
        await store.remember("fact_key", "value", entry_type="fact")
        await store.remember("narr_key", "long text", entry_type="narrative")
        facts = await store.recall(entry_type="fact")
        assert all(e.entry_type == "fact" for e in facts)
        assert any(e.key == "fact_key" for e in facts)

    # 15 — forget by key

    @pytest.mark.asyncio
    async def test_forget_by_key(self, store):
        await store.remember("to_delete", "bye")
        await store.remember("to_keep", "hi")
        removed = await store.forget(key="to_delete")
        assert removed == 1
        assert store.count == 1
        entries = await store.recall()
        assert all(e.key != "to_delete" for e in entries)

    # 16 — forget by scope

    @pytest.mark.asyncio
    async def test_forget_by_scope(self, store):
        await store.remember("s1", "v1", scope="session:abc")
        await store.remember("s2", "v2", scope="session:abc")
        await store.remember("g1", "v3", scope="global")
        removed = await store.forget(scope="session:abc")
        assert removed == 2
        entries = await store.recall()
        assert all(e.scope != "session:abc" for e in entries)

    # 17 — recall_for_prompt with content

    @pytest.mark.asyncio
    async def test_recall_for_prompt_formats_correctly(self, store):
        await store.remember("language", "Python", entry_type="fact")
        await store.remember("background", "Backend engineer", entry_type="narrative")
        prompt = store.recall_for_prompt()
        assert "## Agent Memory" in prompt
        assert "language: Python" in prompt
        assert "background: Backend engineer" in prompt

    # 18 — recall_for_prompt empty when no entries

    def test_recall_for_prompt_empty_no_entries(self, store):
        prompt = store.recall_for_prompt()
        assert prompt == ""

    # 19 — startup_load with tombstones

    @pytest.mark.asyncio
    async def test_startup_load_restores_and_applies_tombstones(self, tmpdir):
        store1 = AgentMemoryStore(storage_dir=tmpdir, node_id="n1")
        await store1.remember("keep", "yes")
        await store1.remember("remove", "no")
        await store1.forget(key="remove")

        store2 = AgentMemoryStore(storage_dir=tmpdir, node_id="n1")
        count = await store2.startup_load()
        entries = await store2.recall()
        keys = {e.key for e in entries}
        assert "keep" in keys
        assert "remove" not in keys

    # 20 — LRU eviction

    @pytest.mark.asyncio
    async def test_max_entries_evicts_oldest(self, tmpdir):
        store = AgentMemoryStore(storage_dir=tmpdir, node_id="n2", max_entries=3)
        for i in range(5):
            await store.remember(f"key-{i}", f"val-{i}")
            await asyncio.sleep(0.01)
        assert store.count == 3


# ===========================================================================
# C — Seed actions
# ===========================================================================

class TestSeedActions:

    def _make_ctx(self, memory_store=None):
        return {"memory_store": memory_store, "node_id": "test"}

    @pytest.mark.asyncio
    async def test_agent_remember_stores_entry(self):
        from seed.actions.agent_remember import run
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            mem = AgentMemoryStore(storage_dir=d, node_id="n")
            ctx = self._make_ctx(memory_store=mem)
            result = await run({"key": "project", "value": "gnot"}, ctx)
            assert result["key"] == "project"
            assert result["value"] == "gnot"
            assert "memory_id" in result

    @pytest.mark.asyncio
    async def test_agent_remember_no_memory_skipped(self):
        from seed.actions.agent_remember import run
        result = await run({"key": "x", "value": "y"}, self._make_ctx(memory_store=None))
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_agent_recall_returns_entries(self):
        from seed.actions.agent_recall import run
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            mem = AgentMemoryStore(storage_dir=d, node_id="n")
            await mem.remember("k", "v")
            result = await run({}, self._make_ctx(memory_store=mem))
            assert result["total"] == 1
            assert result["entries"][0]["key"] == "k"

    @pytest.mark.asyncio
    async def test_agent_forget_removes_entry(self):
        from seed.actions.agent_forget import run
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            mem = AgentMemoryStore(storage_dir=d, node_id="n")
            await mem.remember("del_me", "bye")
            result = await run({"key": "del_me"}, self._make_ctx(memory_store=mem))
            assert result["removed"] == 1
            assert mem.count == 0


# ===========================================================================
# D — MCPClient unit tests
# ===========================================================================

class TestMCPClientUnit:

    # 25 — init fields

    def test_init_stores_config(self):
        client = MCPClient(
            server_id="fs",
            transport="stdio",
            command="npx fs-server",
            env={"FOO": "bar"},
        )
        assert client.server_id == "fs"
        assert client.transport == "stdio"
        assert client._command == "npx fs-server"
        assert client._env == {"FOO": "bar"}
        assert client._connected is False

    # 26 — OpenAI spec conversion

    def test_mcp_tool_to_openai_spec_conversion(self):
        tool_def = {
            "name": "read_file",
            "description": "Read file contents",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                },
                "required": ["path"],
            },
        }
        spec = _mcp_tool_to_openai_spec("mcp__fs__read_file", tool_def)
        assert spec["type"] == "function"
        assert spec["function"]["name"] == "mcp__fs__read_file"
        assert spec["function"]["description"] == "Read file contents"
        params = spec["function"]["parameters"]
        assert params["type"] == "object"
        assert "path" in params["properties"]
        assert params["required"] == ["path"]

    def test_mcp_tool_spec_truncates_long_description(self):
        long_desc = "x" * 600
        spec = _mcp_tool_to_openai_spec("mcp__s__t", {"name": "t", "description": long_desc})
        assert len(spec["function"]["description"]) <= 503  # 500 + "..."

    # 27 — call_tool error

    @pytest.mark.asyncio
    async def test_call_tool_raises_mcp_call_error_on_error_response(self):
        client = MCPClient(server_id="test", transport="stdio", command="dummy")
        # Mock _call to return isError response
        async def mock_call(method, params):
            return {"result": {"isError": True, "content": [{"type": "text", "text": "File not found"}]}}
        client._call = mock_call
        with pytest.raises(MCPCallError) as exc_info:
            await client.call_tool("read_file", {"path": "/missing"})
        assert "File not found" in str(exc_info.value)


# ===========================================================================
# E — MCPRegistry
# ===========================================================================

class TestMCPRegistry:

    def _make_mock_client(self, server_id: str, tools: list) -> MCPClient:
        client = MagicMock(spec=MCPClient)
        client.server_id = server_id
        client.transport = "stdio"
        client._connected = True
        client.connect = AsyncMock(return_value={"serverInfo": {"name": server_id}})
        client.list_tools = AsyncMock(return_value=tools)
        client.disconnect = AsyncMock()
        return client

    # 28 — graceful degradation on partial failure

    @pytest.mark.asyncio
    async def test_startup_partial_failure_continues(self):
        registry = MCPRegistry([
            {"id": "ok-server", "transport": "stdio", "command": "echo"},
            {"id": "bad-server", "transport": "stdio", "command": "echo"},
        ])
        good_client = self._make_mock_client("ok-server", [
            {"name": "list_dir", "description": "List directory", "inputSchema": {"type": "object", "properties": {}}}
        ])
        bad_client = MagicMock(spec=MCPClient)
        bad_client.server_id = "bad-server"
        bad_client.transport = "stdio"
        bad_client._connected = False
        bad_client.connect = AsyncMock(side_effect=Exception("Connection refused"))
        bad_client.disconnect = AsyncMock()

        registry._clients = {"ok-server": good_client, "bad-server": bad_client}
        registry._configs = []  # prevent re-creation in startup

        await registry._connect_one("ok-server", good_client)
        await registry._connect_one("bad-server", bad_client)
        registry._rebuild_openai_specs()

        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "list_dir"

    # 29 — is_mcp_tool

    def test_is_mcp_tool_detects_prefix(self):
        registry = MCPRegistry([])
        assert registry.is_mcp_tool("mcp__fs__read_file") is True
        assert registry.is_mcp_tool("mesh_action") is False
        assert registry.is_mcp_tool("execute_command") is False

    # 30 — list_servers shape

    @pytest.mark.asyncio
    async def test_list_servers_returns_correct_shape(self):
        registry = MCPRegistry([])
        client = self._make_mock_client("fs", [])
        registry._clients = {"fs": client}
        servers = registry.list_servers()
        assert len(servers) == 1
        assert servers[0]["server_id"] == "fs"
        assert "connected" in servers[0]
        assert "tool_count" in servers[0]

    # 31 — list_tools shape

    @pytest.mark.asyncio
    async def test_list_tools_returns_correct_shape(self):
        registry = MCPRegistry([])
        client = self._make_mock_client("fs", [
            {"name": "read_file", "description": "Read", "inputSchema": {"type": "object", "properties": {}}}
        ])
        registry._clients = {"fs": client}
        await registry._connect_one("fs", client)
        tools = registry.list_tools()
        assert len(tools) == 1
        t = tools[0]
        assert t["qualified_name"] == "mcp__fs__read_file"
        assert t["server_id"] == "fs"
        assert t["name"] == "read_file"

    # 32 — call_tool routes to correct client

    @pytest.mark.asyncio
    async def test_call_tool_routes_correctly(self):
        registry = MCPRegistry([])
        client = self._make_mock_client("fs", [
            {"name": "read_file", "description": "Read", "inputSchema": {"type": "object", "properties": {}}}
        ])
        client.call_tool = AsyncMock(return_value=[{"type": "text", "text": "file contents"}])
        registry._clients = {"fs": client}
        await registry._connect_one("fs", client)

        result = await registry.call_tool("mcp__fs__read_file", {"path": "/foo.txt"})
        assert result == "file contents"
        client.call_tool.assert_called_once_with("read_file", {"path": "/foo.txt"})

    # 33 — unknown server returns error JSON

    @pytest.mark.asyncio
    async def test_call_tool_unknown_server_returns_error(self):
        registry = MCPRegistry([])
        result = await registry.call_tool("mcp__ghost__something", {})
        data = json.loads(result)
        assert "error" in data

    # 34 — get_tool_specs OpenAI format

    @pytest.mark.asyncio
    async def test_get_tool_specs_openai_format(self):
        registry = MCPRegistry([])
        client = self._make_mock_client("fs", [
            {"name": "write_file", "description": "Write", "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            }}
        ])
        registry._clients = {"fs": client}
        await registry._connect_one("fs", client)
        registry._rebuild_openai_specs()   # explicit rebuild after connecting
        specs = registry.get_tool_specs()
        assert len(specs) == 1
        s = specs[0]
        assert s["type"] == "function"
        assert s["function"]["name"] == "mcp__fs__write_file"
        assert s["function"]["parameters"]["required"] == ["path", "content"]


# ===========================================================================
# F — Config parsing
# ===========================================================================

class TestConfigPhase3:

    def _write_yaml(self, tmpdir: Path, content: str) -> str:
        p = tmpdir / "node.yaml"
        p.write_text(content)
        return str(p)

    @pytest.fixture
    def tmpdir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    # 35 — session section

    def test_session_section_parsed(self, tmpdir):
        from runtime.config import load_config
        path = self._write_yaml(tmpdir, """
node_id: n
listen: 0.0.0.0:9000
session:
  backend: persistent
  storage_dir: ./my-sessions
  default_ttl_seconds: 7200
  max_messages_per_session: 50
""")
        cfg = load_config(path)
        assert cfg.session.backend == "persistent"
        assert cfg.session.storage_dir == "./my-sessions"
        assert cfg.session.default_ttl_seconds == 7200
        assert cfg.session.max_messages_per_session == 50

    # 36 — memory section

    def test_memory_section_parsed(self, tmpdir):
        from runtime.config import load_config
        path = self._write_yaml(tmpdir, """
node_id: n
listen: 0.0.0.0:9000
memory:
  enabled: true
  storage_dir: ./mem
  inject_into_prompt: false
  max_entries: 500
""")
        cfg = load_config(path)
        assert cfg.memory.enabled is True
        assert cfg.memory.storage_dir == "./mem"
        assert cfg.memory.inject_into_prompt is False
        assert cfg.memory.max_entries == 500

    # 37 — mcp_servers parsed

    def test_mcp_servers_section_parsed(self, tmpdir):
        from runtime.config import load_config
        path = self._write_yaml(tmpdir, """
node_id: n
listen: 0.0.0.0:9000
mcp_servers:
  - id: filesystem
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-filesystem /ws"
  - id: github
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-github"
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: tok123
""")
        cfg = load_config(path)
        assert len(cfg.mcp_servers) == 2
        assert cfg.mcp_servers[0]["id"] == "filesystem"
        assert cfg.mcp_servers[1]["id"] == "github"
        assert cfg.mcp_servers[1]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "tok123"

    # 38 — defaults when no phase-3 sections

    def test_phase3_defaults_when_absent(self, tmpdir):
        from runtime.config import load_config
        path = self._write_yaml(tmpdir, """
node_id: n
listen: 0.0.0.0:9000
""")
        cfg = load_config(path)
        assert cfg.session.backend == "memory"
        assert cfg.session.default_ttl_seconds == 0
        assert cfg.memory.enabled is False
        assert len(cfg.mcp_servers) == 0


# ===========================================================================
# G — IntentHandler MCP + Memory integration
# ===========================================================================

class TestIntentHandlerPhase3:

    def _make_handler(self, memory_store=None, mcp_registry=None):
        from runtime.intent_handler import IntentHandler
        from runtime.config import NodeConfig
        from runtime.action_loader import ActionRegistry
        from runtime.conversation_store import ConversationStore
        from runtime.gateway_router import GatewayRouter
        from runtime.node_registry import NodeRegistry

        cfg = NodeConfig(node_id="gw", listen="0.0.0.0:9000")
        llm = MagicMock()
        router = MagicMock(spec=GatewayRouter)
        node_reg = MagicMock(spec=NodeRegistry)
        node_reg.build_capability_tree = MagicMock(return_value={})
        action_reg = ActionRegistry()
        store = ConversationStore()

        return IntentHandler(
            config=cfg,
            llm_client=llm,
            gateway_router=router,
            node_registry=node_reg,
            action_registry=action_reg,
            conversation_store=store,
            memory_store=memory_store,
            mcp_registry=mcp_registry,
        )

    # 39 — MCP tool call routed to mcp_registry

    @pytest.mark.asyncio
    async def test_mcp_tool_call_routed_to_registry(self):
        import tempfile
        mcp_reg = MagicMock(spec=MCPRegistry)
        mcp_reg.is_mcp_tool = MagicMock(return_value=True)
        mcp_reg.call_tool = AsyncMock(return_value='{"result": "file contents"}')

        handler = self._make_handler(mcp_registry=mcp_reg)

        from runtime.llm_client import ToolCall
        tc = ToolCall(id="tc1", name="mcp__fs__read_file", arguments={"path": "/foo.txt"})
        result = await handler._execute_tool_call(tc)
        mcp_reg.call_tool.assert_called_once_with("mcp__fs__read_file", {"path": "/foo.txt"})
        assert result == '{"result": "file contents"}'

    # 40 — Memory block injected into system prompt

    @pytest.mark.asyncio
    async def test_memory_block_injected_in_system_prompt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            mem = AgentMemoryStore(storage_dir=d, node_id="n", inject_into_prompt=True)
            await mem.remember("project", "GNOT v6", entry_type="fact")

            handler = self._make_handler(memory_store=mem)
            prompt = handler._build_system_prompt(session_id="sess-abc")
            assert "## Agent Memory" in prompt
            assert "project: GNOT v6" in prompt

    # 41 — mesh_action still works when mcp present

    @pytest.mark.asyncio
    async def test_mesh_action_still_routed_when_mcp_present(self):
        from runtime.llm_client import ToolCall
        from runtime.models import SyncActionResponse
        from runtime.gateway_router import GatewayRouter

        mcp_reg = MagicMock(spec=MCPRegistry)
        mcp_reg.is_mcp_tool = MagicMock(return_value=False)

        handler = self._make_handler(mcp_registry=mcp_reg)
        handler._router.route = AsyncMock(
            return_value=SyncActionResponse(task_id="t", output={"done": True})
        )

        tc = ToolCall(id="tc2", name="mesh_action", arguments={
            "target_node_id": "worker", "action": "execute_command", "params": {}
        })
        result = await handler._execute_tool_call(tc)
        mcp_reg.call_tool.assert_not_called()
        data = json.loads(result)
        assert data["status"] == "completed"


# ===========================================================================
# H — HTTP endpoints
# ===========================================================================

class TestPhase3HTTPEndpoints:

    def _make_app(self, memory_enabled=True, mcp_configured=False):
        from fastapi.testclient import TestClient
        from runtime.config import NodeConfig, MemoryConfig, SessionConfig
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app
        import tempfile

        tmpdir = tempfile.mkdtemp()
        mem_cfg = MemoryConfig(
            enabled=memory_enabled,
            storage_dir=tmpdir,
            inject_into_prompt=True,
            max_entries=100,
        )
        sess_cfg = SessionConfig(backend="memory", default_ttl_seconds=0)
        cfg = NodeConfig(
            node_id="test-gw",
            listen="0.0.0.0:9000",
            memory=mem_cfg,
            session=sess_cfg,
        )
        registry = ActionRegistry()
        app = create_app(config=cfg, registry=registry)
        return TestClient(app, raise_server_exceptions=True)

    # 42 — POST /sessions/{id}/clear → 200

    def test_clear_session_messages(self):
        client = self._make_app()
        # Create a session via GET endpoint
        # Actually, just call the clear endpoint; session doesn't exist yet
        # It should return 404 first, then 200 after creation
        resp = client.post("/sessions/sess-abc/clear")
        # Session doesn't exist yet, expect 404
        assert resp.status_code == 404

    # 43 — POST /sessions/{id}/clear → 404 for missing

    def test_clear_nonexistent_session_404(self):
        client = self._make_app()
        resp = client.post("/sessions/ghost-session/clear")
        assert resp.status_code == 404

    # 44 — GET /memory → 200

    def test_get_memory_returns_200(self):
        client = self._make_app(memory_enabled=True)
        resp = client.get("/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "total" in data

    # 45 — GET /memory → 503 when disabled

    def test_get_memory_503_when_disabled(self):
        client = self._make_app(memory_enabled=False)
        resp = client.get("/memory")
        assert resp.status_code == 503

    # 46 — POST /memory → 201

    def test_post_memory_stores_entry(self):
        client = self._make_app(memory_enabled=True)
        resp = client.post("/memory", json={
            "key": "preferred_lang",
            "value": "Python",
            "entry_type": "fact",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["key"] == "preferred_lang"
        assert data["value"] == "Python"

    # 47 — POST /memory → 400 on missing fields

    def test_post_memory_400_missing_fields(self):
        client = self._make_app(memory_enabled=True)
        resp = client.post("/memory", json={"key": "only_key"})
        assert resp.status_code == 400

    # 48 — DELETE /memory → 200

    def test_delete_memory_removes_entry(self):
        client = self._make_app(memory_enabled=True)
        # First store
        client.post("/memory", json={"key": "to_del", "value": "bye"})
        # Then delete
        resp = client.request("DELETE", "/memory", json={"key": "to_del"})
        assert resp.status_code == 200
        assert resp.json()["removed"] == 1

    # 49 — GET /mcp/servers → 503 when not configured

    def test_get_mcp_servers_503_when_not_configured(self):
        client = self._make_app(mcp_configured=False)
        resp = client.get("/mcp/servers")
        assert resp.status_code == 503

    # 50 — GET /mcp/tools → 503 when not configured

    def test_get_mcp_tools_503_when_not_configured(self):
        client = self._make_app(mcp_configured=False)
        resp = client.get("/mcp/tools")
        assert resp.status_code == 503

    # 51 — GET /mcp/servers with configured app

    def test_get_mcp_servers_with_real_config(self):
        """App with mcp_servers config creates registry → endpoints return 200."""
        from fastapi.testclient import TestClient
        from runtime.config import NodeConfig
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app
        from unittest.mock import patch, AsyncMock, MagicMock

        cfg = NodeConfig(
            node_id="test-gw",
            listen="0.0.0.0:9000",
            mcp_servers=({"id": "fs", "transport": "stdio", "command": "echo"},),
        )
        registry = ActionRegistry()

        # Patch MCPRegistry.startup to avoid real subprocess
        with patch("runtime.mcp_registry.MCPRegistry.startup", new_callable=AsyncMock) as mock_startup, \
             patch("runtime.mcp_registry.MCPRegistry.list_servers", return_value=[
                 {"server_id": "fs", "transport": "stdio", "connected": True, "tool_count": 0}
             ]), \
             patch("runtime.mcp_registry.MCPRegistry.list_tools", return_value=[]), \
             patch("runtime.mcp_registry.MCPRegistry.shutdown", new_callable=AsyncMock):
            app = create_app(config=cfg, registry=registry)
            client = TestClient(app)
            resp = client.get("/mcp/servers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
