"""Setup Wizard 测试。

覆盖：
- wizard_config.py：momentum.config.json 读写 + 回退链
- setup_wizard.py：.env 读写、parse_db_url、is_port_available、test_db_connection
- 非交互模式端到端
"""
from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from momentum_agent import wizard_config
from momentum_agent.setup_wizard import (
    WizardResult,
    check_db_connection,
    is_port_available,
    parse_db_url,
    read_env_file,
    write_env_file,
)


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """每个测试在临时 cwd 里跑，避免污染真实项目根。"""
    monkeypatch.chdir(tmp_path)
    # 重置 wizard_config 的 _config_path 缓存（Path.cwd() 每次调用，无需重置）
    return tmp_path


@pytest.fixture
def temp_db_url():
    """临时 SQLite DB URL。"""
    db_path = tempfile.mktemp(suffix=".db")
    yield f"sqlite:///{db_path}"
    try:
        Path(db_path).unlink(missing_ok=True)
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════
# wizard_config：momentum.config.json 读写
# ═══════════════════════════════════════════════════════════════════


class TestWizardConfigLoadSave:
    def test_load_config_file_not_exist_returns_empty_dict(self, isolated_cwd):
        assert wizard_config.load_config() == {}

    def test_load_config_corrupt_json_returns_empty_dict(self, isolated_cwd):
        Path("momentum.config.json").write_text("not json{", encoding="utf-8")
        assert wizard_config.load_config() == {}

    def test_load_config_non_dict_returns_empty_dict(self, isolated_cwd):
        Path("momentum.config.json").write_text("[1,2,3]", encoding="utf-8")
        assert wizard_config.load_config() == {}

    def test_save_and_load_roundtrip(self, isolated_cwd):
        wizard_config.save_config({"web": {"host": "0.0.0.0", "port": 9000}})
        cfg = wizard_config.load_config()
        assert cfg["web"]["host"] == "0.0.0.0"
        assert cfg["web"]["port"] == 9000

    def test_save_config_preserves_other_keys(self, isolated_cwd):
        wizard_config.save_config({"mcp": {"host": "127.0.0.1", "port": 8766}})
        wizard_config.save_config({"web": {"host": "0.0.0.0", "port": 9000}})
        cfg = wizard_config.load_config()
        assert "mcp" in cfg and "web" in cfg


class TestWizardConfigWebHost:
    def test_cli_flag_takes_precedence(self, isolated_cwd, monkeypatch):
        monkeypatch.setenv("MOMENTUM_WEB_HOST", "10.0.0.1")
        wizard_config.save_config({"web": {"host": "0.0.0.0", "port": 8765}})
        assert wizard_config.get_web_host("192.168.1.1") == "192.168.1.1"

    def test_env_takes_precedence_over_config(self, isolated_cwd, monkeypatch):
        monkeypatch.setenv("MOMENTUM_WEB_HOST", "10.0.0.1")
        wizard_config.save_config({"web": {"host": "0.0.0.0", "port": 8765}})
        assert wizard_config.get_web_host(None) == "10.0.0.1"

    def test_config_json_takes_precedence_over_default(self, isolated_cwd, monkeypatch):
        monkeypatch.delenv("MOMENTUM_WEB_HOST", raising=False)
        wizard_config.save_config({"web": {"host": "0.0.0.0", "port": 8765}})
        assert wizard_config.get_web_host(None) == "0.0.0.0"

    def test_default_when_nothing_set(self, isolated_cwd, monkeypatch):
        monkeypatch.delenv("MOMENTUM_WEB_HOST", raising=False)
        assert wizard_config.get_web_host(None) == wizard_config.DEFAULT_WEB_HOST


class TestWizardConfigWebPort:
    def test_cli_flag_takes_precedence(self, isolated_cwd, monkeypatch):
        monkeypatch.setenv("MOMENTUM_WEB_PORT", "9999")
        assert wizard_config.get_web_port(8765) == 8765

    def test_env_takes_precedence_over_config(self, isolated_cwd, monkeypatch):
        monkeypatch.setenv("MOMENTUM_WEB_PORT", "9999")
        wizard_config.save_config({"web": {"host": "127.0.0.1", "port": 8765}})
        assert wizard_config.get_web_port(None) == 9999

    def test_config_json_takes_precedence_over_default(self, isolated_cwd, monkeypatch):
        monkeypatch.delenv("MOMENTUM_WEB_PORT", raising=False)
        wizard_config.save_config({"web": {"host": "127.0.0.1", "port": 9000}})
        assert wizard_config.get_web_port(None) == 9000

    def test_default_when_nothing_set(self, isolated_cwd, monkeypatch):
        monkeypatch.delenv("MOMENTUM_WEB_PORT", raising=False)
        assert wizard_config.get_web_port(None) == wizard_config.DEFAULT_WEB_PORT

    def test_non_digit_env_falls_through(self, isolated_cwd, monkeypatch):
        monkeypatch.setenv("MOMENTUM_WEB_PORT", "not-a-number")
        assert wizard_config.get_web_port(None) == wizard_config.DEFAULT_WEB_PORT


class TestWizardConfigMcpHost:
    def test_full_fallback_chain(self, isolated_cwd, monkeypatch):
        monkeypatch.delenv("MOMENTUM_MCP_HOST", raising=False)
        # 默认
        assert wizard_config.get_mcp_host(None) == wizard_config.DEFAULT_MCP_HOST
        # config.json
        wizard_config.save_config({"mcp": {"host": "0.0.0.0", "port": 8766}})
        assert wizard_config.get_mcp_host(None) == "0.0.0.0"
        # env
        monkeypatch.setenv("MOMENTUM_MCP_HOST", "10.0.0.1")
        assert wizard_config.get_mcp_host(None) == "10.0.0.1"
        # CLI flag
        assert wizard_config.get_mcp_host("192.168.1.1") == "192.168.1.1"


class TestWizardConfigMcpPort:
    def test_full_fallback_chain(self, isolated_cwd, monkeypatch):
        monkeypatch.delenv("MOMENTUM_MCP_PORT", raising=False)
        assert wizard_config.get_mcp_port(None) == wizard_config.DEFAULT_MCP_PORT
        wizard_config.save_config({"mcp": {"host": "127.0.0.1", "port": 9001}})
        assert wizard_config.get_mcp_port(None) == 9001
        monkeypatch.setenv("MOMENTUM_MCP_PORT", "9998")
        assert wizard_config.get_mcp_port(None) == 9998
        assert wizard_config.get_mcp_port(8766) == 8766


class TestWizardConfigSetWebMcp:
    def test_set_web_config_merges(self, isolated_cwd):
        wizard_config.save_config({"existing": {"keep": "me"}})
        wizard_config.set_web_config("0.0.0.0", 9000)
        cfg = wizard_config.load_config()
        assert cfg["existing"]["keep"] == "me"
        assert cfg["web"]["host"] == "0.0.0.0"
        assert cfg["web"]["port"] == 9000

    def test_set_mcp_config_merges(self, isolated_cwd):
        wizard_config.save_config({"existing": {"keep": "me"}})
        wizard_config.set_mcp_config("0.0.0.0", 9001)
        cfg = wizard_config.load_config()
        assert cfg["existing"]["keep"] == "me"
        assert cfg["mcp"]["host"] == "0.0.0.0"
        assert cfg["mcp"]["port"] == 9001


# ═══════════════════════════════════════════════════════════════════
# .env 读写
# ═══════════════════════════════════════════════════════════════════


class TestReadEnvFile:
    def test_no_file_returns_empty(self, isolated_cwd):
        assert read_env_file() == {}

    def test_reads_key_value_pairs(self, isolated_cwd):
        Path(".env").write_text(
            "MOMENTUM_DATABASE_URL=sqlite:///test.db\n"
            "MOMENTUM_LOG_LEVEL=INFO\n",
            encoding="utf-8",
        )
        result = read_env_file()
        assert result["MOMENTUM_DATABASE_URL"] == "sqlite:///test.db"
        assert result["MOMENTUM_LOG_LEVEL"] == "INFO"

    def test_ignores_comments_and_blanks(self, isolated_cwd):
        Path(".env").write_text(
            "# comment\n"
            "\n"
            "KEY=value\n"
            "# another comment\n",
            encoding="utf-8",
        )
        assert read_env_file() == {"KEY": "value"}


class TestWriteEnvFile:
    def test_create_new_env(self, isolated_cwd):
        write_env_file({"MOMENTUM_DATABASE_URL": "sqlite:///test.db"})
        content = Path(".env").read_text(encoding="utf-8")
        assert "MOMENTUM_DATABASE_URL=sqlite:///test.db" in content
        assert "momentum-agent init" in content  # header

    def test_update_existing_key(self, isolated_cwd):
        Path(".env").write_text(
            "# keep me\n"
            "MOMENTUM_DATABASE_URL=old_value\n"
            "MOMENTUM_LOG_LEVEL=INFO\n",
            encoding="utf-8",
        )
        write_env_file({"MOMENTUM_DATABASE_URL": "new_value"})
        lines = Path(".env").read_text(encoding="utf-8").splitlines()
        assert "# keep me" in lines
        assert "MOMENTUM_DATABASE_URL=new_value" in lines
        assert "MOMENTUM_LOG_LEVEL=INFO" in lines  # untouched
        assert "MOMENTUM_DATABASE_URL=old_value" not in lines

    def test_append_new_key(self, isolated_cwd):
        Path(".env").write_text(
            "MOMENTUM_DATABASE_URL=sqlite:///test.db\n",
            encoding="utf-8",
        )
        write_env_file({"MOMENTUM_MCP_API_KEY": "secret"})
        content = Path(".env").read_text(encoding="utf-8")
        assert "MOMENTUM_DATABASE_URL=sqlite:///test.db" in content
        assert "MOMENTUM_MCP_API_KEY=secret" in content

    def test_empty_values_are_skipped(self, isolated_cwd):
        write_env_file({"KEY1": "value1", "KEY2": ""})
        content = Path(".env").read_text(encoding="utf-8")
        assert "KEY1=value1" in content
        assert "KEY2=" not in content

    def test_no_updates_noop(self, isolated_cwd):
        write_env_file({})
        assert not Path(".env").exists()

    def test_preserves_unrelated_lines(self, isolated_cwd):
        Path(".env").write_text(
            "# header comment\n"
            "OTHER_VAR=other\n"
            "MOMENTUM_DATABASE_URL=old\n",
            encoding="utf-8",
        )
        write_env_file({"MOMENTUM_DATABASE_URL": "new"})
        content = Path(".env").read_text(encoding="utf-8")
        assert "# header comment" in content
        assert "OTHER_VAR=other" in content


# ═══════════════════════════════════════════════════════════════════
# parse_db_url
# ═══════════════════════════════════════════════════════════════════


class TestParseDbUrl:
    def test_sqlite_path(self):
        result = parse_db_url("sqlite:///.momentum/tasks.db")
        assert result["scheme"] == "sqlite"
        assert "tasks.db" in result["path"]

    def test_sqlite_memory(self):
        result = parse_db_url("sqlite:///:memory:")
        assert result["scheme"] == "sqlite"
        assert result["path"] == ":memory:"

    def test_mysql_url(self):
        result = parse_db_url("mysql://user:pass@localhost:3306/momentum")
        assert result["scheme"] == "mysql"
        assert result["host"] == "localhost"
        assert result["port"] == 3306
        assert result["user"] == "user"
        assert result["password"] == "pass"
        assert result["database"] == "momentum"

    def test_azure_url(self):
        result = parse_db_url("azure://admin@host:3306/momentum")
        assert result["scheme"] == "azure"
        assert result["host"] == "host"
        assert result["port"] == 3306

    def test_bare_path_treated_as_sqlite(self):
        result = parse_db_url(".momentum/tasks.db")
        assert result["scheme"] == "sqlite"

    def test_url_encoded_password(self):
        result = parse_db_url("mysql://user:p%40ss@localhost:3306/db")
        # urllib.parse unquotes
        assert "p@ss" in (result["password"] or "")


# ═══════════════════════════════════════════════════════════════════
# is_port_available
# ═══════════════════════════════════════════════════════════════════


class TestIsPortAvailable:
    def test_free_port_returns_true(self):
        # 找一个空闲端口
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # s.close 后端口应该可用
        assert is_port_available("127.0.0.1", port) is True

    def test_occupied_port_returns_false(self):
        # 占用一个端口
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            assert is_port_available("127.0.0.1", port) is False
        finally:
            srv.close()


# ═══════════════════════════════════════════════════════════════════
# test_db_connection
# ═══════════════════════════════════════════════════════════════════


class TestTestDbConnection:
    def test_success_sqlite(self, temp_db_url):
        ok, msg = check_db_connection(temp_db_url)
        assert ok is True
        assert "成功" in msg

    def test_failure_bad_url(self):
        # 不存在的 MySQL host
        ok, msg = check_db_connection("mysql://nobody:nopass@127.0.0.1:1/nonexistent")
        assert ok is False
        assert "失败" in msg


# ═══════════════════════════════════════════════════════════════════
# 非交互模式端到端
# ═══════════════════════════════════════════════════════════════════


class TestNonInteractiveMode:
    def test_generates_env_and_config(self, isolated_cwd, monkeypatch, temp_db_url):
        from momentum_agent.setup_wizard import run_wizard

        # 重置环境变量，避免污染
        for var in ["MOMENTUM_WEB_HOST", "MOMENTUM_WEB_PORT", "MOMENTUM_MCP_HOST", "MOMENTUM_MCP_PORT"]:
            monkeypatch.delenv(var, raising=False)

        result = run_wizard(db_url=temp_db_url, non_interactive=True)

        assert result.database_url == temp_db_url
        # .env 已生成
        env_content = Path(".env").read_text(encoding="utf-8")
        assert temp_db_url in env_content
        assert "MOMENTUM_LOG_LEVEL=INFO" in env_content
        # momentum.config.json 已生成
        cfg = wizard_config.load_config()
        assert cfg["web"]["host"] == wizard_config.DEFAULT_WEB_HOST
        assert cfg["web"]["port"] == wizard_config.DEFAULT_WEB_PORT

    def test_idempotent_rerun(self, isolated_cwd, monkeypatch, temp_db_url):
        from momentum_agent.setup_wizard import run_wizard

        for var in ["MOMENTUM_WEB_HOST", "MOMENTUM_WEB_PORT"]:
            monkeypatch.delenv(var, raising=False)

        # 第一次
        run_wizard(db_url=temp_db_url, non_interactive=True)
        env1 = Path(".env").read_text(encoding="utf-8")
        cfg1 = wizard_config.load_config()

        # 第二次
        run_wizard(db_url=temp_db_url, non_interactive=True)
        env2 = Path(".env").read_text(encoding="utf-8")
        cfg2 = wizard_config.load_config()

        # 核心配置一致
        assert "MOMENTUM_DATABASE_URL=" + temp_db_url in env1
        assert "MOMENTUM_DATABASE_URL=" + temp_db_url in env2
        assert cfg1["web"] == cfg2["web"]

    def test_default_password_changed_when_weak(self, isolated_cwd, monkeypatch, temp_db_url):
        from momentum_agent.setup_wizard import run_wizard

        # 第一次跑会初始化 schema + 创建 default/momentum 弱口令
        result = run_wizard(db_url=temp_db_url, non_interactive=True)
        # 第一次跑时 default 账户应该是刚创建的弱口令，被改掉
        assert result.default_password_changed is True

        # 第二次跑，密码已不是弱口令
        result2 = run_wizard(db_url=temp_db_url, non_interactive=True)
        assert result2.default_password_changed is False


# ═══════════════════════════════════════════════════════════════════
# WizardResult 数据结构
# ═══════════════════════════════════════════════════════════════════


class TestWizardResult:
    def test_defaults(self):
        r = WizardResult()
        assert r.database_url is None
        assert r.default_password_changed is False
        assert r.env_updates == {}
        assert r.user_config_updates == {}
        assert r.mcp_sse_enabled is False
        assert r.started_server is False

    def test_mutable_defaults_are_independent(self):
        r1 = WizardResult()
        r2 = WizardResult()
        r1.env_updates["a"] = "b"
        assert r2.env_updates == {}  # 不共享
