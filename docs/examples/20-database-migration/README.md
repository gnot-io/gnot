# Guide 20 — Database Migration Between Private Networks

**Difficulty:** Intermediate  
**Prerequisite:** [Guide 12 — Cross-Node Communication](../12-cross-node-communication/README.md)  
**Goal:** Migrate a live database (MySQL or PostgreSQL) from cen-0 to alm-0 — two machines on different private networks with no direct connection — using deb-0 as the transfer relay.

---

## The Problem

```
cen-0  (CentOS, network A — has source DB)
  │  ✗  no direct path
alm-0  (AlmaLinux, network B — destination)
  
  ✓  both can reach:
deb-0  (gateway, public via Cloudflare)
```

Direct `mysqldump | ssh` or `pg_dump | psql` won't work across NAT. The dump travels as a file through deb-0's staging area.

---

## MySQL / MariaDB Migration

### Part 1 — The `mysql_dump` Action (on cen-0)

```python
# ~/gnot/cen-0/actions/mysql_dump.py
"""
Dump one or all MySQL databases to a compressed .sql.gz file.
"""

import asyncio
import os
import time

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    database   = params.get("database", "--all-databases")
    output_dir = params.get("output_dir", "/tmp/gnot-dumps")
    host       = params.get("host", "127.0.0.1")
    port       = params.get("port", 3306)

    # Credentials from caller_credentials (never hardcode passwords)
    creds = context.get("caller_credentials", {})
    user  = creds.get("mysql_user", "root")
    password = creds.get("mysql_password", "")

    os.makedirs(output_dir, exist_ok=True)

    db_slug   = database.replace("--all-databases", "ALL").replace(" ", "_")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    dump_file = os.path.join(output_dir, f"{db_slug}_{timestamp}.sql.gz")

    # Build mysqldump command — pipe directly through gzip
    if database == "--all-databases":
        dump_cmd = f"mysqldump --all-databases"
    else:
        dump_cmd = f"mysqldump {database}"

    pw_arg = f"-p{password}" if password else ""
    cmd = f"mysqldump -h {host} -P {port} -u {user} {pw_arg} {database} | gzip > {dump_file}"

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)

    if proc.returncode != 0:
        raise RuntimeError(f"mysqldump failed: {stderr.decode()[-400:]}")

    size_mb = os.path.getsize(dump_file) / (1024 * 1024)

    return {
        "dump_file": dump_file,
        "database": database,
        "size_mb": round(size_mb, 2),
        "timestamp": timestamp,
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "mysql_dump",
  "description": "Dump a MySQL database to a compressed .sql.gz file. Requires mysql_user and mysql_password in caller_credentials.",
  "type": "object",
  "properties": {
    "database": {
      "type": "string",
      "description": "Database name to dump, or '--all-databases' for all",
      "default": "--all-databases"
    },
    "output_dir": {
      "type": "string",
      "description": "Directory to write the dump file",
      "default": "/tmp/gnot-dumps"
    },
    "host": {"type": "string", "default": "127.0.0.1"},
    "port": {"type": "integer", "default": 3306}
  },
  "additionalProperties": false
}
```

### Part 2 — The `mysql_restore` Action (on alm-0)

```python
# ~/gnot/alm-0/actions/mysql_restore.py
"""
Restore a .sql.gz dump file into MySQL.
Supports creating the target database if it doesn't exist.
"""

import asyncio
import os

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    dump_file    = params["dump_file"]     # path to .sql.gz on this node
    database     = params.get("database")  # None = uses names from dump (--all-databases)
    create_db    = params.get("create_db", True)
    host         = params.get("host", "127.0.0.1")
    port         = params.get("port", 3306)
    drop_existing = params.get("drop_existing", False)

    creds    = context.get("caller_credentials", {})
    user     = creds.get("mysql_user", "root")
    password = creds.get("mysql_password", "")
    pw_arg   = f"-p{password}" if password else ""

    if not os.path.exists(dump_file):
        raise FileNotFoundError(f"Dump file not found: {dump_file}")

    conn_args = f"-h {host} -P {port} -u {user} {pw_arg}"

    # Optionally drop and recreate DB
    if database and drop_existing:
        drop_proc = await asyncio.create_subprocess_shell(
            f"mysql {conn_args} -e 'DROP DATABASE IF EXISTS `{database}`;'",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await drop_proc.communicate()

    # Create database if needed
    if database and create_db:
        create_proc = await asyncio.create_subprocess_shell(
            f"mysql {conn_args} -e 'CREATE DATABASE IF NOT EXISTS `{database}`;'",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await create_proc.communicate()
        if create_proc.returncode != 0:
            raise RuntimeError(f"CREATE DATABASE failed: {err.decode()[-200:]}")

    # Restore
    db_arg = database if database else ""
    cmd = f"gunzip -c {dump_file} | mysql {conn_args} {db_arg}"

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)

    if proc.returncode != 0:
        raise RuntimeError(f"mysql restore failed: {stderr.decode()[-400:]}")

    # Verify: count tables
    if database:
        count_proc = await asyncio.create_subprocess_shell(
            f"mysql {conn_args} {database} -e 'SHOW TABLES;' | wc -l",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await count_proc.communicate()
        table_count = max(0, int(stdout.decode().strip()) - 1)  # minus header
    else:
        table_count = None

    return {
        "restored_from": dump_file,
        "database": database or "(all from dump)",
        "table_count": table_count,
        "drop_existing": drop_existing,
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "mysql_restore",
  "description": "Restore a .sql.gz dump into MySQL. Requires mysql_user and mysql_password in caller_credentials.",
  "type": "object",
  "properties": {
    "dump_file": {"type": "string", "description": "Path to the .sql.gz file on this node"},
    "database": {"type": "string", "description": "Target database name (omit to use names from dump)"},
    "create_db": {"type": "boolean", "default": true},
    "drop_existing": {"type": "boolean", "default": false},
    "host": {"type": "string", "default": "127.0.0.1"},
    "port": {"type": "integer", "default": 3306}
  },
  "required": ["dump_file"],
  "additionalProperties": false
}
```

---

## PostgreSQL Migration

### `pg_dump_action.py` (on source node)

```python
# ~/gnot/cen-0/actions/pg_dump_action.py

import asyncio
import os
import time

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    database   = params["database"]
    output_dir = params.get("output_dir", "/tmp/gnot-dumps")
    host       = params.get("host", "127.0.0.1")
    port       = params.get("port", 5432)
    format_    = params.get("format", "custom")   # custom (compressed) or plain

    creds    = context.get("caller_credentials", {})
    user     = creds.get("pg_user", "postgres")
    password = creds.get("pg_password", "")

    os.makedirs(output_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    ext = ".dump" if format_ == "custom" else ".sql.gz"
    dump_file = os.path.join(output_dir, f"{database}_{timestamp}{ext}")

    env = os.environ.copy()
    env["PGPASSWORD"] = password

    if format_ == "custom":
        cmd = f"pg_dump -h {host} -p {port} -U {user} -Fc -f {dump_file} {database}"
    else:
        cmd = f"pg_dump -h {host} -p {port} -U {user} {database} | gzip > {dump_file}"

    proc = await asyncio.create_subprocess_shell(
        cmd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)

    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {stderr.decode()[-400:]}")

    size_mb = os.path.getsize(dump_file) / (1024 * 1024)
    return {"dump_file": dump_file, "database": database, "size_mb": round(size_mb, 2)}
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "pg_dump_action",
  "description": "Dump a PostgreSQL database. Requires pg_user and pg_password in caller_credentials.",
  "type": "object",
  "properties": {
    "database": {"type": "string"},
    "output_dir": {"type": "string", "default": "/tmp/gnot-dumps"},
    "host": {"type": "string", "default": "127.0.0.1"},
    "port": {"type": "integer", "default": 5432},
    "format": {"type": "string", "enum": ["custom", "plain"], "default": "custom"}
  },
  "required": ["database"],
  "additionalProperties": false
}
```

### `pg_restore_action.py` (on destination node)

```python
# ~/gnot/alm-0/actions/pg_restore_action.py

import asyncio
import os

ASYNC = True


async def run(params: dict, context: dict) -> dict:
    dump_file    = params["dump_file"]
    database     = params["database"]
    host         = params.get("host", "127.0.0.1")
    port         = params.get("port", 5432)
    drop_existing = params.get("drop_existing", False)

    creds    = context.get("caller_credentials", {})
    user     = creds.get("pg_user", "postgres")
    password = creds.get("pg_password", "")

    if not os.path.exists(dump_file):
        raise FileNotFoundError(f"Not found: {dump_file}")

    env = os.environ.copy()
    env["PGPASSWORD"] = password
    conn = f"-h {host} -p {port} -U {user}"

    if drop_existing:
        await asyncio.create_subprocess_shell(
            f"dropdb {conn} --if-exists {database}", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

    # Create DB
    create = await asyncio.create_subprocess_shell(
        f"createdb {conn} {database}", env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await create.communicate()  # OK if already exists

    # Restore
    is_custom = dump_file.endswith(".dump")
    if is_custom:
        cmd = f"pg_restore {conn} -d {database} -j 4 {dump_file}"
    else:
        cmd = f"gunzip -c {dump_file} | psql {conn} {database}"

    proc = await asyncio.create_subprocess_shell(
        cmd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)

    # pg_restore exits 1 for warnings — check stderr instead
    if proc.returncode > 1:
        raise RuntimeError(f"pg_restore failed: {stderr.decode()[-400:]}")

    # Verify
    count_proc = await asyncio.create_subprocess_shell(
        f"psql {conn} {database} -c '\\dt' | grep -c 'public'", env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await count_proc.communicate()
    table_count = int(stdout.decode().strip() or 0)

    return {
        "restored_from": dump_file,
        "database": database,
        "table_count": table_count,
        "warnings": stderr.decode()[-200:] if stderr else None,
    }
```

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "pg_restore_action",
  "description": "Restore a PostgreSQL .dump or .sql.gz into a database. Requires pg_user and pg_password in caller_credentials.",
  "type": "object",
  "properties": {
    "dump_file": {"type": "string"},
    "database": {"type": "string"},
    "host": {"type": "string", "default": "127.0.0.1"},
    "port": {"type": "integer", "default": 5432},
    "drop_existing": {"type": "boolean", "default": false}
  },
  "required": ["dump_file", "database"],
  "additionalProperties": false
}
```

---

## Full Migration Workflow — Single Prompt

Deploy actions to both nodes, then:

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Migrate the MySQL database '\''myapp'\'' from cen-0 to alm-0. Steps: 1) Dump it on cen-0 using mysql_dump, 2) Upload the .sql.gz to the gateway staging area from cen-0, 3) Download it on alm-0, 4) Restore it with mysql_restore on alm-0, 5) Verify the table count matches. Report row counts for the main tables before and after.",
    "session_id": "db-migration",
    "caller_credentials": {
      "mysql_user": "root",
      "mysql_password": "your-db-password"
    }
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['response'])"
```

The LLM executes the full pipeline:

```
Step 1: mysql_dump on cen-0       → /tmp/gnot-dumps/myapp_20260307_153000.sql.gz (24MB)
Step 2: execute_command on cen-0  → curl upload → file_id: "abc123"
Step 3: execute_command on alm-0  → curl download to /tmp/gnot-dumps/
Step 4: mysql_restore on alm-0    → restored, 18 tables
Step 5: execute_command on both   → SELECT COUNT(*) verification ✅
```

---

## Live Migration (Minimal Downtime)

For production migrations requiring minimal downtime:

```
"Perform a low-downtime migration of MySQL 'myapp' from cen-0 to alm-0:
1. Take a full dump of myapp on cen-0 (it can stay live during dump)
2. Transfer and restore it on alm-0
3. On cen-0, note the binary log position: SHOW MASTER STATUS
4. On cen-0, get all changes since the dump started: SHOW BINLOG EVENTS
5. Apply those changes on alm-0
6. Report: 'Ready to cut over — row count matches: X rows on cen-0, X rows on alm-0'
Note: Don't actually cut over — just report readiness."
```

---

## Validation Queries

The LLM will automatically verify the migration. You can also ask explicitly:

```bash
curl -s -X POST http://localhost:8080/intent \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "For database myapp: on both cen-0 and alm-0, run SELECT COUNT(*) for every table and compare the results. Show me a table with columns: Table | Rows on cen-0 | Rows on alm-0 | Match?",
    "session_id": "db-verify",
    "caller_credentials": {
      "mysql_user": "root",
      "mysql_password": "your-db-password"
    }
  }'
```

---

## Summary

You can now:
- ✅ Dump MySQL/PostgreSQL databases on any node in the mesh
- ✅ Transfer dumps across NAT-isolated networks via gateway staging
- ✅ Restore on destination with automatic verification
- ✅ Run full migrations from a single natural-language prompt

**Next:** [Guide 21 — Notification Actions (Telegram, Discord, SMS, WhatsApp)](../21-notification-actions/README.md)

---

*Part of the [GNOT Examples](../README.md) series.*
