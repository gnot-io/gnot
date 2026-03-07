Rất tốt 👌 đây là bước cực kỳ quan trọng để hệ thống trở thành **self-describing distributed execution fabric**.

Chúng ta sẽ cập nhật lại spec thành **v3.0 – Skill-Driven Architecture**.

Điểm mới:

* Mỗi node có thêm `skill.yaml`
* Node expose thêm endpoint đọc skill
* LLM có thể introspect capability
* Skill file định nghĩa:

  * Intent
  * Action
  * Input schema
  * Output schema

---

# AI-Orchestrated Distributed Node System

## Architecture Specification v3.0 (Skill-Aware Nodes)

---

# 1. Tổng quan kiến trúc

Hệ thống gồm:

* Cloud AI Web (LLM Brain)
* Nhiều Execution Nodes
* Mỗi node:

  * `node.yaml` (network config)
  * `skill.yaml` (capability definition)
  * HTTP API endpoints

Mọi node đều symmetric.

---

# 2. File cấu hình của Node

## 2.1 node.yaml (Network & Routing)

Ví dụ:

```yaml
node_id: node-2
listen: 0.0.0.0:8080

nodes:
  node-1: http://10.0.0.1:8080
  node-2: http://10.0.0.2:8080

default_resolver: node-1

cache_ttl_seconds: 300
max_hop: 10
```

---

## 2.2 skill.yaml (Capability Definition)

Đây là file mới.

Mục tiêu:

* Mô tả node có thể làm gì
* Định nghĩa schema input/output
* Cho phép LLM gửi đúng format

Ví dụ:

```yaml
node_id: node-2
version: 1.0

intents:
  migrate_data:
    description: "Migrate database to another node"
    input_schema:
      type: object
      properties:
        source_db:
          type: string
        target_node:
          type: string
      required: ["source_db", "target_node"]
    output_schema:
      type: object
      properties:
        status:
          type: string

actions:
  dump_database:
    description: "Dump a database to file"
    input_schema:
      type: object
      properties:
        db_name:
          type: string
      required: ["db_name"]
    output_schema:
      type: object
      properties:
        file_path:
          type: string

  check_disk:
    description: "Check disk usage"
    input_schema:
      type: object
      properties: {}
    output_schema:
      type: object
      properties:
        free_space_gb:
          type: number
```

---

# 3. Skill Endpoint

Mỗi node phải expose:

```http
GET /skills
```

Response:

```json
{
  "node_id": "node-2",
  "version": "1.0",
  "intents": {...},
  "actions": {...}
}
```

Cloud LLM sẽ:

1. Gọi `/skills` khi bắt đầu
2. Cache skill
3. Dựa vào schema để generate request đúng format

---

# 4. LLM Skill Discovery Flow

### Step 1 – Discover

Cloud gọi:

```
GET node-2/skills
```

### Step 2 – Load Schema

Cloud lưu:

```text
node-2:
  intents: ...
  actions: ...
```

### Step 3 – Structured Prompting

LLM được cung cấp schema khi reasoning.

Ví dụ prompt nội bộ:

> You can call action dump_database with input_schema: {...}

---

# 5. Endpoint Summary (Updated)

Mỗi node expose:

```text
POST /intent
POST /action
GET  /result/{job_id}
GET  /resolve/{node_id}
GET  /skills
```

---

# 6. Intent Processing (Skill-Aware)

Khi nhận `/intent`:

Node phải:

1. Check intent tồn tại trong skill.yaml
2. Validate input theo input_schema
3. Nếu invalid → reject
4. Nếu valid → tạo job_id

---

# 7. Action Processing (Skill-Aware)

Tương tự:

1. Check action tồn tại
2. Validate input_schema
3. Thực thi
4. Validate output theo output_schema trước khi trả về

---

# 8. Schema Format

Chúng ta dùng chuẩn JSON Schema (subset).

Tương thích với:

* OpenAPI
* Structured output của LLM
* Function calling style

Conceptually tương tự cách tool/function được mô tả trong:

* OpenAI function calling
* Anthropic tool use

Nhưng ở đây skill nằm tại node, không nằm trong Cloud.

---

# 9. Lợi ích của Skill System

### 9.1 Self-Describing

Node có thể thêm action mới mà không cần sửa Cloud code.

### 9.2 Versioning

Skill có version.
Cloud có thể detect mismatch.

### 9.3 Validation Safety

Node validate input trước khi thực thi.

### 9.4 LLM Determinism

LLM generate structured JSON dựa trên schema.

---

# 10. Cloud Skill Management

Cloud nên maintain cache:

```text
node_id → {
  skill_version,
  intents,
  actions
}
```

Cloud có thể:

* Refresh khi version thay đổi
* Refresh theo TTL

---

# 11. Skill Update Strategy

Khi skill.yaml thay đổi:

Node có thể:

* Reload skill
* Tăng version
* Cloud detect version change

---

# 12. Security Consideration

Skill endpoint nên:

* Public read-only
* Hoặc require authentication
* Không expose internal secret

Skill chỉ mô tả capability, không chứa credential.

---

# 13. Updated System Properties

Hệ thống bây giờ là:

* LLM-controlled distributed mesh
* Self-routing
* Async-first
* Skill-driven
* Schema-validated
* Self-describing nodes

---

# 14. Final Architecture Summary (v3.0)

Mỗi node gồm:

1. node.yaml (network & routing)
2. skill.yaml (capability & schema)
3. Job manager
4. HTTP API

Cloud:

* Orchestrator
* Task manager
* Skill-aware reasoning engine

---

# 15. Implementation Order (Updated)

1. Node HTTP server
2. node.yaml loader
3. skill.yaml loader
4. /skills endpoint
5. Schema validation engine
6. Routing
7. Job manager
8. Intent handler
9. Action handler
10. Result polling
11. Cloud skill cache logic

---

Spec v3.0 này đã đủ để:

* Build fully dynamic skill-based orchestration system
* Cho phép LLM tự khám phá và sử dụng node capability

---

Nếu anh muốn, bước tiếp theo em có thể:

* Formal hóa skill schema chuẩn hóa thành JSON Schema spec hoàn chỉnh
* Hoặc thiết kế cách Cloud inject skill vào prompt một cách tối ưu để giảm token cost
* Hoặc thiết kế permission layer per-skill

Anh muốn đi sâu phần nào tiếp theo?