"""Tests for the storage layer (TaskStore)."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from momentum_agent.models import Priority, TaskStatus, TaskRelationType
from momentum_agent.storage import TaskStore


@pytest.fixture
def store(tmp_path):
    """Create a fresh TaskStore for each test."""
    return TaskStore(tmp_path / "test.db")


@pytest.fixture
def store_with_tasks(store):
    """Create a TaskStore with some sample tasks."""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    store.create_task("高优先级任务", priority=Priority.HIGH, due_at=now - timedelta(days=1))
    store.create_task("普通任务", priority=Priority.MEDIUM, due_at=now + timedelta(days=1))
    store.create_task("低优先级任务", priority=Priority.LOW, due_at=now + timedelta(days=7))
    store.create_task("无截止任务", priority=Priority.MEDIUM)
    return store


# ── 基础 CRUD ──────────────────────────────────────────────────────


class TestTaskCRUD:
    def test_create_task(self, store):
        task = store.create_task("测试任务")
        assert task.id > 0
        assert task.title == "测试任务"
        assert task.status == TaskStatus.TODO
        assert task.priority == Priority.MEDIUM

    def test_create_task_with_all_fields(self, store):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        task = store.create_task(
            "完整任务",
            due_at=now,
            priority=Priority.HIGH,
            estimated_minutes=60,
            notes="备注内容",
            recurrence="daily",
            tags=["work", "urgent"],
        )
        assert task.title == "完整任务"
        assert task.due_at is not None
        assert task.priority == Priority.HIGH
        assert task.estimated_minutes == 60
        assert task.notes == "备注内容"
        assert task.recurrence == "daily"
        assert set(task.tags) == {"urgent", "work"}

    def test_list_tasks_by_status(self, store):
        store.create_task("任务1")
        store.create_task("任务2")
        task3 = store.create_task("任务3")
        store.update_status(task3.id, TaskStatus.DONE)

        todo_tasks = store.list_tasks(TaskStatus.TODO)
        done_tasks = store.list_tasks(TaskStatus.DONE)

        assert len(todo_tasks) == 2
        assert len(done_tasks) == 1

    def test_list_all_tasks(self, store):
        store.create_task("任务1")
        store.create_task("任务2")

        all_tasks = store.list_tasks(status=None)
        assert len(all_tasks) == 2

    def test_get_task(self, store):
        task = store.create_task("测试任务")
        retrieved = store._get_task(task.id)
        assert retrieved is not None
        assert retrieved.title == "测试任务"

    def test_get_nonexistent_task(self, store):
        assert store._get_task(99999) is None

    def test_update_task(self, store):
        task = store.create_task("原标题")
        updated = store.update_task(task.id, title="新标题", priority=Priority.HIGH)
        assert updated.title == "新标题"
        assert updated.priority == Priority.HIGH

    def test_update_task_partial(self, store):
        task = store.create_task("测试任务", priority=Priority.LOW)
        updated = store.update_task(task.id, priority=Priority.HIGH)
        assert updated.title == "测试任务"
        assert updated.priority == Priority.HIGH


# ── 状态转换 ──────────────────────────────────────────────────────


class TestStatusTransitions:
    def test_start_task(self, store):
        task = store.create_task("测试任务")
        started = store.start_task(task.id)
        assert started.status == TaskStatus.DOING

    def test_complete_task(self, store):
        task = store.create_task("测试任务")
        completed = store.update_status(task.id, TaskStatus.DONE)
        assert completed.status == TaskStatus.DONE

    def test_drop_task(self, store):
        task = store.create_task("测试任务")
        dropped = store.drop_task(task.id)
        assert dropped.status == TaskStatus.DROPPED

    def test_reopen_task(self, store):
        task = store.create_task("测试任务")
        store.update_status(task.id, TaskStatus.DONE)
        reopened = store.reopen_task(task.id)
        assert reopened.status == TaskStatus.TODO


# ── 子任务 ──────────────────────────────────────────────────────


class TestSubtasks:
    def test_create_subtask(self, store):
        parent = store.create_task("父任务")
        child = store.create_subtask(parent.id, "子任务1")
        assert child.parent_task_id == parent.id

    def test_get_subtasks(self, store):
        parent = store.create_task("父任务")
        store.create_subtask(parent.id, "子任务1")
        store.create_subtask(parent.id, "子任务2")

        subtasks = store.get_subtasks(parent.id)
        assert len(subtasks) == 2

    def test_get_task_with_subtasks(self, store):
        parent = store.create_task("父任务")
        store.create_subtask(parent.id, "子任务1")
        store.create_subtask(parent.id, "子任务2")

        task = store.get_task_with_subtasks(parent.id)
        assert task is not None
        assert len(task.subtasks) == 2

    def test_auto_complete_parent(self, store):
        parent = store.create_task("父任务")
        child1 = store.create_subtask(parent.id, "子任务1")
        child2 = store.create_subtask(parent.id, "子任务2")

        store.update_status(child1.id, TaskStatus.DONE)
        store.update_status(child2.id, TaskStatus.DONE)

        parent_task = store._get_task(parent.id)
        assert parent_task.status == TaskStatus.DONE


# ── 任务关系 ──────────────────────────────────────────────────────


class TestTaskRelations:
    def test_add_dependency(self, store):
        task1 = store.create_task("任务1")
        task2 = store.create_task("任务2")

        relation = store.add_dependency(task1.id, task2.id)
        assert relation is not None
        assert relation.source_task_id == task1.id
        assert relation.target_task_id == task2.id

    def test_get_dependencies(self, store):
        task1 = store.create_task("任务1")
        task2 = store.create_task("任务2")
        store.add_dependency(task1.id, task2.id)

        deps = store.get_dependencies(task1.id)
        assert len(deps) == 1
        assert deps[0].id == task2.id

    def test_get_dependents(self, store):
        task1 = store.create_task("任务1")
        task2 = store.create_task("任务2")
        store.add_dependency(task1.id, task2.id)

        dependents = store.get_dependents(task2.id)
        assert len(dependents) == 1
        assert dependents[0].id == task1.id

    def test_remove_dependency(self, store):
        task1 = store.create_task("任务1")
        task2 = store.create_task("任务2")
        store.add_dependency(task1.id, task2.id)

        result = store.remove_dependency(task1.id, task2.id)
        assert result is True

        deps = store.get_dependencies(task1.id)
        assert len(deps) == 0

    def test_is_task_blocked(self, store):
        task1 = store.create_task("任务1")
        task2 = store.create_task("任务2")
        store.add_dependency(task1.id, task2.id)

        assert store.is_task_blocked(task1.id) is True

        store.update_status(task2.id, TaskStatus.DONE)
        assert store.is_task_blocked(task1.id) is False

    def test_add_task_relation(self, store):
        task1 = store.create_task("任务1")
        task2 = store.create_task("任务2")

        relation = store.add_task_relation(task1.id, task2.id, TaskRelationType.RELATES_TO)
        assert relation is not None

    def test_get_task_relations(self, store):
        task1 = store.create_task("任务1")
        task2 = store.create_task("任务2")
        store.add_task_relation(task1.id, task2.id, TaskRelationType.RELATES_TO)

        relations = store.get_task_relations(task1.id)
        assert len(relations) == 1


# ── 标签 ──────────────────────────────────────────────────────


class TestTags:
    def test_get_all_tags(self, store):
        store.create_task("任务1", tags=["work", "urgent"])
        store.create_task("任务2", tags=["personal"])

        tags = store.get_all_tags()
        assert set(tags) == {"work", "urgent", "personal"}

    def test_get_tasks_by_tag(self, store):
        store.create_task("任务1", tags=["work"])
        store.create_task("任务2", tags=["personal"])
        store.create_task("任务3", tags=["work", "urgent"])

        work_tasks = store.get_tasks_by_tag("work")
        assert len(work_tasks) == 2

    def test_tags_deduplication(self, store):
        store.create_task("任务1", tags=["work", "work", "Work"])

        tags = store.get_all_tags()
        assert tags.count("work") == 1


# ── 批量操作 ──────────────────────────────────────────────────────


class TestBatchOperations:
    def test_batch_update_status(self, store):
        task1 = store.create_task("任务1")
        task2 = store.create_task("任务2")
        task3 = store.create_task("任务3")

        updated = store.batch_update_status([task1.id, task2.id], TaskStatus.DONE)
        assert updated == 2

        assert store._get_task(task1.id).status == TaskStatus.DONE
        assert store._get_task(task2.id).status == TaskStatus.DONE
        assert store._get_task(task3.id).status == TaskStatus.TODO

    def test_batch_add_tags(self, store):
        task1 = store.create_task("任务1")
        task2 = store.create_task("任务2")

        updated = store.batch_add_tags([task1.id, task2.id], ["work"])
        assert updated == 2

        assert "work" in store._get_task(task1.id).tags
        assert "work" in store._get_task(task2.id).tags


# ── 重复任务 ──────────────────────────────────────────────────────


class TestRecurringTasks:
    def test_complete_recurring_task(self, store):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        task = store.create_task(
            "每日任务",
            due_at=now,
            recurrence="daily",
        )

        next_task = store.complete_recurring_task(task.id)
        assert next_task is not None
        assert next_task.recurrence == "daily"
        assert next_task.id != task.id

    def test_complete_non_recurring_task(self, store):
        task = store.create_task("普通任务")
        result = store.complete_recurring_task(task.id)
        assert result is not None
        assert result.status == TaskStatus.DONE


# ── 搜索 ──────────────────────────────────────────────────────


class TestSearch:
    def test_search_by_title(self, store):
        store.create_task("学习Python")
        store.create_task("学习英语")
        store.create_task("工作汇报")

        results = store.search_tasks("学习")
        assert len(results) == 2

    def test_search_by_notes(self, store):
        store.create_task("任务1", notes="包含Python的笔记")
        store.create_task("任务2")

        results = store.search_tasks("Python")
        assert len(results) == 1

    def test_search_no_results(self, store):
        store.create_task("任务1")
        results = store.search_tasks("不存在的内容")
        assert len(results) == 0


# ── 导入导出 ──────────────────────────────────────────────────────


class TestExportImport:
    def test_export_user_data(self, store):
        store.create_task("任务1", tags=["work"])
        store.create_task("任务2")
        store.set_memory("pref1", "value1")

        data = store.export_user_data()
        assert "tasks" in data
        assert len(data["tasks"]) == 2
        assert "memory" in data
        assert data["memory"]["pref1"] == "value1"

    def test_import_user_data(self, store):
        data = {
            "tasks": [
                {"title": "导入任务1", "priority": "high"},
                {"title": "导入任务2", "priority": "low"},
            ],
            "memory": {"pref1": "value1"},
        }

        count = store.import_user_data(data)
        assert count == 2

        tasks = store.list_tasks(status=None)
        assert len(tasks) == 2


# ── 认证 ──────────────────────────────────────────────────────


class TestAuth:
    def test_register_and_login(self, store):
        from momentum_agent.auth import hash_password

        store.register_user("testuser", "Test User", hash_password("password123"))
        token = store.login_user("testuser", "password123")
        assert token is not None

        user_id = store.validate_session(token)
        assert user_id == "testuser"

    def test_login_wrong_password(self, store):
        from momentum_agent.auth import hash_password

        store.register_user("testuser", "Test User", hash_password("password123"))
        token = store.login_user("testuser", "wrongpassword")
        assert token is None

    def test_logout(self, store):
        from momentum_agent.auth import hash_password

        store.register_user("testuser", "Test User", hash_password("password123"))
        token = store.login_user("testuser", "password123")
        assert token is not None

        store.logout_user(token)
        user_id = store.validate_session(token)
        assert user_id is None

    def test_change_password(self, store):
        from momentum_agent.auth import hash_password

        store.register_user("testuser", "Test User", hash_password("oldpass"))
        result = store.change_password("testuser", "oldpass", "newpass")
        assert result is True

        token = store.login_user("testuser", "newpass")
        assert token is not None


# ── 心跳配置 ──────────────────────────────────────────────────────


class TestHeartbeat:
    def test_get_default_heartbeat_config(self, store):
        config = store.get_heartbeat_config()
        assert config["enabled"] is False
        assert config["start_hour"] == 9
        assert config["end_hour"] == 21

    def test_set_heartbeat_config(self, store):
        config = store.set_heartbeat_config(enabled=True, start_hour=8, end_hour=22)
        assert config["enabled"] is True
        assert config["start_hour"] == 8
        assert config["end_hour"] == 22

    def test_should_trigger_heartbeat(self, store):
        store.set_heartbeat_config(enabled=True, start_hour=0, end_hour=23, interval_hours=1)
        assert store.should_trigger_heartbeat() is True

    def test_should_not_trigger_when_disabled(self, store):
        store.set_heartbeat_config(enabled=False)
        assert store.should_trigger_heartbeat() is False


# ── 并发安全 ──────────────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_creates(self, store):
        import concurrent.futures

        def create_task(i):
            return store.create_task(f"并发任务{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(create_task, i) for i in range(10)]
            tasks = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert len(tasks) == 10
        all_tasks = store.list_tasks(status=None)
        assert len(all_tasks) == 10
