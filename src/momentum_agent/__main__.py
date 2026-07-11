"""
Momentum Task Agent - Packagable entry point
This file is used for PyInstaller packaging to avoid relative import issues.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add the src directory to path to ensure imports work correctly
if __name__ == "__main__" and __package__ is None:
    file_path = Path(__file__).resolve()
    src_dir = file_path.parent.parent  # src/
    sys.path.insert(0, str(src_dir))

# Now use absolute imports
from momentum_agent.agent_app import (
    create_plan_from_text,
    create_task_from_text,
    drop_task_cmd,
    edit_task_from_params,
    get_user_config_cmd,
    local_advice,
    local_review,
    postpone_task_cmd,
    provider_status,
    reopen_task_cmd,
    run_agent_message,
    set_user_config_cmd,
    start_task_cmd,
)
from momentum_agent.config import DEFAULT_USER_ID, get_current_user
from momentum_agent.logger import get_logger, init_from_env, setup_logging
from momentum_agent.models import TaskStatus
from momentum_agent.storage import TaskStore, create_task_store
from momentum_agent.web import run_server

log = get_logger("cli")


def main() -> None:
    parser = argparse.ArgumentParser(prog="momentum-agent")
    parser.add_argument("--db", "--database-url", dest="database_url",
                        default=os.environ.get("MOMENTUM_DATABASE_URL", ".momentum/tasks.db"),
                        help="Database URL. Supported formats:\n"
                             "  sqlite:///path/to/db.db  (本地 SQLite，默认)\n"
                             "  mysql://user:pass@host:port/db  (MySQL)\n"
                             "  azure://user:pass@host:port/db  (Azure MySQL，自动启用SSL)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    parser.add_argument("--log-file", type=Path, default=None, help="Override log file path (default: logs/momentum-YYYY-MM-DD.log).")
    parser.add_argument("--log-dir", type=Path, default=None, help="Override log directory (default: logs/).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Create a task from natural language.")
    add_parser.add_argument("text")

    plan_parser = subparsers.add_parser("plan", help="Create a task plan with subtasks.")
    plan_parser.add_argument("text")

    list_parser = subparsers.add_parser("list", help="List tasks.")
    list_parser.add_argument("--status", choices=[status.value for status in TaskStatus], default="todo")

    done_parser = subparsers.add_parser("done", help="Mark a task as done.")
    done_parser.add_argument("task_id", type=int)

    edit_parser = subparsers.add_parser("edit", help="Edit a task's fields.")
    edit_parser.add_argument("task_id", type=int)
    edit_parser.add_argument("--title")
    edit_parser.add_argument("--due", help="ISO date, e.g. 2026-06-01 or 2026-06-01T15:00")
    edit_parser.add_argument("--priority", choices=["low", "medium", "high"])
    edit_parser.add_argument("--estimate", type=int, dest="estimated_minutes")
    edit_parser.add_argument("--notes")
    edit_parser.add_argument("--tags", nargs="+", help="Tags for the task, e.g. --tags work urgent personal")

    postpone_parser = subparsers.add_parser("postpone", help="Postpone a task by N days.")
    postpone_parser.add_argument("task_id", type=int)
    postpone_parser.add_argument("--days", type=int, default=3, help="Days to push forward (default: 3)")

    subparsers.add_parser("drop", help="Drop a task.").add_argument("task_id", type=int)

    subparsers.add_parser("start", help="Mark a task as in progress.").add_argument("task_id", type=int)

    subparsers.add_parser("reopen", help="Reopen a done/dropped task.").add_argument("task_id", type=int)

    subparsers.add_parser("advise", help="Show the best next action for today.")
    subparsers.add_parser("review", help="Show a concise daily task review.")
    subparsers.add_parser("provider", help="Show configured agent provider.")

    config_parser = subparsers.add_parser("config", help="View or set user preferences.")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_set = config_sub.add_parser("set", help="Set a config value.")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_sub.add_parser("show", help="Show all config values.")

    search_parser = subparsers.add_parser("search", help="Search tasks by keyword.")
    search_parser.add_argument("query")
    search_parser.add_argument("--status", choices=[s.value for s in TaskStatus], default=None)

    export_parser = subparsers.add_parser("export", help="Export user data as JSON.")

    import_parser = subparsers.add_parser("import", help="Import tasks from a JSON file.")
    import_parser.add_argument("file", type=Path)

    chat_parser = subparsers.add_parser("chat", help="Run a message through the Agents SDK path.")
    chat_parser.add_argument("message")

    serve_parser = subparsers.add_parser("serve", help="Start the local web app.")
    serve_parser.add_argument("--host", default=None, help="监听地址（默认走 momentum.config.json 或 127.0.0.1）。")
    serve_parser.add_argument("--port", type=int, default=None, help="监听端口（默认走 momentum.config.json 或 8765）。")

    mcp_parser = subparsers.add_parser("mcp", help="Start the MCP server for external AI agents.")
    mcp_parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio",
                            help="Transport: stdio (local, default) or sse (HTTP).")
    mcp_parser.add_argument("--host", default=None, help="SSE 模式监听地址（默认走 momentum.config.json 或 127.0.0.1）。")
    mcp_parser.add_argument("--port", type=int, default=None, help="SSE 模式监听端口（默认走 momentum.config.json 或 8766）。")
    mcp_parser.add_argument("--user", default=None, help="目标用户（默认 MOMENTUM_USER 或 default）。")

    init_parser = subparsers.add_parser("init", help="配置向导：交互式完成全部配置。")
    init_parser.add_argument("--db", default=None, help="预设的数据库 URL（跳过 DB 选择步）。")
    init_parser.add_argument("--non-interactive", action="store_true", help="非交互模式：用默认值+已有配置。")
    init_parser.add_argument("--skip-db-check", action="store_true", help="跳过 DB 连接测试。")

    args = parser.parse_args()

    # logging
    if args.verbose:
        os.environ["MOMENTUM_LOG_LEVEL"] = "DEBUG"
    init_from_env()

    # CLI 显式指定日志文件/目录时，追加文件 handler
    if args.log_file or args.log_dir:
        setup_logging(log_file=args.log_file, log_dir=args.log_dir)

    database_url = args.database_url
    # init 命令自己管理 store 创建，提前创建会在 DB 未配置时崩溃
    if args.command == "init":
        from momentum_agent.setup_wizard import run_wizard

        run_wizard(
            db_url=args.db,
            non_interactive=args.non_interactive,
            skip_db_check=args.skip_db_check,
        )
        return

    store = create_task_store(database_url)
    user_id = get_current_user()

    log.info("command=%s user=%r", args.command, user_id)

    if args.command == "add":
        print(create_task_from_text(store, args.text, user_id=user_id))
    elif args.command == "plan":
        print(create_plan_from_text(store, args.text, user_id=user_id))
    elif args.command == "list":
        print_tasks(store, TaskStatus(args.status), user_id=user_id)
    elif args.command == "done":
        task = store.update_status(args.task_id, TaskStatus.DONE)
        print(f"已完成任务 #{task.id}：{task.title}" if task else "没有找到这个任务。")
    elif args.command == "edit":
        print(edit_task_from_params(
            store, args.task_id,
            title=args.title, due_at=args.due, priority=args.priority,
            estimated_minutes=args.estimated_minutes, notes=args.notes,
            tags=args.tags, user_id=user_id,
        ))
    elif args.command == "postpone":
        print(postpone_task_cmd(store, args.task_id, args.days, user_id=user_id))
    elif args.command == "drop":
        print(drop_task_cmd(store, args.task_id, user_id=user_id))
    elif args.command == "start":
        print(start_task_cmd(store, args.task_id, user_id=user_id))
    elif args.command == "reopen":
        print(reopen_task_cmd(store, args.task_id, user_id=user_id))
    elif args.command == "advise":
        print(local_advice(store, user_id=user_id))
    elif args.command == "review":
        print(local_review(store, user_id=user_id))
    elif args.command == "provider":
        print(provider_status())
    elif args.command == "config":
        if args.config_command == "set":
            print(set_user_config_cmd(store, args.key, args.value, user_id=user_id))
        else:
            print(get_user_config_cmd(store, user_id=user_id))
    elif args.command == "search":
        status = TaskStatus(args.status) if args.status else None
        results = store.search_tasks(args.query, user_id=user_id, status=status)
        if not results:
            print("没有匹配的任务。")
        else:
            for task in results:
                st = f"[{task.status.value}]" if status is None else ""
                due = task.due_at.strftime("%Y-%m-%d %H:%M") if task.due_at else "无截止"
                print(f"#{task.id} {st} [{task.priority.value}] {task.title} | {due}")
    elif args.command == "export":
        import json as _json
        data = store.export_user_data(user_id=user_id)
        print(_json.dumps(data, ensure_ascii=False, indent=2))
    elif args.command == "import":
        import json as _json
        text = args.file.read_text(encoding="utf-8")
        data = _json.loads(text)
        n = store.import_user_data(data, user_id=user_id)
        print(f"已导入 {n} 个任务。")
    elif args.command == "chat":
        print(asyncio.run(run_agent_message(database_url, args.message, user_id=user_id)))
    elif args.command == "serve":
        # 回退链：--host/--port > env > momentum.config.json > 默认
        from momentum_agent.wizard_config import get_web_host, get_web_port

        web_host = get_web_host(args.host)
        web_port = get_web_port(args.port)
        log.info("starting server %s:%s", web_host, web_port)
        run_server(database_url, host=web_host, port=web_port)
    elif args.command == "mcp":
        from momentum_agent.mcp_server import run_mcp_server
        from momentum_agent.wizard_config import get_mcp_host, get_mcp_port

        mcp_user = args.user or user_id
        mcp_host = get_mcp_host(args.host)
        mcp_port = get_mcp_port(args.port)
        log.info("starting MCP server transport=%s user=%r", args.transport, mcp_user)
        run_mcp_server(
            database_url,
            transport=args.transport,
            user_id=mcp_user,
            host=mcp_host,
            port=mcp_port,
        )


def print_tasks(store: TaskStore, status: TaskStatus, *, user_id: str = DEFAULT_USER_ID) -> None:
    tasks = store.list_tasks(status, user_id=user_id)
    if not tasks:
        print("没有任务。")
        return

    for task in tasks:
        due = task.due_at.strftime("%Y-%m-%d %H:%M") if task.due_at else "无截止"
        estimate = f"{task.estimated_minutes} 分钟" if task.estimated_minutes else "未估时"
        recurrence = {"daily": " 🔁每天", "weekly": " 🔁每周", "monthly": " 🔁每月"}.get(task.recurrence or "", "")
        print(f"#{task.id} [{task.priority.value}] {task.title} | {due} | {estimate}{recurrence}")


if __name__ == "__main__":
    main()
