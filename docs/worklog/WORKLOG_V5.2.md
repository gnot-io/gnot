# WORKLOG v5.2 — One-liner Setup, LLM Client, Distribution Endpoints

**Date:** 2026-03-01
**Runtime:** Mesh Node Runtime v5.2
**Repository:** ai-infra-runtime-v2/mesh

---

## 1. Overview

v5.2 adds 3 major capabilities that make the mesh self-distributing and AI-native:

| # | Feature | Description |
|---|---------|-------------|
| A | One-liner Node Setup | Universal `setup.sh` — any Linux distro, one command |
| B | Distribution Endpoints | `GET /setup.sh` + `GET /runtime-bundle` served from gateway |
| C | Shared LLM Client | `runtime/llm_client.py` — OpenAI-compatible async client injected into every action context |

These enable the core use case: **Claude sends a prompt → gateway serves setup script → new node bootstraps → LLM-powered actions run on any node.**

---

## 2. Feature Details

### Feature A — Universal Setup Script (`static/setup.sh`)

**491 lines** — handles Debian/Ubuntu, CentOS/RHEL, AlmaLinux/Rocky, Arch, Alpine.

**Usage:**
```bash
curl -sSL https://local-agent-server-v5.vietml.com/setup.sh | bash -s -- \
    --node-id node-B \
    --port 8080 \
    --gateway http://10.0.0.1:8080 \
    --systemd
```

**What it does (8 steps):**

| Step | Action | Notes |
|------|--------|-------|
| 1 | Detect OS | Reads `/etc/os-release`, maps to family (debian/rhel/arch/alpine) |
| 2 | Install Python 3.10+ | `apt` / `dnf` / `pacman` / `apk` — tries python3.11 first |
| 3 | Download runtime | Fetches `/runtime-bundle` tar.gz from gateway |
| 4 | Setup venv | Creates virtualenv, installs requirements |
| 5 | Configure node | Generates `node.yaml` + `skills.md`, copies seed actions |
| 6 | Start node | `nohup` background process, health check |
| 7 | Systemd (optional) | Creates + enables `mesh-<node-id>.service` |
| 8 | Print summary | Connection info, test commands |

**Options:**

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--node-id` | ✅ | — | Unique node identifier |
| `--gateway` | ✅ | — | Gateway HTTP URL (internal IP) |
| `--port` | ❌ | 8080 | Listen port |
| `--auth-token` | ❌ | — | Bearer token (only needed if public-facing) |
| `--install-dir` | ❌ | /opt/mesh | Installation directory |
| `--systemd` | ❌ | false | Create systemd service |

### Feature B — Distribution Endpoints

Two new endpoints on the gateway, **exempt from auth** (so `setup.sh` can download without a token):

| Endpoint | Method | Auth | Response |
|----------|--------|------|----------|
| `/setup.sh` | GET | ❌ exempt | Shell script (`text/x-shellscript`) |
| `/runtime-bundle` | GET | ❌ exempt | Tar.gz archive (`application/gzip`) |

**Runtime bundle contents:** `runtime/`, `seed/`, `static/`, `node_runtime.py`, `requirements.txt`, `pyproject.toml`

**Auth exempt paths (updated):** `/health`, `/skills`, `/docs`, `/openapi.json`, `/setup.sh`, `/runtime-bundle`

### Feature C — Shared LLM Client (`runtime/llm_client.py`)

**322 lines** — async OpenAI-compatible client using `httpx`.

**Configuration in `node.yaml`:**
```yaml
# Flat style
llm_api_key: sk-xxx
llm_base_url: https://api.openai.com/v1
llm_default_model: gpt-4o-mini

# Or nested style
llm:
  api_key: sk-xxx
  base_url: https://api.openai.com/v1
  default_model: gpt-4o-mini
  timeout_seconds: 120
  extra_headers:
    X-Custom: value
```

**Compatible providers:**

| Provider | base_url | Notes |
|----------|----------|-------|
| OpenAI | `https://api.openai.com/v1` | Default |
| Anthropic | Via proxy (litellm) | Needs OpenAI-compatible proxy |
| Ollama | `http://localhost:11434/v1` | Local LLM |
| vLLM | `http://localhost:8000/v1` | Self-hosted |
| LiteLLM | `http://localhost:4000/v1` | Multi-provider proxy |

**Client API:**

| Method | Purpose | Returns |
|--------|---------|---------|
| `chat(messages, system, model, temperature, json_mode)` | Full chat completion | `LLMResponse` |
| `ask(prompt, system)` | Simple single-turn Q&A | `str` |
| `generate_image_prompt(description, style)` | Optimize prompt for image gen | `str` |
| `generate_image(prompt, model, size, quality)` | DALL-E image generation | `dict` |

**Injection:** The LLM client is automatically injected into every action's `context["llm"]`:

```python
# In any action module:
async def run(params, context):
    llm = context["llm"]  # LLMClient instance
    response = await llm.ask("What is 2+2?")
    # or
    result = await llm.chat(messages=[...], system="You are...")
    # or
    image = await llm.generate_image("A sunset over mountains")
```

### New LLM-Powered Actions

**`generate_image`** (async) — Text-to-image via DALL-E or compatible API

| Param | Type | Required | Default |
|-------|------|----------|---------|
| `prompt` | string | ✅ | — |
| `output_path` | string | ❌ | — |
| `size` | enum | ❌ | 1024x1024 |
| `model` | string | ❌ | dall-e-3 |
| `quality` | enum | ❌ | standard |
| `optimize_prompt` | bool | ❌ | false |

**`llm_chat`** (async) — General-purpose LLM chat/completion

| Param | Type | Required | Default |
|-------|------|----------|---------|
| `prompt` | string | ✅* | — |
| `system` | string | ❌ | — |
| `messages` | array | ❌* | — |
| `model` | string | ❌ | config default |
| `temperature` | float | ❌ | 0.7 |
| `max_tokens` | int | ❌ | 4096 |
| `json_mode` | bool | ❌ | false |

*One of `prompt` or `messages` required.

---

## 3. File Changes

### New Files (8)

| File | Lines | Description |
|------|-------|-------------|
| `static/setup.sh` | 491 | Universal one-liner setup script |
| `runtime/llm_client.py` | 322 | Async OpenAI-compatible LLM client |
| `seed/actions/generate_image.py` | 100 | Text-to-image action |
| `seed/actions/generate_image.schema.json` | 37 | Schema for generate_image |
| `seed/actions/llm_chat.py` | 87 | General LLM chat action |
| `seed/actions/llm_chat.schema.json` | 47 | Schema for llm_chat |
| `tests/test_llm_client.py` | 277 | LLM client unit tests (11 tests) |
| `tests/test_v52_features.py` | 180 | Config, auth, endpoint tests (13 tests) |

### Modified Files (5)

| File | Changes |
|------|---------|
| `runtime/config.py` | Added `llm_api_key`, `llm_base_url`, `llm_default_model`, `llm_timeout_seconds`, `llm_extra_headers`, `llm_enabled` property; supports flat + nested `llm:` section |
| `runtime/action_executor.py` | Added `llm_client` parameter; injects `context["llm"]` into every action |
| `runtime/server.py` | Creates `LLMClient` from config; passes to executor; added `/setup.sh` + `/runtime-bundle` endpoints; passes `runtime_base_dir` |
| `runtime/auth.py` | Added `/setup.sh` and `/runtime-bundle` to `DEFAULT_EXEMPT_PATHS` |
| `node_runtime.py` | Updated to v5.2; resolves `runtime_base_dir`; logs LLM status |

---

## 4. Test Results

| Test File | Tests | Status |
|-----------|-------|--------|
| test_action_loader.py | 5 | ✅ PASS |
| test_actions.py | 9 | ✅ PASS |
| test_auth.py | 6 | ✅ PASS |
| test_bootstrap.py | 8 | ✅ PASS |
| test_integration.py | 10 | ✅ PASS |
| test_job_cleanup.py | 7 | ✅ PASS |
| test_job_manager.py | 8 | ✅ PASS |
| test_llm_client.py | 11 | ✅ PASS |
| test_resolver.py | 4 | ✅ PASS |
| test_router.py | 5 | ✅ PASS |
| test_schema_validator.py | 12 | ✅ PASS |
| test_v52_features.py | 13 | ✅ PASS |
| **TOTAL** | **102** | **✅ ALL PASS** |

---

## 5. Live Verification

| Test | Result |
|------|--------|
| `GET /health` → actions_loaded: 5 | ✅ (was 3 in v5.1) |
| `GET /setup.sh` (no auth) → shell script | ✅ |
| `GET /runtime-bundle` (no auth) → 80KB tar.gz | ✅ |
| `generate_image` missing prompt → 422 schema error | ✅ |
| `generate_image` invalid size → 422 enum validation | ✅ |
| `llm_chat` without LLM config → clear error message | ✅ |
| All v5.1 features (auth, schema, async, cleanup) | ✅ unchanged |

---

## 6. Metrics

| Metric | v5.0 | v5.1 | v5.2 |
|--------|------|------|------|
| Files | 25 | 36 | 44 |
| Lines of code | ~1,200 | ~2,400 | ~4,000 |
| Tests | 35 | 78 | 102 |
| Endpoints | 5 | 6 | 8 |
| Actions (seed) | 3 | 3 | 5 |
| Dependencies | 8 | 9 | 9 |

---

## 7. Remaining / Next Steps

| Priority | Item | Description |
|----------|------|-------------|
| P1 | Cloud AI Planner | ReAct loop, task lifecycle, skills caching — the "brain" |
| P1 | Bootstrap + setup.sh integration | Claude calls bootstrap → bootstrap invokes setup.sh on remote via SSH |
| P2 | Multi-node live test | Deploy node-B on CentOS, node-C on AlmaLinux using setup.sh |
| P2 | Container isolation | Per-node Docker/Podman containers |
| P3 | LLM action examples | Code review, summarize file, translate, extract structured data |
| P3 | Persistent job store | SQLite/Redis for job history across restarts |
| P3 | Observability stack | Prometheus metrics, structured JSON logs |
| P4 | Migrate FastAPI on_event → lifespan | Remove deprecation warnings |

---

*Generated: 2026-03-01 | Mesh Runtime v5.2 | 102/102 tests passing*
