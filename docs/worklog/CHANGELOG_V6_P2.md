# GNOT v6.0 Phase 2 — Autonomy & Multi-Gateway

## New Files

### `runtime/scheduler.py`  (~350 LOC)
Autonomy engine. Supports 4 trigger types:
- **condition**: poll `check_action` every N seconds → fire `run_action` when `should_run=True`
- **cron**: 5-field cron expression (`*/5 * * * *`)
- **once**: fire at specific unix timestamp, then auto-disable
- **event**: subscribe EventBus pattern → reactive dispatch

Features: `max_concurrent`, `skip_if_running`, `retry_on_failure` w/ exponential backoff.

### `runtime/gateway_connection.py`  (~330 LOC)
Per-gateway register+heartbeat+poll lifecycle (extracted from WorkerAgent).
Implements **adaptive polling** (Level 2 autonomy):
- empty poll → interval × `poll_backoff_multiplier` (default 1.5)
- max: `poll_interval_max_seconds` (default 60s)
- job received → reset to base

## Modified Files

### `runtime/worker_agent.py`
Multi-gateway refactor: WorkerAgent orchestrates N `GatewayConnection` instances.
- Primary: `config.gateway_address`
- Additional: `config.additional_gateways` list
- `add_sub_route()` re-advertises to all gateways
- Full backward compat (single-gateway unchanged)

### `runtime/node_registry.py`
Added `registration_policy`:
- `"open"` (default): any authenticated node auto-registered (v5.x behavior)
- `"whitelist"`: only `trusted_nodes` accepted
- `"invite_only"`: reserved (treated as whitelist)

### `runtime/config.py`
New `NodeConfig` fields: `scheduler`, `schedule`, `poll_interval_max_seconds`,
`poll_backoff_multiplier`, `additional_gateways`, `registration_policy`.

### `runtime/models.py`
Added `ScheduleEntry`, `SchedulePatchRequest`.

### `runtime/server.py`
Wires Scheduler into lifespan + 5 new endpoints:
`POST /schedule`, `GET /schedule`, `DELETE /schedule/{id}`,
`POST /schedule/{id}/trigger`, `PATCH /schedule/{id}`

## Tests
`tests/test_scheduler.py` — **45 tests, all pass**

## Install
```bash
pip install croniter   # needed for cron triggers
```
