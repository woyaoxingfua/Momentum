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
    """隔离 DB、工作目录与 provider，确保不碰真实数据也不写进仓库。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: None)
    for name in PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)
    database_path = tmp_path / "cli.sqlite3"
    store = SQLiteTaskStore(database_path)
    return {"url": f"sqlite:///{database_path}", "store": store, "path": database_path}


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

