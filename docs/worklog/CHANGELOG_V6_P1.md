# Phase 1: Event Foundation — GNOT v6.0

**Date:** 2026-03-08
**Base:** v5.13b → v6.0 Phase 1

## New files

| File | LOC | Description |
|------|-----|-------------|
| `runtime/event_bus.py` | ~300 | EventBus core: emit, subscribe, background delivery worker, JSONL persistence |
| `runtime/channel_registry.py` | ~35 | Thin view of NodeRegistry from channel perspective |
| `tests/test_event_bus.py` | ~400 | 53 unit tests covering all Phase 1 features |

## Modified files

| File | Changes |
|------|---------|
| `runtime/models.py` | Added: `Event`, `Subscription`, `EmitRequest`, `EmitResponse`, `SubscribeRequest`, `SubscribeResponse` |
| `runtime/config.py` | Added: `EventBusConfig` dataclass, `event_bus` field in `NodeConfig`, `event_bus` section parsing in `load_config()` |
| `runtime/server.py` | Version → 6.0.0; wired EventBus + ChannelRegistry; added 5 HTTP endpoints; updated lifespan start/stop; updated `/health` with v6 info |

## New HTTP endpoints

- `POST /emit` — publish event
- `POST /subscribe` — register subscription
- `DELETE /subscriptions/{sub_id}` — cancel subscription
- `GET /subscriptions` — list subscriptions
- `GET /events` — replay event log (with filters: event_type, source_node, since, limit)

## node.yaml config (optional)

```yaml
event_bus:
  enabled: true                    # default: true
  max_log_size: 10000              # default: 10000
  delivery_timeout_seconds: 10     # default: 10
  delivery_retry_count: 3          # default: 3
  delivery_retry_backoff: 2.0      # default: 2.0
  persistence_path: /tmp/events.jsonl  # default: null (disabled)
```

## Acceptance criteria — all met

- ✅ Node emits event → subscriber gets callback action dispatched via GatewayRouter
- ✅ Event log queryable via GET /events with filters
- ✅ Events persisted to JSONL file (configurable path)
- ✅ Zero impact on existing endpoints (backward compatible)
- ✅ 53 tests — all pass

## Deploy

```bash
# Extract into project root (preserving directory structure)
unzip patch_v6_phase1.zip -d /path/to/gnot/
```
