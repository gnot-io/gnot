# Guide 21 — Notification Actions (Telegram, Discord, SMS, WhatsApp)

**Difficulty:** Beginner  
**Prerequisite:** [Guide 08 — Advanced Custom Actions](../08-advanced-actions/README.md)  
**Goal:** Build a reusable notification action library — send alerts and messages through Telegram, Discord, Twilio SMS, and WhatsApp from any node in the mesh.

---

## Overview

All four actions follow the same pattern:
- Credentials go in `caller_credentials` (never in params or action files)
- Actions live on deb-0 (the gateway) so any node in the mesh can trigger notifications
- A single `/intent` prompt can decide which channel to use based on context

---

## Action 1 — Telegram

> **Setup:** Create a bot with [@BotFather](https://t.me/botfather) on Telegram → get `bot_token`. Start a conversation with your bot and get your `chat_id` via `https://api.telegram.org/bot<token>/getUpdates`.

### `notify_telegram.py`

```python
# ~/gnot-nodes/deb-0/actions/notify_telegram.py

import httpx

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    message    = params["message"]
    parse_mode = params.get("parse_mode", "Markdown")
    disable_preview = params.get("disable_web_page_preview", True)

    creds    = context.get("caller_credentials", {})
    bot_token = creds.get("telegram_bot_token")
    chat_id   = creds.get("telegram_chat_id")

    if not bot_token or not chat_id:
        raise ValueError("Missing credentials: telegram_bot_token and telegram_chat_id")

    payload = {
        "chat_id":    chat_id,
        "text":       message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
        )
        data = r.json()

    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")

    return {
        "sent": True,
        "message_id": data["result"]["message_id"],
        "chat_id": chat_id,
        "preview": message[:80] + ("..." if len(message) > 80 else ""),
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "notify_telegram",
  "description": "Send a message via Telegram Bot API. Requires telegram_bot_token and telegram_chat_id in caller_credentials.",
  "type": "object",
  "properties": {
    "message": {
      "type": "string",
      "description": "Message text. Supports Markdown formatting.",
      "minLength": 1,
      "maxLength": 4096
    },
    "parse_mode": {
      "type": "string",
      "enum": ["Markdown", "HTML", "MarkdownV2"],
      "default": "Markdown"
    },
    "disable_web_page_preview": {
      "type": "boolean",
      "default": true
    }
  },
  "required": ["message"],
  "additionalProperties": false
}
```

**Test it:**

```bash
curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "deb-0",
    "payload": {
      "action": "notify_telegram",
      "params": {"message": "🚀 GNOT mesh is online!"},
      "caller_credentials": {
        "telegram_bot_token": "123456:ABC-your-token",
        "telegram_chat_id": "987654321"
      }
    }
  }' | python3 -m json.tool
```

---

## Action 2 — Discord

> **Setup:** In your Discord server → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL.

### `notify_discord.py`

```python
# ~/gnot-nodes/deb-0/actions/notify_discord.py

import httpx

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    content   = params.get("content", "")
    username  = params.get("username", "GNOT Bot")
    embeds    = params.get("embeds", [])
    avatar_url = params.get("avatar_url")

    if not content and not embeds:
        raise ValueError("Provide at least 'content' or 'embeds'")

    creds       = context.get("caller_credentials", {})
    webhook_url = creds.get("discord_webhook_url")

    if not webhook_url:
        raise ValueError("Missing credential: discord_webhook_url")

    payload = {"username": username}
    if content:
        payload["content"] = content[:2000]
    if embeds:
        payload["embeds"] = embeds[:10]   # Discord limit: 10 embeds per message
    if avatar_url:
        payload["avatar_url"] = avatar_url

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(webhook_url, json=payload)

    # Discord returns 204 on success
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Discord webhook error: {r.status_code} — {r.text[:200]}")

    return {
        "sent": True,
        "channel": webhook_url.split("/")[-2],   # rough channel ID
        "preview": (content or str(embeds))[:80],
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "notify_discord",
  "description": "Post a message to a Discord channel via webhook. Requires discord_webhook_url in caller_credentials.",
  "type": "object",
  "properties": {
    "content": {
      "type": "string",
      "description": "Plain text message (up to 2000 characters)",
      "maxLength": 2000
    },
    "username": {
      "type": "string",
      "description": "Override the webhook's display name",
      "default": "GNOT Bot"
    },
    "embeds": {
      "type": "array",
      "description": "Discord embed objects (rich cards). See Discord embed documentation.",
      "items": {"type": "object"},
      "maxItems": 10
    },
    "avatar_url": {
      "type": "string",
      "description": "Override the webhook's avatar image URL"
    }
  },
  "additionalProperties": false
}
```

**Rich embed example:**

```bash
curl -s -X POST http://localhost:8080/action \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "deb-0",
    "payload": {
      "action": "notify_discord",
      "params": {
        "content": "⚠️ Deployment alert",
        "embeds": [{
          "title": "React App Deployed",
          "description": "Successfully deployed v2.1.0 to production",
          "color": 3066993,
          "fields": [
            {"name": "Node", "value": "cen-0", "inline": true},
            {"name": "Files", "value": "42", "inline": true},
            {"name": "Time", "value": "< 30s", "inline": true}
          ]
        }]
      },
      "caller_credentials": {
        "discord_webhook_url": "https://discord.com/api/webhooks/YOUR/WEBHOOK"
      }
    }
  }' | python3 -m json.tool
```

---

## Action 3 — Twilio SMS

> **Setup:** Sign up at [twilio.com](https://twilio.com) → get Account SID, Auth Token, and a Twilio phone number.

### `notify_sms.py`

```python
# ~/gnot-nodes/deb-0/actions/notify_sms.py

import httpx
import base64

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    to_number = params["to"]        # E.164 format: +84901234567
    message   = params["message"][:1600]  # SMS limit

    creds        = context.get("caller_credentials", {})
    account_sid  = creds.get("twilio_account_sid")
    auth_token   = creds.get("twilio_auth_token")
    from_number  = creds.get("twilio_from_number")   # your Twilio number

    if not all([account_sid, auth_token, from_number]):
        raise ValueError(
            "Missing credentials: twilio_account_sid, twilio_auth_token, twilio_from_number"
        )

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Basic {credentials}"},
            data={
                "To":   to_number,
                "From": from_number,
                "Body": message,
            },
        )
        data = r.json()

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Twilio error {r.status_code}: {data.get('message', data)}")

    return {
        "sent": True,
        "message_sid": data.get("sid"),
        "to": to_number,
        "status": data.get("status"),
        "preview": message[:60] + ("..." if len(message) > 60 else ""),
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "notify_sms",
  "description": "Send an SMS via Twilio. Requires twilio_account_sid, twilio_auth_token, and twilio_from_number in caller_credentials.",
  "type": "object",
  "properties": {
    "to": {
      "type": "string",
      "description": "Recipient phone number in E.164 format (e.g. +84901234567)"
    },
    "message": {
      "type": "string",
      "description": "SMS message body (max 1600 characters)",
      "minLength": 1,
      "maxLength": 1600
    }
  },
  "required": ["to", "message"],
  "additionalProperties": false
}
```

---

## Action 4 — WhatsApp (via Twilio)

WhatsApp through Twilio uses the same API as SMS — just a different `from` number format and pre-approved message templates.

> **Setup:** In the Twilio Console → Messaging → Try it out → Send a WhatsApp Message. You'll use a sandbox number for testing.

### `notify_whatsapp.py`

```python
# ~/gnot-nodes/deb-0/actions/notify_whatsapp.py

import httpx
import base64

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    to_number = params["to"]          # WhatsApp number: whatsapp:+84901234567
    message   = params["message"]
    media_url = params.get("media_url")   # optional image/video

    # Normalize number format
    if not to_number.startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"

    creds       = context.get("caller_credentials", {})
    account_sid = creds.get("twilio_account_sid")
    auth_token  = creds.get("twilio_auth_token")
    from_number = creds.get("twilio_whatsapp_number")   # whatsapp:+14155238886 (sandbox)

    if not all([account_sid, auth_token, from_number]):
        raise ValueError(
            "Missing credentials: twilio_account_sid, twilio_auth_token, twilio_whatsapp_number"
        )

    if not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()

    data = {"To": to_number, "From": from_number, "Body": message}
    if media_url:
        data["MediaUrl"] = media_url

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            url, headers={"Authorization": f"Basic {auth}"}, data=data,
        )
        resp = r.json()

    if r.status_code not in (200, 201):
        raise RuntimeError(f"WhatsApp error {r.status_code}: {resp.get('message', resp)}")

    return {
        "sent": True,
        "message_sid": resp.get("sid"),
        "to": to_number,
        "status": resp.get("status"),
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "notify_whatsapp",
  "description": "Send a WhatsApp message via Twilio. Requires twilio_account_sid, twilio_auth_token, and twilio_whatsapp_number in caller_credentials.",
  "type": "object",
  "properties": {
    "to": {
      "type": "string",
      "description": "Recipient WhatsApp number in E.164 format (e.g. +84901234567)"
    },
    "message": {
      "type": "string",
      "description": "Message text",
      "minLength": 1,
      "maxLength": 4096
    },
    "media_url": {
      "type": "string",
      "description": "Public URL of an image or video to attach (optional)"
    }
  },
  "required": ["to", "message"],
  "additionalProperties": false
}
```

---

## Multi-Channel Notification via /intent

With all four actions loaded, a single prompt can route to the right channel:

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Send an alert that the database migration completed successfully. Send it to: 1) Telegram with a markdown-formatted summary including table counts, 2) Discord with an embed card in green, 3) SMS to +84901234567 with a short one-line summary.",
    "session_id": "notify-all",
    "caller_credentials": {
      "telegram_bot_token": "123456:ABC-token",
      "telegram_chat_id": "987654321",
      "discord_webhook_url": "https://discord.com/api/webhooks/...",
      "twilio_account_sid": "ACxxxx",
      "twilio_auth_token": "your-auth-token",
      "twilio_from_number": "+15551234567"
    }
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

The LLM will call all three notification actions with appropriately formatted messages for each channel.

---

## Alerting Pattern — Combine with Health Checks

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Check disk usage on all nodes. If any node has disk usage above 85%, send a Telegram alert with the details. If all is OK, do nothing.",
    "session_id": "disk-alert",
    "caller_credentials": {
      "telegram_bot_token": "123456:ABC-token",
      "telegram_chat_id": "987654321"
    }
  }'
```

Add this to a cron job on deb-0 for automated monitoring.

---

## Summary

You now have four notification actions:
- ✅ `notify_telegram` — rich Markdown messages to any Telegram chat
- ✅ `notify_discord` — text and embed cards to Discord channels
- ✅ `notify_sms` — plain SMS via Twilio to any phone number
- ✅ `notify_whatsapp` — WhatsApp messages (text + media) via Twilio

All credentials are passed at call time — never stored in action files.

**Next:** [Guide 22 — Text-to-Image and Text-to-Speech Actions](../22-tts-tti-actions/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
