# GNOT Transport Bridges

This directory contains **channel transport bridge** plugins for GNOT v6.0 Phase 7.

## Architecture

```
runtime/transport_bridge.py     ← ABC + Registry (the ONLY shared surface)
transports/
  telegram/bridge.py            ← TelegramBridge(ChannelTransportBridge)
  slack/                        ← Not yet implemented (see slack/README.md)
  discord/                      ← Not yet implemented
```

**Design rule:** `runtime/` never imports from `transports/`. The coupling is one-way:
`transports/` imports from `runtime/transport_bridge.py` and `runtime/models.py` only.

## How to write a new transport bridge

### 1. Create directory structure

```
transports/{channel}/
  __init__.py
  bridge.py         ← main bridge (subclass ChannelTransportBridge)
  bot_api.py        ← pure HTTP client for the channel API (no GNOT deps)
  config.py         ← data models / config dataclasses
  formatters.py     ← message formatters
  tests/
    __init__.py
    test_bridge.py
```

### 2. Implement ChannelTransportBridge

```python
# transports/slack/bridge.py

from runtime.transport_bridge import ChannelTransportBridge
from runtime.models import ExternalParticipant, InteractionThread
from fastapi import APIRouter

class SlackBridge(ChannelTransportBridge):
    transport_id = "slack"                    # must be unique

    def __init__(self, config) -> None:
        self._config = config
        self._ready = False

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def startup(self) -> None:
        """Connect to Slack, load state, start background tasks."""
        # ... initialize Slack client, load persisted channels ...
        self._ready = True

    async def shutdown(self) -> None:
        """Stop gracefully. Must not raise."""
        try:
            # ... disconnect from Slack ...
            pass
        except Exception as exc:
            logger.error("SlackBridge: shutdown error: %s", exc)
        self._ready = False

    async def notify(
        self,
        participant: ExternalParticipant,
        thread: InteractionThread,
    ) -> None:
        """Push Q&A notification to participant. Must not raise."""
        try:
            # participant.transport_target = Slack channel/user ID
            # ...
            pass
        except Exception as exc:
            logger.error("SlackBridge.notify: %s", exc)

    def get_fastapi_router(self) -> APIRouter | None:
        """Return router for inbound Slack events (or None for polling-only)."""
        from transports.slack.webhook_router import build_router
        return build_router(self)
```

### 3. Enable in node.yaml

```yaml
transports:
  slack:
    enabled: true
    # channel-specific config here
```

### 4. Register in server.py `_setup_transport_bridges()`

```python
if config.slack.enabled:
    from transports.slack.bridge import SlackBridge
    registry.register(SlackBridge(config=config))
```

### 5. Add SlackConfig to runtime/config.py

```python
@dataclass(frozen=True)
class SlackTransportConfig:
    enabled: bool = False
    # ...
```

## Rules

- `transports/{channel}/` imports from `runtime/transport_bridge.py` and `runtime/models.py` ONLY
- `runtime/` must have zero imports from `transports/`
- `notify()` must catch all exceptions internally — never let them propagate to InteractionRouter
- `startup()` and `shutdown()` are idempotent — safe to call multiple times
- `is_ready` must reflect actual runtime state (not just "initialized")
- Bot tokens / API keys must never appear in API responses

## Currently available transports

| Transport | Status | Directory |
|-----------|--------|-----------|
| `telegram` | ✅ v6.0 Phase 7 | `transports/telegram/` |
| `slack` | ⏳ Deferred | `transports/slack/README.md` |
| `discord` | ⏳ Deferred | `transports/discord/README.md` |
| `webhook` | ✅ Built-in | `runtime/interaction_router.py` |
| `polling` | ✅ Built-in | `runtime/interaction_router.py` |
