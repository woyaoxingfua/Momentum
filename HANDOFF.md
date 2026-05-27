# Momentum Task Agent — Handoff

**Last session**: 2026-05-27

## Quick Resume

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest                           # expect 5+ passed
momentum-agent --user default list         # see current tasks
momentum-agent serve                       # start at http://127.0.0.1:8765
```

## Current Task State

| # | Status | Title | User |
|---|--------|-------|------|
| 1 | todo | 我要在进行刷课 | default |
| 4 | todo | 老师检查任务 | default |
| 2 | done | 我要退了 | default |

## Architecture

```
CLI / Web UI
    ↓
agent_app.py  ──→  storage.py (SQLite)
    ↓                  ├── tasks (+ user_id, recurrence, parent)
OpenAI Agents SDK      ├── users
    ├── handoffs       ├── user_memory
    ├── streaming      └── task_events
    ├── guardrails
    └── function_tools
```

## What's Working

- Full task CRUD + status flow: todo → doing → done / dropped → reopen
- Status filter tabs in Web UI (待办/进行中/已完成/已放弃)
- Auto-complete parent when all child subtasks done
- Recurring tasks (daily/weekly/monthly)
- Multi-user data isolation (--user flag, env var, Web selector + dialog)
- AI chat with streaming SSE + Markdown rendering
- Multi-agent handoffs (triage → creator/planner/coach)
- Search, JSON export/import
- Structured logging (INFO default, --verbose for DEBUG)
- Regex parser fallback when API fails (DeepSeek doesn't support json_schema)

## File Map

| File | What |
|------|------|
| `storage.py` | SQLite: TaskStore with auto-migration, users, memory, search, export |
| `agent_app.py` | Core: handoff agents, streaming, guardrails, all business functions |
| `cli.py` | CLI: add/plan/list/edit/postpone/drop/done/start/reopen/search/export/import/users/config/advise/review/chat/serve |
| `web.py` | HTTP server: REST API + SSE streaming, request logging |
| `logger.py` | Structured logging config, file+console handlers |
| `config.py` | Env loading: MOMENTUM_* > OPENAI_*, MOMENTUM_USER |
| `parser.py` | Regex NLP fallback for Chinese task text |
| `planner.py` | Template subtask generation (fallback) |
| `context.py` | Task scoring, advice, review with user prefs |
| `models.py` | Dataclasses + Pydantic models for SDK structured output |
| `static/` | ES modules: api/tasks/chat/advice/config/app.js + CSS |

## API Endpoints

GET: `/api/tasks?status=todo&user_id=default` `/api/tasks?q=搜索` `/api/advice` `/api/review` `/api/provider` `/api/config` `/api/users` `/api/export` `/js/*`
POST: `/api/tasks` `/api/plan` `/api/tasks/:id/done` `/api/tasks/:id/start` `/api/tasks/:id/reopen` `/api/tasks/:id/postpone` `/api/tasks/:id/drop` `/api/chat` `/api/chat/stream` `/api/config` `/api/users` `/api/import`
PUT: `/api/tasks/:id`

## Known Issues

1. DeepSeek doesn't support `response_format: json_schema` — AI parse/plan falls back with try/except
2. `.env` file loads before pytest monkeypatch — one config test always fails
3. Windows temp dir permission blocks 3 context tests (pytest tmp_path)

## CLI Reference

```
momentum-agent add "..."                --user alice add "..."
momentum-agent plan "..."               --user bob list --status done
momentum-agent list                     search "关键词"
momentum-agent start 1 | done 1 | reopen 2 | postpone 1 --days 5 | drop 3
momentum-agent edit 1 --title "..." --priority high --due "2026-06-01"
momentum-agent config set daily_capacity_minutes 120 | config show
momentum-agent users add bob "Bob" | users list
momentum-agent export > backup.json | import backup.json
momentum-agent advise | review | provider | chat "..." | serve -v
```

## Next Ideas

- OS notifications for due tasks
- Task notes with Markdown rendering in cards
- Dashboard/statistics view
- Real frontend framework if UI grows further
