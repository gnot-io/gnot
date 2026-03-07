# AI-Orchestrated Self-Bootstrapping Execution Mesh v5.0

A minimal-kernel distributed execution system that starts from a single **Seed Node** and can self-expand into an infinite mesh of specialized worker nodes — all orchestrated by an AI planner.

## Architecture

```
User → Cloud LLM (Planner) → Seed Node (node-0) → [self-expanding mesh]
```

The Seed Node exposes just three primitives (`write_file`, `read_file`, `execute_command`) from which the AI planner can bootstrap any number of new nodes with arbitrary capabilities.

## Project Structure

```
mesh/
├── node_runtime.py           # Entry point
├── runtime/
│   ├── models.py             # Pydantic request/response models
│   ├── config.py             # YAML config loader
│   ├── server.py             # FastAPI HTTP server
│   ├── router.py             # Routing + proxy logic
│   ├── resolver.py           # Node address resolution (with TTL cache)
│   ├── action_loader.py      # Plugin action discovery
│   ├── action_executor.py    # Sync/async action dispatch
│   └── job_manager.py        # In-memory async job tracking
├── seed/actions/             # Seed Node's 3 built-in actions
│   ├── write_file.py
│   ├── read_file.py
│   └── execute_command.py
├── node-0/                   # Seed Node config
│   ├── node.yaml
│   └── skills.md
├── tests/                    # pytest suite
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Quick Start (Local)

```bash
pip install -r requirements.txt
python node_runtime.py --config node-0/node.yaml
```

## Quick Start (Docker)

```bash
docker compose up --build
```

## API Endpoints

### POST /action — Execute an action

```bash
curl -X POST http://localhost:8080/action \
  -H "Content-Type: application/json" \
  -d '{
    "target_node_id": "node-0",
    "task_id": "task-001",
    "trace": {"hop_count": 0, "route_path": []},
    "payload": {
      "action": "write_file",
      "params": {"path": "/tmp/test.txt", "content": "Hello Mesh!"}
    }
  }'
```

### GET /result/{job_id} — Poll async job status

```bash
curl http://localhost:8080/result/node0-job-abc12345
```

### GET /resolve/{node_id} — Resolve node address

```bash
curl http://localhost:8080/resolve/node-0
```

### GET /skills — Node capabilities (Markdown)

```bash
curl http://localhost:8080/skills
```

### GET /health — Node health check

```bash
curl http://localhost:8080/health
```

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```
