"""配置向导「交互式」各步骤的行为验证（用脚本化的假 questionary 注入）。

此前只有非交互模式有端到端用例；交互式才是用户真正敲 momentum-agent init 时走的路径，
而它占 setup_wizard.py 三分之二的语句、覆盖率为 26%。
各 step_* 都以 questionary 作为参数，正好可以注入一个按顺序作答的假实现。
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import tempfile

import pytest

from momentum_agent import config as config_module
from momentum_agent import setup_wizard
from momentum_agent.storage import SQLiteTaskStore, create_task_store


class Answer:
    def __init__(self, value):
        self._value = value

    def ask(self):
        return self._value


class Choice:
    def __init__(self, title, value=None):
        self.title = title
        self.value = title if value is None else value


class ScriptedQuestionary:
    """按脚本顺序作答；答完还问就报错，避免用例悄悄问错地方。"""

    Choice = Choice

    def __init__(self, *answers):
        self.queue = list(answers)
        self.prompts: list[tuple[str, str]] = []

    def _take(self, kind, prompt):
        self.prompts.append((kind, prompt))
        if not self.queue:
            raise AssertionError(f"没有脚本答案却继续追问：{kind}({prompt!r})；已问：{self.prompts}")
        return Answer(self.queue.pop(0))

    def text(self, prompt, **kwargs):
        return self._take("text", prompt)

    def password(self, prompt, **kwargs):
        return self._take("password", prompt)

    def select(self, prompt, **kwargs):
        return self._take("select", prompt)

    def confirm(self, prompt, **kwargs):
        return self._take("confirm", prompt)


class Console:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, *args, **kwargs):
        self.lines.append(" ".join(str(a) for a in args))


@pytest.fixture
def console():
    return Console()


@pytest.fixture
def store():
    path = pathlib.Path(tempfile.mkdtemp()) / "wizard.sqlite3"
    return SQLiteTaskStore(path)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ── 1/11 数据库 ────────────────────────────────────────────────

def test_database_sqlite_makes_an_absolute_url(console):
    fake = ScriptedQuestionary("sqlite", "data/mine.db")
    url, initialized, needs_change = setup_wizard.step_database(
        fake, console, existing_url=None, skip_db_check=True,
    )
    assert url.startswith("sqlite:///")
    assert url.endswith("data/mine.db") or url.endswith("data\\mine.db"), url
    assert pathlib.Path(url.replace("sqlite:///", "")).is_absolute()
    assert (initialized, needs_change) == (False, False)


def test_database_memory_path_is_preserved(console):
    fake = ScriptedQuestionary("sqlite", ":memory:")
    url, _initialized, _needs = setup_wizard.step_database(fake, console, existing_url=None, skip_db_check=True)
    assert url == "sqlite:///:memory:"


def test_database_existing_sqlite_url_is_passed_through(console):
    fake = ScriptedQuestionary("sqlite", "sqlite:///already/here.db")
    url, _initialized, _needs = setup_wizard.step_database(fake, console, existing_url=None, skip_db_check=True)
    assert url == "sqlite:///already/here.db"


def test_database_mysql_encodes_the_password(console):
    fake = ScriptedQuestionary("mysql", "db.example.com", "3307", "root", "p@ss:w/rd", "momentum")
    url, _initialized, _needs = setup_wizard.step_database(fake, console, existing_url=None, skip_db_check=True)
    assert url == "mysql://root:p%40ss%3Aw%2Frd@db.example.com:3307/momentum", url


def test_database_azure_uses_the_azure_scheme(console):
    fake = ScriptedQuestionary("azure", "srv.mysql.database.azure.com", "3306", "admin", "secret", "momentum")
    url, _initialized, _needs = setup_wizard.step_database(fake, console, existing_url=None, skip_db_check=True)
    assert url.startswith("azure://admin:secret@srv.mysql.database.azure.com:3306/momentum"), url


# ── 4/11 工作偏好 ──────────────────────────────────────────────

def test_preferences_collects_and_normalizes(console):
    fake = ScriptedQuestionary(True, "300", "09:30", "18:30")
    result = setup_wizard.step_preferences(fake, console, {})
    assert result == {
        "vision_enabled": "true",
        "daily_capacity_minutes": "300",
        "working_hours_start": "09:30",
        "working_hours_end": "18:30",
    }


def test_preferences_retries_on_non_numeric_capacity(console):
    fake = ScriptedQuestionary(False, "abc", "240", "09:00", "18:00")
    result = setup_wizard.step_preferences(fake, console, {})
    assert result["daily_capacity_minutes"] == "240"
    assert result["vision_enabled"] == "false"


# ── 5/11 位置 ──────────────────────────────────────────────────

def test_location_uses_existing_value_as_default(console):
    fake = ScriptedQuestionary("上海")
    assert setup_wizard.step_location(fake, console, {}) == {"user_location": "上海"}


# ── 6/11 心跳 ──────────────────────────────────────────────────

def test_heartbeat_disabled_keeps_defaults(console, store):
    fake = ScriptedQuestionary(False)
    result = setup_wizard.step_heartbeat(fake, console, store, "default", {})
    config = json.loads(result["heartbeat_config"])
    assert config["enabled"] is False
    assert config["interval_hours"] == 4


def test_heartbeat_clamps_out_of_range_values(console, store):
    fake = ScriptedQuestionary(True, "99", "-3", "99")
    result = setup_wizard.step_heartbeat(fake, console, store, "default", {})
    config = json.loads(result["heartbeat_config"])
    assert config["enabled"] is True
    assert config["start_hour"] == 23
    assert config["end_hour"] == 0
    assert config["interval_hours"] == 24


def test_heartbeat_falls_back_on_invalid_numbers(console, store):
    fake = ScriptedQuestionary(True, "abc", "xyz", "??")
    result = setup_wizard.step_heartbeat(fake, console, store, "default", {})
    config = json.loads(result["heartbeat_config"])
    assert (config["start_hour"], config["end_hour"], config["interval_hours"]) == (9, 21, 4)


# ── 7/11 Web 监听 ─────────────────────────────────────────────

def test_web_server_accepts_a_free_port(console):
    port = free_port()
    fake = ScriptedQuestionary("127.0.0.1", str(port))
    host, chosen = setup_wizard.step_web_server(fake, console, "127.0.0.1", 8765)
    assert host == "127.0.0.1"
    assert chosen == port


def test_web_server_retries_on_a_non_numeric_port(console):
    port = free_port()
    fake = ScriptedQuestionary("127.0.0.1", "eight-thousand", str(port))
    host, chosen = setup_wizard.step_web_server(fake, console, "127.0.0.1", 8765)
    assert (host, chosen) == ("127.0.0.1", port)


def test_web_server_custom_host(console):
    port = free_port()
    fake = ScriptedQuestionary("custom", "0.0.0.0", str(port))
    host, chosen = setup_wizard.step_web_server(fake, console, "127.0.0.1", 8765)
    assert (host, chosen) == ("0.0.0.0", port)


# ── 8/11 MCP ──────────────────────────────────────────────────

def test_mcp_disabled_returns_no_auth(console):
    fake = ScriptedQuestionary(False)
    enabled, host, port, api_key = setup_wizard.step_mcp(fake, console, "127.0.0.1", 8766)
    assert (enabled, host, port, api_key) == (False, "127.0.0.1", 8766, None)


def test_mcp_requires_the_two_keys_to_match(console):
    port = free_port()
    fake = ScriptedQuestionary(True, "127.0.0.1", str(port), True, "first-key", "different-key", "final-key", "final-key")
    enabled, host, port_out, api_key = setup_wizard.step_mcp(fake, console, "127.0.0.1", 8766)
    assert enabled is True
    assert api_key == "final-key", api_key
    assert port_out == port


# ── 9/11 日志 ─────────────────────────────────────────────────

def test_logging_returns_level_dir_and_rotation(console):
    fake = ScriptedQuestionary("DEBUG", "logs", "10", "3")
    result = setup_wizard.step_logging(fake, console, {})
    assert any(value == "DEBUG" for value in result.values()), result
    assert any("logs" in str(value) for value in result.values()), result


# ── 10/11 进阶（默认跳过） ────────────────────────────────────

def test_advanced_can_be_skipped(console):
    fake = ScriptedQuestionary(False)
    assert setup_wizard.step_advanced(fake, console, {}) == {}


# ── 2/11 安全（弱口令检测未命中时不应发问） ───────────────────

def test_security_is_silent_when_change_is_not_needed(console, store):
    fake = ScriptedQuestionary()
    changed, admin = setup_wizard.step_security(fake, console, store, needs_change=False)
    assert (changed, admin) == (False, None)
    assert fake.prompts == [], fake.prompts



# ── 完整交互式流程（run_wizard 编排本身） ──────────────────────

def test_full_interactive_wizard_persists_a_working_configuration(tmp_path, monkeypatch):
    """把 11 步走完：断言落盘文件、user_memory、口令轮换与不启动 serve。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: None)
    port = free_port()
    fake = ScriptedQuestionary(
        "sqlite",                      # 1/11 后端
        "tasks.db",                    # 1/11 文件路径（相对 -> 绝对）
        "newpassword1", "newpassword1",  # 2/11 新口令 x2
        False,                         # 2/11 不注册新管理员
        "none",                        # 3/11 AI 提供商：跳过
        False,                         # 4/11 关闭视觉
        "240", "09:00", "18:00",       # 4/11 偏好
        "上海",                         # 5/11 位置
        False,                         # 6/11 关闭心跳
        "127.0.0.1", str(port),        # 7/11 Web 监听
        False,                         # 8/11 不启用 MCP
        "INFO", "logs", "10", "5",     # 9/11 日志
        False,                         # 10/11 跳过进阶
        False,                         # 11/11 不启动 serve
    )
    monkeypatch.setattr(setup_wizard, "_import_questionary", lambda: fake)
    # step_preview 内部直接 import 真实 questionary（会真交互），这里替换为确认
    monkeypatch.setattr(setup_wizard, "step_preview", lambda console, result: True)

    # 不跳过 DB 检查：step_database 会因此返回 needs_password_change=True，弱口令轮换才会被执行
    result = setup_wizard.run_wizard(db_url=None, skip_db_check=False)

    assert result.database_url and result.database_url.startswith("sqlite:///")
    assert str(tmp_path) in result.database_url.replace("/", os.sep) or str(tmp_path) in result.database_url

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MOMENTUM_DATABASE_URL=" in env_text
    assert "MOMENTUM_LOG_LEVEL=INFO" in env_text, env_text

    config = json.loads((tmp_path / "momentum.config.json").read_text(encoding="utf-8"))
    assert config["web"] == {"host": "127.0.0.1", "port": port}, config
    assert "mcp" not in config, config

    store = create_task_store(result.database_url)
    user_id = "default"
    memory = store.get_all_memory(user_id=user_id)
    assert memory.get("provider") == "none", memory
    assert memory.get("user_location") == "上海", memory
    assert memory.get("daily_capacity_minutes") == "240", memory
    assert json.loads(memory["heartbeat_config"])["enabled"] is False, memory

    assert result.default_password_changed is True, "弱口令必须被轮换"
    assert store.login_user("default", "momentum") is None
    assert store.login_user("default", "newpassword1") is not None
    assert result.started_server is False
