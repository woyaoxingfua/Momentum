"""Tests for the MySQL storage backend.

These tests require a running MySQL instance. Set the environment variable
``MOMENTUM_TEST_MYSQL_URL`` to enable them, e.g.::

    export MOMENTUM_TEST_MYSQL_URL="mysql://root:0000@localhost:3306/momentum_test"
"""
from __future__ import annotations

import os

import pytest

from momentum_agent.models import Priority, TaskStatus
from momentum_agent.storage import MySQLTaskStore


MYSQL_URL = os.environ.get("MOMENTUM_TEST_MYSQL_URL")


@pytest.fixture
def mysql_store():
    if not MYSQL_URL:
        pytest.skip("MOMENTUM_TEST_MYSQL_URL is not set")
    store = MySQLTaskStore(MYSQL_URL)
    # Clean up tables for a fresh test run
    with store._connect() as conn:
        cur = store._cursor(conn)
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in ["task_events", "task_relations", "tasks", "user_memory", "sessions", "users"]:
            cur.execute(f"TRUNCATE TABLE {table}")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
    return store


@pytest.mark.skipif(not MYSQL_URL, reason="MOMENTUM_TEST_MYSQL_URL is not set")
class TestMySQLTaskStore:
    def test_create_task(self, mysql_store):
        task = mysql_store.create_task("测试任务")
        assert task.id > 0
        assert task.title == "测试任务"
        assert task.status == TaskStatus.TODO

    def test_create_task_with_fields(self, mysql_store):
        from datetime import datetime, timezone

        due = datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc)
        task = mysql_store.create_task(
            "完整任务",
            due_at=due,
            priority=Priority.HIGH,
            estimated_minutes=60,
            notes="备注",
            tags=["work", "urgent"],
        )
        assert task.priority == Priority.HIGH
        assert task.estimated_minutes == 60
        assert set(task.tags) == {"urgent", "work"}

    def test_list_tasks_by_status(self, mysql_store):
        mysql_store.create_task("任务1")
        mysql_store.create_task("任务2")
        done_task = mysql_store.create_task("任务3")
        mysql_store.update_status(done_task.id, TaskStatus.DONE)

        todo_tasks = mysql_store.list_tasks(TaskStatus.TODO)
        done_tasks = mysql_store.list_tasks(TaskStatus.DONE)
        assert len(todo_tasks) == 2
        assert len(done_tasks) == 1

    def test_subtasks(self, mysql_store):
        parent = mysql_store.create_task("父任务")
        child = mysql_store.create_subtask(parent.id, "子任务")
        assert child.parent_task_id == parent.id

        subtasks = mysql_store.get_subtasks(parent.id)
        assert len(subtasks) == 1

    def test_task_relations(self, mysql_store):
        task1 = mysql_store.create_task("任务1")
        task2 = mysql_store.create_task("任务2")
        relation = mysql_store.add_dependency(task1.id, task2.id)
        assert relation is not None
        assert relation.source_task_id == task1.id
        assert relation.target_task_id == task2.id

        deps = mysql_store.get_dependencies(task1.id)
        assert len(deps) == 1
        assert deps[0].id == task2.id

    def test_tags(self, mysql_store):
        mysql_store.create_task("任务1", tags=["work"])
        mysql_store.create_task("任务2", tags=["personal"])

        tags = mysql_store.get_all_tags()
        assert set(tags) == {"work", "personal"}

    def test_search(self, mysql_store):
        mysql_store.create_task("学习Python")
        mysql_store.create_task("学习英语")
        mysql_store.create_task("工作汇报")

        results = mysql_store.search_tasks("学习")
        assert len(results) == 2

    def test_auth(self, mysql_store):
        from momentum_agent.auth import hash_password

        mysql_store.register_user("mysqluser", "MySQL User", hash_password("secret"))
        token = mysql_store.login_user("mysqluser", "secret")
        assert token is not None

        user_id = mysql_store.validate_session(token)
        assert user_id == "mysqluser"

        mysql_store.logout_user(token)
        assert mysql_store.validate_session(token) is None

    def test_export_import(self, mysql_store):
        mysql_store.create_task("导出任务", tags=["work"])
        mysql_store.set_memory("key", "value")

        data = mysql_store.export_user_data()
        assert len(data["tasks"]) == 1
        assert data["memory"]["key"] == "value"

    def test_heartbeat_config(self, mysql_store):
        config = mysql_store.get_heartbeat_config()
        assert config["enabled"] is False

        config = mysql_store.set_heartbeat_config(enabled=True, start_hour=8)
        assert config["enabled"] is True
        assert config["start_hour"] == 8
