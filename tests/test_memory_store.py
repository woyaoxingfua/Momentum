"""内存库（sqlite:///:memory:）必须真正可用：建表后数据要跨连接存活，且实例之间互相隔离。"""
from __future__ import annotations

from momentum_agent.insights import InsightsEngine
from momentum_agent.models import Priority, TaskStatus
from momentum_agent.storage import SQLiteTaskStore, create_task_store


def test_factory_memory_url_keeps_schema_and_data():
    store = create_task_store("sqlite:///:memory:")
    assert isinstance(store, SQLiteTaskStore)
    task = store.create_task("写周报")
    assert [item.id for item in store.list_tasks()] == [task.id]
    assert store._get_task(task.id).title == "写周报"
    store.update_status(task.id, TaskStatus.DONE, user_id="default")
    assert store._get_task(task.id).status == TaskStatus.DONE
    assert [item.id for item in store.list_tasks(status=None)] == [task.id]


def test_memory_store_survives_many_connections():
    store = SQLiteTaskStore(":memory:")
    for index in range(5):
        store.create_task(f"任务{index}")
    assert len(store.list_tasks()) == 5
    assert store.search_tasks("任务3")
    assert store.get_all_tags() == []


def test_two_memory_stores_are_isolated():
    first = SQLiteTaskStore(":memory:")
    second = SQLiteTaskStore(":memory:")
    first.create_task("只属于第一个")
    assert second.list_tasks() == []
    assert first.list_tasks()[0].title == "只属于第一个"


def test_memory_store_supports_users_sessions_and_memory():
    from momentum_agent.auth import hash_password

    store = SQLiteTaskStore(":memory:")
    store.register_user("alice", "Alice", hash_password("memory-store-password"))
    token = store.login_user("alice", "memory-store-password")
    assert token and store.validate_session(token) == "alice"
    store.set_memory("city", "北京", user_id="alice")
    assert store.get_memory("city", user_id="alice") == "北京"
    store.logout_user(token)
    assert store.validate_session(token) is None


def test_insights_and_events_work_on_a_memory_store():
    store = SQLiteTaskStore(":memory:")
    task = store.create_task("写周报", priority=Priority.HIGH)
    store.update_status(task.id, TaskStatus.DONE, user_id="default")
    engine = InsightsEngine(store)
    profile = engine.build_profile()
    assert profile.total_created == 1
    assert profile.total_completed == 1
    assert isinstance(engine.generate_insights(store.list_tasks(status=None)), list)
    events = engine.get_completion_events()
    assert [event["task_id"] for event in events] == [task.id]


def test_memory_store_supports_recurrence_and_relations():
    store = SQLiteTaskStore(":memory:")
    recurring = store.create_task("每日复盘", recurrence="daily")
    from datetime import datetime, timezone

    store.update_task(recurring.id, due_at=datetime.now(timezone.utc))
    next_task = store.complete_recurring_task(recurring.id)
    assert next_task is not None and next_task.id != recurring.id
    parent = store.create_task("父任务")
    child = store.create_task("子任务", parent_task_id=parent.id)
    assert [item.title for item in store.get_subtasks(parent.id)] == ["子任务"]
    assert store.get_task_with_subtasks(parent.id).subtasks[0].id == child.id


def test_a_released_memory_store_never_leaks_into_the_next_one():
    """回归：内存库名不能基于 id()（会被复用），否则新实例可能串到旧库的数据。"""
    import gc

    first = SQLiteTaskStore(":memory:")
    first.create_task("旧实例的数据")
    first_uri = first._memory_uri
    first.close()
    del first
    gc.collect()

    second = SQLiteTaskStore(":memory:")
    assert second._memory_uri != first_uri
    assert second.list_tasks() == [], "新内存库不得看到上一个实例的数据"


def test_closing_a_memory_store_releases_its_anchor():
    store = SQLiteTaskStore(":memory:")
    store.create_task("写周报")
    store.close()
    assert store._memory_anchor is None
    store.close()  # 幂等

