"""Tests for the storage factory and backend routing."""
from __future__ import annotations

import os

import pytest

from momentum_agent.storage import MySQLTaskStore, SQLiteTaskStore, create_task_store


def test_factory_defaults_to_sqlite(tmp_path):
    store = create_task_store(str(tmp_path / "tasks.db"))
    assert isinstance(store, SQLiteTaskStore)


def test_factory_sqlite_url():
    store = create_task_store("sqlite:///:memory:")
    assert isinstance(store, SQLiteTaskStore)


def test_factory_mysql_url(monkeypatch):
    monkeypatch.setattr(MySQLTaskStore, "_init_schema", lambda self: None)
    store = create_task_store("mysql://root:0000@localhost:3306/momentum_db")
    assert isinstance(store, MySQLTaskStore)
    assert store.dsn == "mysql://root:0000@localhost:3306/momentum_db"


def test_factory_rejects_unsupported_url():
    with pytest.raises(ValueError, match="不支持的数据库 URL"):
        create_task_store("oracle://user:pass@localhost/db")


def test_factory_uses_environment_variable(tmp_path, monkeypatch):
    db_path = str(tmp_path / "env.db")
    monkeypatch.setenv("MOMENTUM_DATABASE_URL", db_path)
    store = create_task_store()
    assert isinstance(store, SQLiteTaskStore)
    assert str(store.db_path) == db_path


def test_factory_environment_variable_mysql(monkeypatch):
    monkeypatch.setenv("MOMENTUM_DATABASE_URL", "mysql://root:0000@localhost:3306/momentum_db")
    monkeypatch.setattr(MySQLTaskStore, "_init_schema", lambda self: None)
    store = create_task_store()
    assert isinstance(store, MySQLTaskStore)
