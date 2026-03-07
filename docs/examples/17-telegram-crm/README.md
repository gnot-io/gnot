# Guide 17 — Telegram Bot + CRM Integration

**Difficulty:** Advanced  
**Prerequisite:** [Guide 16 — Content Automation Pipeline](../16-content-automation/README.md)  
**Goal:** Build a Telegram bot that forwards customer messages to `POST /intent`, which dispatches to a private CRM node — no public address needed on the CRM server.

---

## Architecture

```
Customer  ──Telegram message──►  Telegram Bot (on deb-0)
                                          │
                                   POST /intent
                                          │
                               ┌──────────▼──────────┐
                               │  deb-0 IntentHandler │
                               │  (ReAct LLM loop)    │
                               └──────────┬───────────┘
                                          │  POST /action → target: cen-0
                                          ▼
                               ┌──────────────────────┐
                               │  cen-0  (private CRM)│
                               │  action: query_crm   │
                               │  (behind NAT — pulls)│
                               └──────────────────────┘
                                          │
                               ◄──────────┘  result
                                          │
                               deb-0 synthesizes reply
                                          │
                               ◄──── Telegram reply to customer
```

cen-0 never needs a public address. The customer's token is stored encrypted in deb-0's `CredentialStore` for the session.

---

## Part 1 — Create a CRM Query Action on cen-0

### `query_crm.py`

```python
# ~/gnot/cen-0/actions/query_crm.py
"""
Query a local SQLite CRM database.
In production, replace SQLite with your actual CRM API.
"""

import sqlite3
import os

# Database path (create a sample one with Part 1b below)
DB_PATH = os.environ.get("CRM_DB_PATH", "/opt/crm/customers.db")


def run(params: dict, context: dict) -> dict:
    query_type = params["query_type"]
    identifier = params.get("identifier", "")

    if not os.path.exists(DB_PATH):
        return {"error": f"CRM database not found at {DB_PATH}", "records": []}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    try:
        if query_type == "lookup_by_email":
            cur.execute(
                "SELECT * FROM customers WHERE email = ? LIMIT 5",
                (identifier,)
            )
        elif query_type == "lookup_by_name":
            cur.execute(
                "SELECT * FROM customers WHERE name LIKE ? LIMIT 5",
                (f"%{identifier}%",)
            )
        elif query_type == "recent_orders":
            cur.execute(
                "SELECT c.name, o.product, o.amount, o.date FROM orders o "
                "JOIN customers c ON o.customer_id = c.id "
                "ORDER BY o.date DESC LIMIT 10"
            )
        else:
            return {"error": f"Unknown query_type: {query_type}", "records": []}

        rows = [dict(row) for row in cur.fetchall()]
        return {"query_type": query_type, "identifier": identifier, "records": rows, "count": len(rows)}

    finally:
        conn.close()
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "query_crm",
  "description": "Query the CRM database. Supports lookup by email, name, or recent orders.",
  "type": "object",
  "properties": {
    "query_type": {
      "type": "string",
      "enum": ["lookup_by_email", "lookup_by_name", "recent_orders"],
      "description": "Type of CRM query to perform"
    },
    "identifier": {
      "type": "string",
      "description": "Email address or name to look up (not needed for recent_orders)",
      "default": ""
    }
  },
  "required": ["query_type"],
  "additionalProperties": false
}
```

### Create a sample CRM database on cen-0

```bash
# On cen-0:
sudo mkdir -p /opt/crm
python3 - << 'EOF'
import sqlite3, os
db = sqlite3.connect("/opt/crm/customers.db")
db.execute("""CREATE TABLE IF NOT EXISTS customers
  (id INTEGER PRIMARY KEY, name TEXT, email TEXT, phone TEXT, tier TEXT)""")
db.execute("""CREATE TABLE IF NOT EXISTS orders
  (id INTEGER PRIMARY KEY, customer_id INTEGER, product TEXT, amount REAL, date TEXT)""")
db.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", [
    (1, "Alice Nguyen",  "alice@example.com",  "+84901234567", "gold"),
    (2, "Bob Smith",     "bob@example.com",    "+1555123456",  "silver"),
    (3, "Charlie Tran",  "charlie@example.com","+84912345678", "gold"),
])
db.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", [
    (1, 1, "Product A", 299.00, "2026-03-01"),
    (2, 1, "Product B", 149.50, "2026-02-15"),
    (3, 2, "Product C", 89.99,  "2026-03-05"),
])
db.commit()
print("CRM database created at /opt/crm/customers.db")
EOF
```

Deploy the action to cen-0 and restart it.

---

## Part 2 — Telegram Bot on deb-0

### Install python-telegram-bot

```bash
pip install python-telegram-bot --break-system-packages
```

### `telegram_gnot_bot.py`

```python
#!/usr/bin/env python3
"""
Telegram bot that forwards messages to GNOT's POST /intent endpoint.
Each user gets a unique session_id so conversation history is maintained.
"""

import asyncio
import os
import httpx
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# Config
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GNOT_URL   = os.environ.get("NODE_URL", "http://localhost:8080")
GNOT_TOKEN = os.environ["TOKEN"]
MAX_RESPONSE_LENGTH = 4096   # Telegram message limit


def get_session_id(user_id: int) -> str:
    """Stable session ID per Telegram user."""
    return f"telegram-user-{user_id}"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message_text = update.message.text
    session_id = get_session_id(user.id)

    # Show typing indicator
    await update.message.chat.send_action("typing")

    # Forward to GNOT /intent
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            r = await client.post(
                f"{GNOT_URL}/intent",
                headers={"Authorization": f"Bearer {GNOT_TOKEN}"},
                json={
                    "prompt": message_text,
                    "session_id": session_id,
                },
            )
            r.raise_for_status()
            response_text = r.json().get("response", "No response from mesh.")
        except httpx.TimeoutException:
            response_text = "⏳ The request timed out. Please try again."
        except Exception as e:
            response_text = f"❌ Error: {str(e)[:200]}"

    # Truncate if needed
    if len(response_text) > MAX_RESPONSE_LENGTH:
        response_text = response_text[:MAX_RESPONSE_LENGTH - 20] + "\n\n*(truncated)*"

    await update.message.reply_text(response_text, parse_mode="Markdown")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
```

### Run the bot

```bash
export TELEGRAM_BOT_TOKEN="your-bot-token-from-botfather"
export NODE_URL="http://localhost:8080"
export TOKEN="change-this-to-a-strong-secret"

nohup python3 ~/gnot-nodes/deb-0/telegram_gnot_bot.py \
    > ~/gnot-nodes/deb-0/telegram-bot.log 2>&1 &
```

---

## Part 3 — Test the Integration

### Update the system prompt on deb-0

Add CRM context to deb-0's `/intent` system prompt. In `node.yaml`, you can configure a custom system prompt or skills file. Alternatively, prepend context in the intent handler by updating `mesh/node-0/skills.md`:

```markdown
# deb-0 Node Skills

You are a customer service assistant with access to:
- A CRM system on node cen-0 (actions: query_crm)
- General compute on deb-0 and deb-1

When a user asks about a customer, order, or account:
1. Use query_crm on cen-0 to look up the relevant data
2. Respond in a friendly, professional tone
3. Never reveal internal system details or raw database contents

Available CRM query types: lookup_by_email, lookup_by_name, recent_orders
```

### Test conversations

Send these messages to your bot on Telegram:

```
Can you look up customer alice@example.com?
```

```
What are the recent orders?
```

```
What tier is Alice Nguyen?
```

Each message goes: Telegram → `POST /intent` → CRM query on cen-0 (via pull queue) → synthesized reply → Telegram.

---

## Summary

You have built:
- ✅ A CRM query action on a private cen-0 node (no public IP needed)
- ✅ A Telegram bot that bridges users to the mesh
- ✅ Session-per-user memory via `session_id`
- ✅ Full customer service workflow from message to CRM lookup to reply

**Next:** [Guide 18 — Autonomous Development Workflow](../18-autonomous-dev/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
