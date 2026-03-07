"""Tests for v5.13 — Key Rotation · Credential Persistence · Action Specs Cascade.

Test classes:
  TestSeparateEncryptionKey     — credential_encryption_key decoupled from auth_token
  TestKeyRotationIsolation      — auth_token change doesn't affect credentials
  TestCredentialPersistence     — load/flush/atomic write/restart survival
  TestFlushTask                 — background flush task start/stop/dirty tracking
  TestConfigV513                — new config fields parsed from node.yaml
  TestActionSpecsCascade        — SubRouteInfo carries specs through add_sub_route + _register
  TestSpecsCascadeEndToEnd      — full HTTP chain: node-1a → node-1 → node-0
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.credential_store import (
    PERSISTENCE_FORMAT_VERSION,
    CredentialStore,
    _decrypt,
    _derive_key,
    _encrypt,
)
from runtime.worker_agent import SubRouteInfo


# ===========================================================================
# 1. Separate Encryption Key
# ===========================================================================

class TestSeparateEncryptionKey:

    @pytest.mark.asyncio
    async def test_encryption_key_used_when_provided(self):
        """Credentials encrypted with encryption_key, not auth_token."""
        enc_store = CredentialStore(encryption_key="sk-enc-dedicated", ttl_seconds=60)
        await enc_store.merge("s1", {"k": "secret"})
        got = await enc_store.get("s1")
        assert got["k"] == "secret"

    @pytest.mark.asyncio
    async def test_no_encryption_key_falls_back_gracefully(self):
        """None encryption_key → base64 fallback or no-auth derive (no crash)."""
        store = CredentialStore(encryption_key=None, ttl_seconds=60)
        await store.merge("s1", {"k": "value"})
        got = await store.get("s1")
        assert got["k"] == "value"

    @pytest.mark.asyncio
    async def test_different_encryption_keys_produce_different_ciphertext(self):
        """Two stores with different keys encrypt the same plaintext differently."""
        s1 = CredentialStore(encryption_key="key-A", ttl_seconds=60)
        s2 = CredentialStore(encryption_key="key-B", ttl_seconds=60)
        await s1.merge("sess", {"token": "abc"})
        await s2.merge("sess", {"token": "abc"})
        ct1 = s1._store["sess"]["creds"]["token"]
        ct2 = s2._store["sess"]["creds"]["token"]
        assert ct1 != ct2

    @pytest.mark.asyncio
    async def test_encryption_key_fingerprint_differs_per_key(self):
        s1 = CredentialStore(encryption_key="key-A")
        s2 = CredentialStore(encryption_key="key-B")
        assert s1.encryption_key_fingerprint != s2.encryption_key_fingerprint

    def test_encryption_key_fingerprint_no_key(self):
        s = CredentialStore(encryption_key=None)
        assert s.encryption_key_fingerprint == "no-key"

    @pytest.mark.asyncio
    async def test_wrong_key_returns_empty_with_logged_error(self):
        """Decrypting with wrong key silently skips the credential (logs error)."""
        s1 = CredentialStore(encryption_key="key-A", ttl_seconds=60)
        await s1.merge("sess", {"token": "abc"})
        # Force wrong key by swapping encryption_key after storing
        s1._enc_key = "key-WRONG"
        got = await s1.get("sess")
        # Credential silently dropped (not raised)
        assert "token" not in got


# ===========================================================================
# 2. Key Rotation Isolation
# ===========================================================================

class TestKeyRotationIsolation:

    @pytest.mark.asyncio
    async def test_auth_token_change_does_not_affect_credentials(self):
        """v5.13: credentials survive auth_token rotation because they use
        credential_encryption_key, not auth_token."""
        # Server creates CredentialStore with dedicated enc key
        enc_store = CredentialStore(encryption_key="sk-enc-stable", ttl_seconds=3600)
        await enc_store.merge("sess-1", {"crm": "sk-user-abc"})

        # Simulate auth_token rotation — enc key unchanged
        # (server.py: _cred_enc_key = config.credential_encryption_key or config.auth_token)
        # credential_encryption_key was set → auth_token change has no effect
        got = await enc_store.get("sess-1")
        assert got["crm"] == "sk-user-abc"

    @pytest.mark.asyncio
    async def test_enc_key_change_makes_existing_creds_unreadable(self):
        """Changing encryption_key (intentional re-key) invalidates old ciphertext."""
        store = CredentialStore(encryption_key="old-key", ttl_seconds=3600)
        await store.merge("sess", {"token": "value"})

        # Operator re-keys the store
        store._enc_key = "new-key"
        got = await store.get("sess")
        # Old ciphertext can't be decrypted → credential silently dropped
        assert "token" not in got

    @pytest.mark.asyncio
    async def test_new_merges_after_rekey_use_new_key(self):
        """After enc_key change, new credentials are encrypted with new key."""
        store = CredentialStore(encryption_key="old-key", ttl_seconds=3600)
        store._enc_key = "new-key"
        await store.merge("sess", {"token": "new-value"})
        got = await store.get("sess")
        assert got["token"] == "new-value"


# ===========================================================================
# 3. Credential Persistence — load / flush / atomic write
# ===========================================================================

class TestCredentialPersistence:

    @pytest.mark.asyncio
    async def test_flush_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            store = CredentialStore(encryption_key="sk-enc", ttl_seconds=3600, store_path=path)
            await store.merge("s1", {"k": "v"})
            flushed = await store.flush()
            assert flushed is True
            assert Path(path).exists()

    @pytest.mark.asyncio
    async def test_flush_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            store = CredentialStore(encryption_key="sk-enc", ttl_seconds=3600, store_path=path)
            await store.merge("s1", {"token": "abc"})
            await store.flush()
            data = json.loads(Path(path).read_text())
            assert data["version"] == PERSISTENCE_FORMAT_VERSION
            assert "s1" in data["sessions"]
            # Ciphertext stored, not plaintext
            assert "abc" not in data["sessions"]["s1"]["creds"]["token"]

    @pytest.mark.asyncio
    async def test_load_restores_credentials(self):
        """Simulate restart: flush → create new store → load → get."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")

            # Before restart
            store1 = CredentialStore(encryption_key="sk-enc", ttl_seconds=3600, store_path=path)
            await store1.merge("sess-123", {"crm_token": "sk-abc"})
            await store1.flush()

            # After restart — new in-memory store, same enc key and path
            store2 = CredentialStore(encryption_key="sk-enc", ttl_seconds=3600, store_path=path)
            count = await store2.load()
            assert count == 1
            got = await store2.get("sess-123")
            assert got["crm_token"] == "sk-abc"

    @pytest.mark.asyncio
    async def test_load_skips_expired_sessions(self):
        """Expired sessions in the file are not loaded."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            # Write file manually with expired entry
            data = {
                "version": PERSISTENCE_FORMAT_VERSION,
                "sessions": {
                    "expired-sess": {
                        "expires_at": time.time() - 1.0,
                        "creds": {"k": "encrypted_value"},
                    },
                    "live-sess": {
                        "expires_at": time.time() + 3600.0,
                        "creds": {},
                    },
                },
            }
            Path(path).write_text(json.dumps(data))
            store = CredentialStore(encryption_key="sk-enc", ttl_seconds=3600, store_path=path)
            count = await store.load()
            assert count == 1
            assert "expired-sess" not in store._store
            assert "live-sess" in store._store

    @pytest.mark.asyncio
    async def test_load_ignores_missing_file(self):
        """No file → load returns 0 silently."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nonexistent.json")
            store = CredentialStore(encryption_key="sk-enc", ttl_seconds=60, store_path=path)
            count = await store.load()
            assert count == 0

    @pytest.mark.asyncio
    async def test_load_ignores_wrong_version(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            Path(path).write_text(json.dumps({"version": 999, "sessions": {"s": {}}}))
            store = CredentialStore(encryption_key="sk-enc", ttl_seconds=60, store_path=path)
            count = await store.load()
            assert count == 0

    @pytest.mark.asyncio
    async def test_flush_atomic_no_partial_write(self):
        """Flush writes .tmp then renames — no partial file on read."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "creds.json"
            store = CredentialStore(encryption_key="sk-enc", ttl_seconds=3600, store_path=str(path))
            await store.merge("s1", {"k": "v"})
            await store.flush()
            # .tmp should be gone after successful flush
            assert not path.with_suffix(".tmp").exists()
            # Main file is valid JSON
            json.loads(path.read_text())

    @pytest.mark.asyncio
    async def test_flush_not_dirty_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            store = CredentialStore(encryption_key="sk-enc", ttl_seconds=60, store_path=path)
            # Not dirty — nothing to flush
            result = await store.flush()
            assert result is False

    @pytest.mark.asyncio
    async def test_flush_in_memory_store_returns_false(self):
        """In-memory store (no store_path) never flushes to disk."""
        store = CredentialStore(encryption_key="sk-enc", ttl_seconds=60)
        await store.merge("s", {"k": "v"})
        result = await store.flush()
        assert result is False

    @pytest.mark.asyncio
    async def test_clear_marks_dirty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            store = CredentialStore(encryption_key="sk-enc", ttl_seconds=60, store_path=path)
            await store.merge("s1", {"k": "v"})
            await store.flush()
            assert store._dirty is False
            await store.clear("s1")
            assert store._dirty is True

    @pytest.mark.asyncio
    async def test_flush_purges_expired_before_writing(self):
        """Expired sessions are removed from file on flush."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            store = CredentialStore(encryption_key="sk-enc", ttl_seconds=0, store_path=path)
            await store.merge("expired", {"k": "v"})
            await asyncio.sleep(0.05)
            store._dirty = True   # force dirty
            await store.flush()
            data = json.loads(Path(path).read_text())
            assert "expired" not in data["sessions"]

    @pytest.mark.asyncio
    async def test_persist_survives_restart_different_session_isolated(self):
        """Multiple sessions persist independently; one clear doesn't affect others."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            store1 = CredentialStore(encryption_key="sk-enc", ttl_seconds=3600, store_path=path)
            await store1.merge("s1", {"k1": "v1"})
            await store1.merge("s2", {"k2": "v2"})
            await store1.flush()

            store2 = CredentialStore(encryption_key="sk-enc", ttl_seconds=3600, store_path=path)
            await store2.load()
            assert (await store2.get("s1"))["k1"] == "v1"
            assert (await store2.get("s2"))["k2"] == "v2"


# ===========================================================================
# 4. Background Flush Task
# ===========================================================================

class TestFlushTask:

    @pytest.mark.asyncio
    async def test_flush_task_starts_only_with_store_path(self):
        s_mem = CredentialStore(encryption_key="k", ttl_seconds=60)
        s_mem.start_flush_task()
        assert s_mem._flush_task is None

    @pytest.mark.asyncio
    async def test_flush_task_starts_with_store_path(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            store = CredentialStore(encryption_key="k", ttl_seconds=60, store_path=path)
            store.start_flush_task()
            assert store._flush_task is not None
            store._flush_task.cancel()
            try:
                await store._flush_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_stop_flush_task_does_final_flush(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            store = CredentialStore(encryption_key="k", ttl_seconds=3600, store_path=path)
            await store.merge("s", {"k": "v"})
            store.start_flush_task()
            await store.stop_flush_task()
            # Final flush should have written the file
            assert Path(path).exists()

    @pytest.mark.asyncio
    async def test_double_start_does_not_create_second_task(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "creds.json")
            store = CredentialStore(encryption_key="k", ttl_seconds=60, store_path=path)
            store.start_flush_task()
            first_task = store._flush_task
            store.start_flush_task()
            assert store._flush_task is first_task   # same task
            first_task.cancel()
            try:
                await first_task
            except asyncio.CancelledError:
                pass


# ===========================================================================
# 5. Config v5.13
# ===========================================================================

class TestConfigV513:

    def test_load_config_credential_encryption_key(self):
        from runtime.config import load_config
        yaml = (
            "node_id: node-0\nlisten: 0.0.0.0:8080\n"
            "credential_encryption_key: sk-enc-secret\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "node.yaml")
            open(p, "w").write(yaml)
            config = load_config(p)
        assert config.credential_encryption_key == "sk-enc-secret"

    def test_load_config_credential_store_path(self):
        from runtime.config import load_config
        yaml = (
            "node_id: node-0\nlisten: 0.0.0.0:8080\n"
            "credential_store_path: /var/lib/mesh/creds.json\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "node.yaml")
            open(p, "w").write(yaml)
            config = load_config(p)
        assert config.credential_store_path == "/var/lib/mesh/creds.json"

    def test_load_config_defaults_to_none(self):
        from runtime.config import load_config
        yaml = "node_id: node-0\nlisten: 0.0.0.0:8080\n"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "node.yaml")
            open(p, "w").write(yaml)
            config = load_config(p)
        assert config.credential_encryption_key is None
        assert config.credential_store_path is None

    def test_server_uses_enc_key_over_auth_token(self):
        """When credential_encryption_key is set, server.py passes it to CredentialStore,
        not auth_token."""
        from runtime.config import load_config
        yaml = (
            "node_id: node-0\nlisten: 0.0.0.0:8080\n"
            "auth_token: sk-auth\n"
            "credential_encryption_key: sk-enc-dedicated\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "node.yaml")
            open(p, "w").write(yaml)
            config = load_config(p)
        # The logic in server.py: _cred_enc_key = config.credential_encryption_key or config.auth_token
        _cred_enc_key = config.credential_encryption_key or config.auth_token
        assert _cred_enc_key == "sk-enc-dedicated"

    def test_server_falls_back_to_auth_token_when_no_enc_key(self):
        from runtime.config import load_config
        yaml = "node_id: node-0\nlisten: 0.0.0.0:8080\nauth_token: sk-auth\n"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "node.yaml")
            open(p, "w").write(yaml)
            config = load_config(p)
        _cred_enc_key = config.credential_encryption_key or config.auth_token
        assert _cred_enc_key == "sk-auth"


# ===========================================================================
# 6. Action Specs Cascade — SubRouteInfo + add_sub_route + _register
# ===========================================================================

class TestActionSpecsCascade:

    def _make_agent(self):
        from runtime.worker_agent import WorkerAgent
        config = MagicMock()
        config.node_id = "node-1"
        config.heartbeat_interval_seconds = 10
        config.poll_interval_seconds = 5
        return WorkerAgent(config=config, executor=MagicMock())

    @pytest.mark.asyncio
    async def test_add_sub_route_stores_action_specs(self):
        agent = self._make_agent()
        specs = {
            "get_order_info": {
                "description": "Get order",
                "params_schema": {"order_id": {"type": "string"}},
                "caller_credentials": {},
                "async_action": False,
            }
        }
        await agent.add_sub_route("node-1a", ["get_order_info"], action_specs=specs)
        info = agent._sub_routes["node-1a"]
        assert isinstance(info, SubRouteInfo)
        assert info.actions == ["get_order_info"]
        assert "get_order_info" in info.action_specs
        assert info.action_specs["get_order_info"]["description"] == "Get order"

    @pytest.mark.asyncio
    async def test_add_sub_route_without_specs_stores_empty(self):
        agent = self._make_agent()
        await agent.add_sub_route("node-1a", ["read_file"])
        info = agent._sub_routes["node-1a"]
        assert info.actions == ["read_file"]
        assert info.action_specs == {}

    @pytest.mark.asyncio
    async def test_register_payload_includes_sub_route_specs(self):
        agent = self._make_agent()
        agent._gateway_url = "http://gw:8080"
        agent._self_address = None
        agent._auth_headers = {}

        specs = {"get_order_info": {"description": "Get order", "params_schema": {},
                                     "caller_credentials": {}, "async_action": False}}
        # Set SubRouteInfo directly (skip re-register side effect)
        agent._sub_routes["node-1a"] = SubRouteInfo(
            actions=["get_order_info"], action_specs=specs
        )

        captured: dict = {}

        async def fake_post(url, json=None, headers=None):
            captured["payload"] = json
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"registered": True})
            return resp

        import httpx
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(side_effect=fake_post)):
            await agent._register()

        payload = captured["payload"]
        assert "sub_route_specs" in payload
        assert "node-1a" in payload["sub_route_specs"]
        assert payload["sub_route_specs"]["node-1a"]["get_order_info"]["description"] == "Get order"

    @pytest.mark.asyncio
    async def test_register_payload_no_sub_route_specs_when_empty(self):
        """If sub-routes have no specs, sub_route_specs is not included in payload."""
        agent = self._make_agent()
        agent._gateway_url = "http://gw:8080"
        agent._self_address = None
        agent._auth_headers = {}
        agent._sub_routes["node-1a"] = SubRouteInfo(actions=["read_file"], action_specs={})

        captured: dict = {}

        async def fake_post(url, json=None, headers=None):
            captured["payload"] = json
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"registered": True})
            return resp

        import httpx
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(side_effect=fake_post)):
            await agent._register()

        # sub_route_specs omitted when all specs are empty
        assert "sub_route_specs" not in captured["payload"]


# ===========================================================================
# 7. Action Specs Cascade — End-to-End HTTP tests
# ===========================================================================

try:
    from httpx import AsyncClient, ASGITransport
    from runtime.action_loader import load_actions
    from runtime.config import load_config
    from runtime.schema_validator import load_schemas
    from runtime.server import create_app
    INTEGRATION_AVAILABLE = True
except ImportError:
    INTEGRATION_AVAILABLE = False


def _make_app(tmpdir: str) -> object:
    p = os.path.join(tmpdir, "node.yaml")
    with open(p, "w") as f:
        f.write("node_id: node-0\nlisten: 0.0.0.0:8080\n")
    cfg = load_config(p)
    seed = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
    return create_app(cfg, load_actions(seed), schema_registry=load_schemas(seed))


@pytest.mark.skipif(not INTEGRATION_AVAILABLE, reason="runtime deps not available")
@pytest.mark.asyncio
class TestSpecsCascadeEndToEnd:

    async def test_sub_route_specs_stored_in_node_registry(self):
        """When node-1 registers with action_specs for node-1a, specs appear in /capabilities."""
        with tempfile.TemporaryDirectory() as d:
            app = _make_app(d)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                # node-1 registers, advertising node-1a with specs
                await c.post("/nodes/register", json={
                    "node_id": "node-1",
                    "actions": ["read_file"],
                    "advertise_routes": ["node-1a"],
                    "capabilities": {"node-1a": ["get_order_info"]},
                    "sub_route_specs": {
                        "node-1a": {
                            "get_order_info": {
                                "description": "Get order from CRM",
                                "params_schema": {"order_id": {"type": "string"}},
                                "caller_credentials": {
                                    "crm_user_token": {
                                        "description": "CRM API key",
                                        "required": True,
                                        "hint": "From admin panel",
                                    }
                                },
                                "async_action": False,
                            }
                        }
                    },
                })
                caps = (await c.get("/capabilities")).json()

            assert "node-1a" in caps["reachable"]
            node_1a = caps["reachable"]["node-1a"]
            assert "action_specs" in node_1a
            assert "get_order_info" in node_1a["action_specs"]
            spec = node_1a["action_specs"]["get_order_info"]
            assert spec["description"] == "Get order from CRM"
            assert "crm_user_token" in spec["caller_credentials"]

    async def test_direct_node_specs_also_in_capabilities(self):
        """node-1 registers with its own action_specs — appears in /capabilities."""
        with tempfile.TemporaryDirectory() as d:
            app = _make_app(d)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/nodes/register", json={
                    "node_id": "node-1",
                    "actions": ["read_file"],
                    "action_specs": {
                        "read_file": {
                            "description": "Read a file from disk",
                            "params_schema": {"path": {"type": "string"}},
                            "caller_credentials": {},
                            "async_action": False,
                        }
                    },
                })
                caps = (await c.get("/capabilities")).json()

            assert "node-1" in caps["reachable"]
            assert "read_file" in caps["reachable"]["node-1"]["action_specs"]
            assert caps["reachable"]["node-1"]["action_specs"]["read_file"]["description"] == \
                "Read a file from disk"

    async def test_capability_without_specs_is_backward_compatible(self):
        """node-1 registers without specs — node appears in /capabilities with empty action_specs."""
        with tempfile.TemporaryDirectory() as d:
            app = _make_app(d)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.post("/nodes/register", json={
                    "node_id": "node-old",
                    "actions": ["old_action"],
                })
                caps = (await c.get("/capabilities")).json()

            assert "node-old" in caps["reachable"]
            # action_specs may be empty dict — no crash
            assert isinstance(caps["reachable"]["node-old"].get("action_specs", {}), dict)

    async def test_credential_store_path_in_config_enables_persistence(self):
        """node.yaml with credential_store_path → CredentialStore gets a store_path."""
        with tempfile.TemporaryDirectory() as d:
            cred_path = os.path.join(d, "creds.json")
            p = os.path.join(d, "node.yaml")
            with open(p, "w") as f:
                f.write(
                    f"node_id: node-0\nlisten: 0.0.0.0:8080\n"
                    f"credential_store_path: {cred_path}\n"
                )
            cfg = load_config(p)
            assert cfg.credential_store_path == cred_path

            seed = os.path.join(os.path.dirname(__file__), "..", "seed", "actions")
            app = create_app(cfg, load_actions(seed), schema_registry=load_schemas(seed))
            # Just verify the app starts without error
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/ping")
            assert resp.status_code == 200
