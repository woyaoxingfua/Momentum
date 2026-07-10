# Momentum

> Not just another todo list — an AI task system that helps you actually finish things.

[简体中文](./README.md)

Momentum is a **local-first** task management app that combines practical execution tooling with AI assistance:
- Capture tasks in natural language
- Break goals into executable subtasks
- Track execution quality with behavior insights
- Use both Web UI and CLI in one workflow

---

## Why Momentum

- **Fast start**: SQLite + local server works out of the box
- **Reliable fallback**: if AI is unavailable, local parsing/planning still works
- **Insight-driven**: completion trends, estimate bias, overdue analysis
- **Flexible provider model**: OpenAI-compatible endpoints and Ollama supported

---

## Core Capabilities

### Task & Planning
- Natural-language task creation (deadline/priority/recurrence parsing)
- One-shot plan generation for large goals (AI + local fallback)
- Task lifecycle: Todo / Doing / Done / Dropped / Reopen
- Edit/search/postpone/tag/import/export operations
- Task relation support: dependency, blocking, hierarchy, sequence

### Agent Experience
- Orchestrated main agent + specialist agents (insight / weather / focus)
- Streaming responses with tool execution flow
- Optional image-based task extraction (when vision is enabled)
- User memory and context-aware interactions

### Behavioral Insights
- Completion stats and trend tracking
- Time estimation deviation analysis
- Due-today / due-this-week / overdue / in-progress views
- Next-best-action recommendation

---

## Quick Start

### Requirements
- Python 3.11+

### Install & Run

```bash
git clone https://github.com/woyaoxingfua/Momentum.git
cd Momentum

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

momentum-agent serve
# open http://127.0.0.1:8765
```

Default account: `default` / `momentum` (please change the password after login).

---

## AI Configuration

Momentum supports any OpenAI-compatible API.

```bash
export MOMENTUM_API_KEY="sk-..."
export MOMENTUM_BASE_URL="https://api.deepseek.com/v1"
export MOMENTUM_MODEL="deepseek-chat"
```

> Use `MOMENTUM_BASE_URL` (not `MOMENTUM_API_BASE`).

### Ollama

```bash
export MOMENTUM_PROVIDER="ollama"
export MOMENTUM_BASE_URL="http://localhost:11434"
export MOMENTUM_MODEL="llama3.2"
```

Momentum automatically normalizes the `/v1` endpoint for Ollama-style OpenAI compatibility.

---

## CLI Examples

```bash
# create and plan
momentum-agent add "Pay utility bill tomorrow 3pm"
momentum-agent plan "Prepare PM interview next week"

# list and state
momentum-agent list --status todo
momentum-agent start 1
momentum-agent done 1
momentum-agent reopen 1
momentum-agent drop 1

# organize
momentum-agent edit 1 --priority high --tags work urgent
momentum-agent postpone 1 --days 3
momentum-agent search "interview"

# recommendations and review
momentum-agent advise
momentum-agent review

# config and data
momentum-agent config show
momentum-agent config set daily_capacity_minutes 240
momentum-agent export > backup.json
momentum-agent import backup.json
```

---

## Storage

Default: SQLite at `.momentum/tasks.db`

Optional MySQL / Azure MySQL:

```bash
pip install -e ".[mysql]"
export MOMENTUM_DATABASE_URL="mysql://user@host:3306/momentum_db"
momentum-agent serve
```

Supported DSN formats:
- `sqlite:///absolute/path/to/db.db`
- `sqlite:///:memory:`
- `mysql://user@host:port/db`
- `azure://user@host:port/db`

---

## Project Layout

```text
src/momentum_agent/
├── cli.py
├── agent_app.py
├── config.py
├── context.py
├── insights.py
├── parser.py
├── planner.py
├── auth.py
├── web/
├── static/
├── storage/
└── agents/
```

---

## Testing

```bash
pytest tests -v
```

MySQL tests are skipped by default; enable with:

```bash
export MOMENTUM_TEST_MYSQL_URL="mysql://user@localhost:3306/momentum_test"
pytest tests/test_mysql_store.py -v
```

---

## Screenshots

UI screenshots are not included yet.

---

## Live Demo

https://myfirst.cc.cd

---

## License

MIT
