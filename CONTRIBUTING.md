# Contributing to GNOT

Thank you for your interest in contributing to GNOT! This document explains how to get involved.

---

## Ways to Contribute

- **Bug reports** — open an Issue with steps to reproduce
- **Feature requests** — open an Issue describing the use case
- **Code contributions** — fix bugs, add actions, improve the runtime
- **Documentation** — improve examples, fix typos, add translations
- **Community** — share use cases, answer questions in Discussions

---

## Branching Model

```
main        — stable releases only
dev     — integration branch (PRs go here)
feature/*   — your feature or fix branch
```

**All PRs must target `dev`, not `main`.**

---

## Getting Started

```bash
# 1. Fork the repo on GitHub, then clone your fork
git clone https://github.com/<your-username>/gnot.git
cd gnot

# 2. Install dependencies
pip install -r src/requirements.txt

# 3. Create your feature branch from dev
git checkout dev
git checkout -b feature/my-feature

# 4. Make your changes, then run tests
cd src && python -m pytest tests/ -v

# 5. Push and open a PR targeting dev
git push origin feature/my-feature
```

---

## Writing a Custom Action (Easiest Contribution)

Drop two files into `src/seed/actions/`:

```python
# src/seed/actions/my_action.py
def run(params: dict, context: dict) -> dict:
    return {"result": "hello from my action"}
```

```json
// src/seed/actions/my_action.schema.json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "my_action",
  "description": "Describe what your action does.",
  "type": "object",
  "properties": {
    "input": { "type": "string", "description": "Example param" }
  },
  "required": ["input"],
  "additionalProperties": false
}
```

See existing actions in `src/seed/actions/` for reference.

---

## PR Guidelines

- Keep PRs focused — one feature or fix per PR
- Write or update tests for any changed behavior
- Follow existing code style (no external formatter required)
- Add a clear description of *what* and *why* in the PR body
- Reference any related Issues (e.g. `Closes #42`)

---

## Reporting Bugs

Please include:
- GNOT version / commit hash
- Python version and OS
- Minimal steps to reproduce
- Actual vs expected behavior
- Relevant logs or error messages

---

## Code of Conduct

Be respectful and constructive. We welcome contributors of all backgrounds and experience levels.

---

## Questions?

Open a [GitHub Discussion](https://github.com/gnot-io/gnot/discussions) or reach out via Issues.
