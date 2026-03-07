# Guide 08 — Advanced Custom Actions

**Difficulty:** Intermediate  
**Prerequisite:** [Guide 07 — Long-Running Actions and Async Polling](../07-async-polling/README.md)  
**Goal:** Write production-quality action plugins with input validation, error handling, external API calls, and caller-supplied credentials.

---

## Action Anatomy (Complete)

```python
# actions/my_action.py

# Optional: declare async if the action does I/O or takes time
ASYNC = True  # or False (default)

async def run(params: dict, context: dict) -> dict:
    """
    params:  validated dict from caller (matches schema)
    context: runtime info injected by GNOT:
      - context["node_id"]           → this node's ID
      - context["caller_credentials"] → dict of caller-supplied secrets
      - context["task_id"]           → unique task identifier
    """
    ...
    return { ... }   # must be JSON-serializable
```

GNOT validates `params` against the schema **before** calling `run`. If validation fails, the caller gets a `422` error — your code never runs.

---

## Example 1 — Input Validation and Error Handling

This action pings a host and returns latency. It demonstrates:
- Schema-enforced validation
- Graceful error handling
- Structured return values

### `ping_host.py`

```python
# ~/gnot-nodes/deb-1/actions/ping_host.py

import asyncio
import re

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    host = params["host"]
    count = params.get("count", 4)

    # Extra validation beyond what schema can express
    # Prevent command injection
    if not re.match(r'^[a-zA-Z0-9.\-]+$', host):
        raise ValueError(f"Invalid host format: {host!r}")

    command = f"ping -c {count} -W 3 {host}"

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        return {"reachable": False, "error": "timeout after 30s", "host": host}

    output = stdout.decode()
    exit_code = proc.returncode

    if exit_code != 0:
        return {
            "reachable": False,
            "host": host,
            "error": stderr.decode().strip() or "ping failed",
        }

    # Parse average RTT from: "rtt min/avg/max/mdev = 1.2/2.3/3.4/0.5 ms"
    avg_ms = None
    for line in output.splitlines():
        if "rtt min/avg/max" in line:
            try:
                avg_ms = float(line.split("=")[1].strip().split("/")[1])
            except (IndexError, ValueError):
                pass

    return {
        "reachable": True,
        "host": host,
        "packets_sent": count,
        "avg_rtt_ms": avg_ms,
        "raw_output": output.strip(),
    }
```

### `ping_host.schema.json`

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ping_host",
  "description": "Ping a hostname or IP and return reachability and latency.",
  "type": "object",
  "properties": {
    "host": {
      "type": "string",
      "description": "Hostname or IP address to ping",
      "minLength": 1,
      "maxLength": 253
    },
    "count": {
      "type": "integer",
      "description": "Number of ICMP packets to send",
      "minimum": 1,
      "maximum": 20,
      "default": 4
    }
  },
  "required": ["host"],
  "additionalProperties": false
}
```

---

## Example 2 — Calling an External API

This action queries a public HTTP API. It demonstrates:
- Using `httpx` for async HTTP calls
- Timeout handling for external calls
- Structuring the return value for LLM consumption

### `get_weather.py`

```python
# ~/gnot-nodes/deb-1/actions/get_weather.py
"""
Fetch current weather for a city using the Open-Meteo API (free, no key needed).
"""

import asyncio
import httpx

ASYNC = True

# Map city names to approximate coordinates (extend as needed)
CITIES = {
    "hanoi":         (21.0285, 105.8542),
    "ho chi minh":   (10.8231, 106.6297),
    "london":        (51.5074, -0.1278),
    "tokyo":         (35.6762, 139.6503),
    "new york":      (40.7128, -74.0060),
    "paris":         (48.8566, 2.3522),
    "singapore":     (1.3521, 103.8198),
}

WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 51: "Light drizzle", 61: "Light rain", 71: "Light snow",
    80: "Rain showers", 95: "Thunderstorm",
}


async def run(params: dict, context: dict) -> dict:
    city = params["city"].lower().strip()

    if city not in CITIES:
        available = sorted(CITIES.keys())
        raise ValueError(f"City {city!r} not found. Available: {available}")

    lat, lon = CITIES[city]
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weathercode"
        f"&temperature_unit=celsius"
    )

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise RuntimeError("Weather API timed out")
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Weather API error: {e.response.status_code}")

    data = response.json()
    current = data["current"]
    code = current.get("weathercode", 0)

    return {
        "city": city.title(),
        "temperature_c": current["temperature_2m"],
        "humidity_pct": current["relative_humidity_2m"],
        "wind_speed_kmh": current["wind_speed_10m"],
        "condition": WMO_CODES.get(code, f"Code {code}"),
        "coordinates": {"lat": lat, "lon": lon},
    }
```

### `get_weather.schema.json`

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "get_weather",
  "description": "Get current weather for a city. Supported cities: Hanoi, Ho Chi Minh, London, Tokyo, New York, Paris, Singapore.",
  "type": "object",
  "properties": {
    "city": {
      "type": "string",
      "description": "City name (case-insensitive). E.g.: 'hanoi', 'london', 'tokyo'"
    }
  },
  "required": ["city"],
  "additionalProperties": false
}
```

> **Dependency:** This action uses `httpx`. Install it on the node:
> ```bash
> pip install httpx --break-system-packages
> ```

---

## Example 3 — Caller-Supplied Credentials

Some actions need secrets (API keys, passwords) that the caller provides per-session — not hardcoded in the action. GNOT's `CredentialStore` handles this securely.

### `send_slack_message.py`

```python
# ~/gnot-nodes/deb-1/actions/send_slack_message.py
"""
Post a message to a Slack channel using a caller-supplied webhook URL.
The webhook URL is passed as a caller credential — not as a param.
"""

import httpx

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    message = params["message"]
    channel = params.get("channel", "#general")

    # Read the webhook from caller_credentials (supplied at /intent call time)
    credentials = context.get("caller_credentials", {})
    webhook_url = credentials.get("slack_webhook_url")

    if not webhook_url:
        raise ValueError(
            "Missing credential: 'slack_webhook_url'. "
            "Pass it in the caller_credentials field of your /intent request."
        )

    payload = {
        "channel": channel,
        "text": message,
        "username": "GNOT Bot",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(webhook_url, json=payload)
        success = r.status_code == 200 and r.text == "ok"

    return {
        "sent": success,
        "channel": channel,
        "message_preview": message[:80] + ("..." if len(message) > 80 else ""),
    }
```

### `send_slack_message.schema.json`

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "send_slack_message",
  "description": "Post a message to Slack. Requires 'slack_webhook_url' in caller_credentials.",
  "type": "object",
  "properties": {
    "message": {
      "type": "string",
      "description": "Message text to post",
      "minLength": 1,
      "maxLength": 4000
    },
    "channel": {
      "type": "string",
      "description": "Slack channel name (e.g. '#alerts')",
      "default": "#general"
    }
  },
  "required": ["message"],
  "additionalProperties": false,
  "x-caller-credentials": {
    "slack_webhook_url": {
      "description": "Slack incoming webhook URL",
      "required": true,
      "hint": "https://hooks.slack.com/services/..."
    }
  }
}
```

**Calling with credentials:**

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Post a message to Slack saying the GNOT mesh is online.",
    "session_id": "slack-test",
    "caller_credentials": {
      "slack_webhook_url": "https://hooks.slack.com/services/YOUR/WEBHOOK"
    }
  }' | python3 -m json.tool
```

The credential is encrypted (AES-256-GCM) and stored for the session. Subsequent `/intent` calls in the same session reuse it automatically — no need to re-supply.

---

## Deploying the New Actions

```bash
# Restart deb-1 to load all new actions
kill $(cat ~/gnot-nodes/deb-1/node.pid)
cd /path/to/gnot
nohup python3 gnot/src/node_runtime.py --config ~/gnot-nodes/deb-1/node.yaml \
    > ~/gnot-nodes/deb-1/node.log 2>&1 &
echo $! > ~/gnot-nodes/deb-1/node.pid

# Verify new actions are loaded
curl -s http://localhost:8080/capabilities \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import sys, json
caps = json.load(sys.stdin)
deb1 = caps['reachable'].get('deb-1', {})
print('deb-1 actions:', deb1.get('actions', []))
"
```

---

## Best Practices Checklist

| Practice | Why |
|----------|-----|
| Validate inputs beyond the schema when needed | Schema catches type errors; Python catches logic errors (injection, range) |
| Always set timeouts on external calls | Prevents the action from hanging forever |
| Return structured dicts, not strings | The LLM can parse and reason about structured data |
| Use `raise ValueError("...")` for user errors | GNOT surfaces the message in the `error` field |
| Use `raise RuntimeError("...")` for system errors | Same effect, but semantically different for humans reading logs |
| Never hardcode credentials in action files | Use `caller_credentials` or environment variables |
| Keep actions focused | One action = one responsibility. Compose in the LLM, not in action code |

---

## Summary

You can now write:
- ✅ Actions with robust input validation and error handling
- ✅ Actions that call external HTTP APIs
- ✅ Actions that use caller-supplied credentials (via `CredentialStore`)

**Next:** [Guide 09 — Execute Across Multiple Nodes](../09-multi-node-execution/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
