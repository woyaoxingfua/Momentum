"""CLI 命令的行为验证（进程内调用 cli.main，避免子进程）。

CLI 是本地用户最主要的入口，但此前只有零散覆盖；这里把常用命令跑一遍并断言输出与落库结果。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from momentum_agent import cli as cli_module
from momentum_agent import config as config_module
from momentum_agent.storage import SQLiteTaskStore

PROVIDER_VARS = (
    "MOMENTUM_API_KEY", "OPENAI_API_KEY", "MOMENTUM_BASE_URL", "OPENAI_BASE_URL",
    "MOMENTUM_MODEL", "OPENAI_MODEL", "MOMENTUM_PROVIDER", "MOMENTUM_TEST_MYSQL_URL",
)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """隔离 DB、工作目录、provider 与日志 handler。

    cli.main() 会调用 init_from_env() 往 root logger 挂 StreamHandler；
    若不回收，用例结束后 pytest 关掉捕获流，后续日志就会往已关闭的文件写，
    刷出一堆 “I/O operation on closed file” 噪音。
    """
    import logging

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: None)
    for name in PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)

    root = logging.getLogger()
    handlers_before = list(root.handlers)
    database_path = tmp_path / "cli.sqlite3"
    store = SQLiteTaskStore(database_path)
    try:
        yield {"url": f"sqlite:///{database_path}", "store": store, "path": database_path}
    finally:
        for handler in list(root.handlers):
            if handler not in handlers_before:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass


def run_cli(monkeypatch, capsys, database_url, *args):
    monkeypatch.setattr(sys, "argv", ["momentum-agent", "--db", database_url, *args])
    cli_module.main()
    return capsys.readouterr().out


def test_add_then_list_round_trip(monkeypatch, capsys, cli_env):
    output = run_cli(monkeypatch, capsys, cli_env["url"], "add", "明天下午三点交周报")
    assert output.strip()
    titles = [task.title for task in cli_env["store"].list_tasks(status=None)]
    assert any("周报" in title for title in titles), titles

    listing = run_cli(monkeypatch, capsys, cli_env["url"], "list")
    assert "周报" in listing, listing


def test_list_reports_when_empty(monkeypatch, capsys, cli_env):
    assert "没有任务" in run_cli(monkeypatch, capsys, cli_env["url"], "list")


def test_done_is_idempotent_and_reports_status(monkeypatch, capsys, cli_env):
    task = cli_env["store"].create_task("CLI 完成测试")
    first = run_cli(monkeypatch, capsys, cli_env["url"], "done", str(task.id))
    assert f"#{task.id}" in first, first
    second = run_cli(monkeypatch, capsys, cli_env["url"], "done", str(task.id))
    assert "已处于完成状态" in second, second
    assert cli_env["store"]._get_task(task.id).status.value == "done"


def test_done_with_unknown_id_is_reported(monkeypatch, capsys, cli_env):
    output = run_cli(monkeypatch, capsys, cli_env["url"], "done", "424242")
    assert "没有找到" in output, output


def test_status_transition_commands(monkeypatch, capsys, cli_env):
    task = cli_env["store"].create_task("CLI 状态流转")
    assert run_cli(monkeypatch, capsys, cli_env["url"], "start", str(task.id)).strip()
    assert cli_env["store"]._get_task(task.id).status.value == "doing"
    assert run_cli(monkeypatch, capsys, cli_env["url"], "reopen", str(task.id)).strip()
    assert cli_env["store"]._get_task(task.id).status.value in ("todo", "doing")
    assert run_cli(monkeypatch, capsys, cli_env["url"], "drop", str(task.id)).strip()
    assert cli_env["store"]._get_task(task.id).status.value == "dropped"


def test_postpone_pushes_the_due_date(monkeypatch, capsys, cli_env):
    task = cli_env["store"].create_task("CLI 顺延", due_at=None)
    cli_env["store"].update_task(task.id, due_at=datetime(2026, 10, 9, 9, 0, tzinfo=timezone(timedelta(hours=8))))
    assert run_cli(monkeypatch, capsys, cli_env["url"], "postpone", str(task.id), "--days", "3").strip()
    updated = cli_env["store"]._get_task(task.id)
    assert updated.due_at is not None and updated.due_at.day == 12, updated.due_at


def test_search_reports_hits_and_misses(monkeypatch, capsys, cli_env):
    cli_env["store"].create_task("独一无二的关键词任务")
    hit = run_cli(monkeypatch, capsys, cli_env["url"], "search", "独一无二")
    assert "独一无二" in hit, hit
    miss = run_cli(monkeypatch, capsys, cli_env["url"], "search", "根本不存在的词")
    assert "没有匹配的任务" in miss, miss


def test_config_set_and_show(monkeypatch, capsys, cli_env):
    set_output = run_cli(monkeypatch, capsys, cli_env["url"], "config", "set", "daily_capacity_minutes", "300")
    assert set_output.strip()
    show_output = run_cli(monkeypatch, capsys, cli_env["url"], "config", "show")
    assert "300" in show_output, show_output


def test_advise_review_and_provider(monkeypatch, capsys, cli_env):
    cli_env["store"].create_task("给建议用的任务")
    assert run_cli(monkeypatch, capsys, cli_env["url"], "advise").strip()
    assert run_cli(monkeypatch, capsys, cli_env["url"], "review").strip()
    assert run_cli(monkeypatch, capsys, cli_env["url"], "provider").strip()


def test_export_and_import_round_trip(monkeypatch, capsys, cli_env, tmp_path):
    cli_env["store"].create_task("导出到 JSON 的任务")
    exported = run_cli(monkeypatch, capsys, cli_env["url"], "export")
    payload = json.loads(exported)
    assert payload["tasks"], payload

    other_db = tmp_path / "other.sqlite3"
    file_path = tmp_path / "backup.json"
    file_path.write_text(exported, encoding="utf-8")
    output = run_cli(monkeypatch, capsys, f"sqlite:///{other_db}", "import", str(file_path))
    assert "已导入" in output, output
    titles = [task.title for task in SQLiteTaskStore(other_db).list_tasks(status=None)]
    assert any("导出到 JSON" in title for title in titles), titles


def test_chat_falls_back_to_local_mode(monkeypatch, capsys, cli_env):
    assert config_module.load_provider_config({}).is_configured is False
    output = run_cli(monkeypatch, capsys, cli_env["url"], "chat", "整理一下下周的发布清单")
    assert output.strip()


def test_plan_creates_subtasks(monkeypatch, capsys, cli_env):
    output = run_cli(monkeypatch, capsys, cli_env["url"], "plan", "发布一个新版本")
    assert output.strip()
    assert cli_env["store"].list_tasks(status=None), "计划应产生任务"



def test_cli_lists_and_approves_pending_operations(monkeypatch, capsys, cli_env):
    """破坏性操作被门禁拦下后，必须能用 CLI 看到并批准。"""
    import json as _json

    from momentum_agent import approvals

    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    task = cli_env["store"].create_task("CLI 待确认任务")
    cli_env["store"].set_memory(
        approvals.PENDING_MEMORY_KEY,
        _json.dumps([{"id": "cli001", "tool": "drop_task", "arguments": {"task_id": task.id},
                      "summary": f"放弃任务 #{task.id}", "created_at": "2026-10-08T00:00:00+00:00"}]),
        user_id="default",
    )

    listing = run_cli(monkeypatch, capsys, cli_env["url"], "approvals")
    assert "cli001" in listing, listing
    assert "放弃任务" in listing, listing

    approved = run_cli(monkeypatch, capsys, cli_env["url"], "approve", "cli001")
    assert "已放弃" in approved, approved
    assert cli_env["store"]._get_task(task.id).status.value == "dropped"


def test_cli_rejects_a_pending_operation(monkeypatch, capsys, cli_env):
    import json as _json

    from momentum_agent import approvals

    task = cli_env["store"].create_task("CLI 取消任务")
    cli_env["store"].set_memory(
        approvals.PENDING_MEMORY_KEY,
        _json.dumps([{"id": "cli002", "tool": "drop_task", "arguments": {"task_id": task.id},
                      "summary": "放弃任务", "created_at": "2026-10-08T00:00:00+00:00"}]),
        user_id="default",
    )
    rejected = run_cli(monkeypatch, capsys, cli_env["url"], "reject", "cli002")
    assert "已取消" in rejected, rejected
    assert cli_env["store"]._get_task(task.id).status.value == "todo"
    assert run_cli(monkeypatch, capsys, cli_env["url"], "approvals").find("cli002") == -1


def test_cli_history_shows_persisted_turns(monkeypatch, capsys, cli_env):
    """对话历史落库后，CLI 应能直接看到。"""
    from momentum_agent import agent_app

    agent_app._conversation_history.clear()
    agent_app._save_history("default", [
        {"role": "user", "content": "CLI 历史提问"},
        {"role": "assistant", "content": "CLI 历史回答"},
    ], cli_env["store"])
    agent_app._conversation_history.clear()

    output = run_cli(monkeypatch, capsys, cli_env["url"], "history")
    assert "CLI 历史提问" in output, output
    assert "CLI 历史回答" in output, output
    agent_app._conversation_history.clear()
