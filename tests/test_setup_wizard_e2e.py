"""配置向导（momentum-agent init）的端到端验证。

设计稿 docs/superpowers/specs/2026-07-11-setup-wizard-design.md 承诺「一条命令完成
DB/AI/安全/服务配置」并写出 .env 与 momentum.config.json；此前没有用例真的把 CLI 跑一遍，
也没有断言过写出来的配置真的可用、弱口令真的被换掉。

安全前提：向导把 .env 与 momentum.config.json 写在 Path.cwd() 下，所以这里一律用临时目录做 cwd，
绝不触碰仓库或用户真实的 .env。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"


def clean_env(home: Path) -> dict:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": str(SOURCE_ROOT),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "LC_CTYPE": "C.UTF-8",
    }
    for name in ("TEMP", "TMP", "TMPDIR", "SystemRoot", "windir", "ComSpec"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def run_init(tmp_path: Path, *extra: str, timeout: int = 180):
    command = [sys.executable, "-m", "momentum_agent", "init", "--non-interactive", *extra]
    return subprocess.run(
        command,
        cwd=str(tmp_path),
        env=clean_env(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_non_interactive_init_writes_env_and_web_config(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'wizard.sqlite3'}"
    result = run_init(tmp_path, "--db", database_url)
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")

    env_file = tmp_path / ".env"
    config_file = tmp_path / "momentum.config.json"
    assert env_file.is_file(), "向导必须写出 .env"
    assert config_file.is_file(), "向导必须写出 momentum.config.json"

    env_text = env_file.read_text(encoding="utf-8")
    assert f"MOMENTUM_DATABASE_URL={database_url}" in env_text, env_text
    assert "MOMENTUM_LOG_LEVEL=" in env_text and "MOMENTUM_LOG_DIR=" in env_text, env_text

    config = json.loads(config_file.read_text(encoding="utf-8"))
    assert isinstance(config.get("web", {}).get("port"), int), config
    assert config["web"]["host"] in ("127.0.0.1", "localhost", "::1"), config
    # 非交互模式不启用 MCP SSE，因此不该凭空写出 mcp 段（启用才写）
    assert "mcp" not in config, config


def test_init_rotates_the_weak_default_password_and_prints_the_new_one(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'rotate.sqlite3'}"
    result = run_init(tmp_path, "--db", database_url)
    assert result.returncode == 0, result.stdout + result.stderr
    output = (result.stdout or "") + (result.stderr or "")

    assert "已自动改为随机口令" in output, "弱口令必须被替换并明确告知：" + output[-600:]
    match = re.search(r"已自动改为随机口令：([^\s]+)", output)
    assert match, output[-600:]
    new_password = match.group(1).strip()
    assert len(new_password) >= 8

    from momentum_agent.storage import create_task_store

    store = create_task_store(database_url)
    assert store.login_user("default", "momentum") is None, "弱口令必须不再可用"
    assert store.login_user("default", new_password) is not None, "打印出来的新口令必须能登录"


def test_written_database_url_is_usable_and_gets_default_preferences(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'usable.sqlite3'}"
    result = run_init(tmp_path, "--db", database_url)
    assert result.returncode == 0, result.stdout + result.stderr

    from momentum_agent.storage import create_task_store

    store = create_task_store(database_url)
    task = store.create_task("向导生成配置后的第一个任务")
    assert store._get_task(task.id).title == "向导生成配置后的第一个任务"

    memory = store.get_all_memory(user_id="default")
    assert memory.get("provider") == "none", memory
    assert memory.get("daily_capacity_minutes") == "240", memory
    assert memory.get("user_location") == "北京", memory


def test_rerunning_init_keeps_existing_values(tmp_path):
    first_url = f"sqlite:///{tmp_path / 'first.sqlite3'}"
    assert run_init(tmp_path, "--db", first_url).returncode == 0
    before = (tmp_path / ".env").read_text(encoding="utf-8")
    assert first_url in before

    result = run_init(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    after = (tmp_path / ".env").read_text(encoding="utf-8")
    assert first_url in after, "重复运行不得丢掉已有配置：" + after
    assert after.count("MOMENTUM_DATABASE_URL=") == 1, "不得重复追加同一键：" + after
    # 第二次运行时弱口令已被换掉，不该再次改动口令
    assert "已自动改为随机口令" not in ((result.stdout or "") + (result.stderr or ""))


def test_init_does_not_touch_a_parent_env(tmp_path):
    """向导只应写 cwd；父目录里已有的 .env 必须原样不动。"""
    parent_env = tmp_path / ".env"
    parent_env.write_text("MOMENTUM_API_KEY=keep-me\n", encoding="utf-8")
    workdir = tmp_path / "inner"
    workdir.mkdir()

    result = run_init(workdir, "--db", f"sqlite:///{workdir / 'inner.sqlite3'}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert parent_env.read_text(encoding="utf-8") == "MOMENTUM_API_KEY=keep-me\n"
    assert (workdir / ".env").is_file()


def test_skip_db_check_still_writes_config(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'skipped.sqlite3'}"
    result = run_init(tmp_path, "--db", database_url, "--skip-db-check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / ".env").is_file()
    assert (tmp_path / "momentum.config.json").is_file()

