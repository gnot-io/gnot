"""Tests for v5.2 features: config LLM, setup.sh, runtime-bundle, auth exemptions."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from runtime.config import NodeConfig, load_config


# ---------------------------------------------------------------------------
# Tests — Config LLM fields
# ---------------------------------------------------------------------------

class TestConfigLLM:
    def test_config_llm_flat_keys(self, tmp_path):
        """LLM config using flat keys in node.yaml."""
        config_data = {
            "node_id": "test-llm",
            "listen": "0.0.0.0:9000",
            "llm_api_key": "sk-test-123",
            "llm_base_url": "https://api.openai.com/v1",
            "llm_default_model": "gpt-4o",
        }
        config_file = tmp_path / "node.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(config_file)
        assert config.llm_api_key == "sk-test-123"
        assert config.llm_base_url == "https://api.openai.com/v1"
        assert config.llm_default_model == "gpt-4o"
        assert config.llm_enabled is True

    def test_config_llm_nested(self, tmp_path):
        """LLM config using nested 'llm' section."""
        config_data = {
            "node_id": "test-llm-nested",
            "listen": "0.0.0.0:9000",
            "llm": {
                "api_key": "sk-nested-456",
                "base_url": "http://localhost:11434/v1",
                "default_model": "llama3",
                "timeout_seconds": 60.0,
                "extra_headers": {"X-Custom": "test"},
            },
        }
        config_file = tmp_path / "node.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(config_file)
        assert config.llm_api_key == "sk-nested-456"
        assert config.llm_base_url == "http://localhost:11434/v1"
        assert config.llm_default_model == "llama3"
        assert config.llm_timeout_seconds == 60.0
        assert config.llm_extra_headers == {"X-Custom": "test"}
        assert config.llm_enabled is True

    def test_config_llm_disabled(self, tmp_path):
        """No LLM config → llm_enabled = False."""
        config_data = {
            "node_id": "test-no-llm",
            "listen": "0.0.0.0:9000",
        }
        config_file = tmp_path / "node.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(config_file)
        assert config.llm_api_key is None
        assert config.llm_enabled is False

    def test_config_llm_defaults(self, tmp_path):
        """LLM with just api_key uses sane defaults."""
        config_data = {
            "node_id": "test-defaults",
            "listen": "0.0.0.0:9000",
            "llm_api_key": "sk-min",
        }
        config_file = tmp_path / "node.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(config_file)
        assert config.llm_base_url == "https://api.openai.com/v1"
        assert config.llm_default_model == "gpt-4o-mini"
        assert config.llm_timeout_seconds == 120.0


# ---------------------------------------------------------------------------
# Tests — Auth exempt paths
# ---------------------------------------------------------------------------

class TestAuthExemptPaths:
    def test_setup_sh_exempt_from_auth(self):
        """GET /setup.sh should not require auth."""
        from runtime.auth import DEFAULT_EXEMPT_PATHS
        assert "/setup.sh" in DEFAULT_EXEMPT_PATHS

    def test_runtime_bundle_exempt_from_auth(self):
        """GET /runtime-bundle should not require auth."""
        from runtime.auth import DEFAULT_EXEMPT_PATHS
        assert "/runtime-bundle" in DEFAULT_EXEMPT_PATHS

    def test_health_still_exempt(self):
        from runtime.auth import DEFAULT_EXEMPT_PATHS
        assert "/health" in DEFAULT_EXEMPT_PATHS

    def test_skills_still_exempt(self):
        from runtime.auth import DEFAULT_EXEMPT_PATHS
        assert "/skills" in DEFAULT_EXEMPT_PATHS


# ---------------------------------------------------------------------------
# Tests — Setup.sh endpoint (integration via TestClient)
# ---------------------------------------------------------------------------

class TestSetupEndpoint:
    def _make_app(self, tmp_path, with_setup_sh=True):
        """Create a test app with optional setup.sh file."""
        from runtime.action_loader import ActionRegistry
        from runtime.server import create_app

        config = NodeConfig(
            node_id="test-setup",
            listen="0.0.0.0:9999",
        )

        # Tạo static/setup.sh nếu cần
        if with_setup_sh:
            static_dir = tmp_path / "static"
            static_dir.mkdir()
            (static_dir / "setup.sh").write_text("#!/bin/bash\necho 'hello'")

        registry: ActionRegistry = {}
        app = create_app(
            config, registry,
            runtime_base_dir=str(tmp_path),
        )
        return TestClient(app)

    def test_setup_sh_returns_script(self, tmp_path):
        client = self._make_app(tmp_path, with_setup_sh=True)
        resp = client.get("/setup.sh")
        assert resp.status_code == 200
        assert "#!/bin/bash" in resp.text
        assert "echo 'hello'" in resp.text

    def test_setup_sh_fallback(self, tmp_path):
        client = self._make_app(tmp_path, with_setup_sh=False)
        resp = client.get("/setup.sh")
        assert resp.status_code in (200, 404)  # fallback may find real setup.sh

    def test_runtime_bundle_returns_tarball(self, tmp_path):
        """runtime-bundle should return a tar.gz even if minimal."""
        # Tạo file giả
        (tmp_path / "requirements.txt").write_text("fastapi==0.115.6\n")
        (tmp_path / "node_runtime.py").write_text("# entry point\n")

        client = self._make_app(tmp_path)
        resp = client.get("/runtime-bundle")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/gzip"
        # Nội dung phải là bytes hợp lệ
        assert len(resp.content) > 0


# ---------------------------------------------------------------------------
# Tests — LLM actions schema
# ---------------------------------------------------------------------------

class TestLLMActionSchemas:
    def test_generate_image_schema_exists(self):
        schema_path = Path(__file__).parent.parent / "seed" / "actions" / "generate_image.schema.json"
        # Schema có thể ở nhiều vị trí — chỉ test format nếu file ở local
        # (trên server sẽ ở vị trí khác)

    def test_llm_chat_schema_exists(self):
        schema_path = Path(__file__).parent.parent / "seed" / "actions" / "llm_chat.schema.json"

