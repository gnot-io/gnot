# Node: node-0 (Seed Node / Gateway)

## Role

Seed Node is the bootstrap kernel and gateway coordinator of the Execution Mesh.
It provides fundamental primitives and coordinates job delivery (push/pull) to
all worker nodes. The Cloud AI Planner communicates exclusively with this node.

## Capabilities

---

### Action: write_file

**Description:** Write content to a file on disk. Creates parent directories
automatically if they do not exist. Overwrites existing files. Text only.

**Input:**
- `path` (string, required): Absolute or relative file path.
- `content` (string, required): Text content to write.

**Output:**
- `success` (boolean): Always `true` on success.
- `path` (string): The resolved absolute path of the written file.

**Mode:** Synchronous

---

### Action: read_file

**Description:** Read the full content of a text file on disk.
Do NOT use for binary files (use read_file_b64 instead).

**Input:**
- `path` (string, required): File path to read.

**Output:**
- `content` (string): The file content (UTF-8 text).
- `size_bytes` (integer): File size in bytes.

**Mode:** Synchronous

---

### Action: read_file_b64

**Description:** Read any file — text or binary — and return its content
as a Base64-encoded string. Use this for database dumps (.sql, .sql.gz),
compressed archives, or any binary file that must be transferred between nodes.

**Input:**
- `path` (string, required): File path to read.

**Output:**
- `content_b64` (string): Base64-encoded file content.
- `size_bytes` (integer): Original file size in bytes.
- `path` (string): Resolved absolute path.

**Mode:** Synchronous

**Transfer pattern (with write_file_b64):**
```
Step 1: read_file_b64  {path: "/backup/db.sql.gz"}     → on source node
Step 2: write_file_b64 {path: "/dest/db.sql.gz",
                        content_b64: "<step1.content_b64>"}  → on dest node
```

---

### Action: write_file_b64

**Description:** Decode Base64 content and write the raw bytes to disk.
Binary-safe counterpart of read_file_b64. Use for receiving transferred files.

**Input:**
- `path` (string, required): Destination file path. Parent dirs created automatically.
- `content_b64` (string, required): Base64-encoded content (from read_file_b64).

**Output:**
- `success` (boolean): Always `true` on success.
- `path` (string): Resolved absolute path of written file.
- `size_bytes` (integer): Number of bytes written.

**Mode:** Synchronous

---

### Action: execute_command

**Description:** Execute an arbitrary shell command and return its output.
This is the most powerful primitive — it can run database tools, install packages,
start services, build containers, and create new nodes.

**Input:**
- `command` (string, required): Shell command to execute.
- `timeout_seconds` (integer, optional, default 60): Maximum execution time.

**Output:**
- `exit_code` (integer): Process exit code (0 = success).
- `stdout` (string): Standard output.
- `stderr` (string): Standard error.

**Mode:** Asynchronous — always returns a `job_id` for polling.

**Common uses:**
- Database backup:  `mysqldump -u root mydb > /tmp/backup.sql`
- Database restore: `mysql -u root mydb < /tmp/restore.sql`
- Postgres backup:  `pg_dump -U postgres mydb -f /tmp/backup.sql`
- Compress:         `gzip /tmp/backup.sql`
- Verify file:      `ls -lh /tmp/backup.sql && md5sum /tmp/backup.sql`
