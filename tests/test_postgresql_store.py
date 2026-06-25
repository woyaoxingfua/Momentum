"""Tests for the PostgreSQL storage backend.

These tests require a running PostgreSQL instance. Set the environment variable
``MOMENTUM_TEST_POSTGRES_URL`` to enable them, e.g.::

    export MOMENTUM_TEST_POSTGRES_URL="postgresql://postgres:postgres@localhost:5432/momentum_test"
"""
from __future__ import annotations

import os

import pytest

from momentum_agent.models import Priority, TaskStatus
from momentum_agent.storage import PostgreSQLTaskStore


PG_URL = os.environ.get("MOMENTUM_TEST_POSTGRES_URL")


@pytest.fixture
def pg_store():
    if not PG_URL:
        pytest.skip("MOMENTUM_TEST_POSTGRES_URL is not set")
    store = PostgreSQLTaskStore(PG_URL)
    # Clean up tables for a fresh test run
    with store._connect() as conn:
        cur = store._cursor(conn)
        cur.execute("TRUNCATE TABLE task_events, task_relations, tasks, user_memory, sessions, users RESTART IDENTITY CASCADE")
        conn.commit()
    return store


@pytest.mark.skipif(not PG_URL, reason="MOMENTUM_TEST_POSTGRES_URL is not set")
class TestPostgreSQLTaskStore:
    def test_create_task(self, pg_store):
        task = pg_store.create_task("测试任务")
        assert task.id > 0
        assert task.title == "测试任务"
        assert task.status == TaskStatus.TODO

    def test_create_task_with_fields(self, pg_store):
        from datetime import datetime, timezone

        due = datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc)
        task = pg_store.create_task(
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

    def test_list_tasks_by_status(self, pg_store):
        pg_store.create_task("任务1")
        pg_store.create_task("任务2")
        done_task = pg_store.create_task("任务3")
        pg_store.update_status(done_task.id, TaskStatus.DONE)

        todo_tasks = pg_store.list_tasks(TaskStatus.TODO)
        done_tasks = pg_store.list_tasks(TaskStatus.DONE)
        assert len(todo_tasks) == 2
        assert len(done_tasks) == 1

    def test_subtasks(self, pg_store):
        parent = pg_store.create_task("父任务")
        child = pg_store.create_subtask(parent.id, "子任务")
        assert child.parent_task_id == parent.id

        subtasks = pg_store.get_subtasks(parent.id)
        assert len(subtasks) == 1

    def test_task_relations(self, pg_store):
        task1 = pg_store.create_task("任务1")
        task2 = pg_store.create_task("任务2")
        relation = pg_store.add_dependency(task1.id, task2.id)
        assert relation is not None
        assert relation.source_task_id == task1.id
        assert relation.target_task_id == task2.id

        deps = pg_store.get_dependencies(task1.id)
        assert len(deps) == 1
        assert deps[0].id == task2.id

    def test_tags(self, pg_store):
        pg_store.create_task("任务1", tags=["work"])
        pg_store.create_task("任务2", tags=["personal"])

        tags = pg_store.get_all_tags()
        assert set(tags) == {"work", "personal"}

    def test_search(self, pg_store):
        pg_store.create_task("学习Python")
        pg_store.create_task("学习英语")
        pg_store.create_task("工作汇报")

        results = pg_store.search_tasks("学习")
        assert len(results) == 2

    def test_auth(self, pg_store):
        from momentum_agent.auth import hash_password

        pg_store.register_user("pguser", "PG User", hash_password("secret"))
        token = pg_store.login_user("pguser", "secret")
        assert token is not None

        user_id = pg_store.validate_session(token)
        assert user_id == "pguser"

        pg_store.logout_user(token)
        assert pg_store.validate_session(token) is None

    def test_export_import(self, pg_store):
        pg_store.create_task("导出任务", tags=["work"])
        pg_store.set_memory("key", "value")

        data = pg_store.export_user_data()
        assert len(data["tasks"]) == 1
        assert data["memory"]["key"] == "value"

    def test_heartbeat_config(self, pg_store):
        config = pg_store.get_heartbeat_config()
        assert config["enabled"] is False

        config = pg_store.set_heartbeat_config(enabled=True, start_hour=8)
        assert config["enabled"] is True
        assert config["start_hour"] == 8
