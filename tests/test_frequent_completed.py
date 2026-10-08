"""历史常完成任务 — 口径、聚合与存储测试（docs/PRODUCT_REQUESTS_20261007.md 第一项之外的第二项）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from momentum_agent.auth import hash_password
from backend_fixtures import fetch_scalar, run_sql
from momentum_agent.frequent import (
    MIN_DISTINCT_TASKS,
    aggregate_frequent_tasks,
    find_frequent_task,
    history_since,
    normalize_title,
)
from momentum_agent.models import Priority, TaskStatus
from momentum_agent.storage import SQLiteTaskStore

PASSWORD = "frequent-completed-password"


@pytest.fixture(params=["sqlite", "mysql"])
def store(request, tmp_path):
    """同一批「常完成」断言同时跑 SQLite 与 MySQL。"""
    if request.param == "sqlite":
        database = SQLiteTaskStore(tmp_path / "frequent.db")
    else:
        from backend_fixtures import fresh_mysql_store

        database = fresh_mysql_store()
    for user_id in ("alice", "bob"):
        database.register_user(user_id, user_id, hash_password(PASSWORD))
    return database


def _complete(store, title, *, user_id="alice", priority=Priority.MEDIUM, minutes=None, tags=None, recurrence=None):
    task = store.create_task(
        title,
        priority=priority,
        estimated_minutes=minutes,
        tags=tags,
        recurrence=recurrence,
        user_id=user_id,
    )
    store.update_status(task.id, TaskStatus.DONE, user_id=user_id)
    return task


def _backdate(store, task_id, days):
    moment = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    run_sql(
        store,
        "UPDATE task_events SET created_at = {ph} WHERE task_id = {ph}"
        " AND event_type = 'status_changed' AND payload = 'done'",
        (moment, task_id),
    )


def _done_event_count(store, task_id):
    return fetch_scalar(
        store,
        "SELECT COUNT(*) FROM task_events WHERE task_id = {ph}"
        " AND event_type = 'status_changed' AND payload = 'done'",
        (task_id,),
    )


def _groups(store, *, user_id="alice", days=180, min_distinct=MIN_DISTINCT_TASKS):
    rows = store.list_completed_task_history(user_id=user_id, since=history_since(days))
    return aggregate_frequent_tasks(rows, min_distinct_tasks=min_distinct)


# ── 标题标准化：合并 / 不合并（模糊匹配被明确禁止） ────────────────

@pytest.mark.parametrize("left,right", [
    ("写周报", "  写周报  "),
    ("写 周报", "写  周报"),   # 仅折叠空白串，不删除分隔空格
    ("Write Report", "write report"),
    ("ＡＢＣ 报告", "ABC 报告"),
])
def test_normalization_merges_only_after_deterministic_normalization(left, right):
    assert normalize_title(left) == normalize_title(right)


@pytest.mark.parametrize("left,right", [
    ("写周报", "写周报（第2版）"),
    ("写周报", "写周报2"),
    ("写周报", "写周报!"),
    ("写周报", "写 周报 总结"),
    ("写周报", "周报"),
    ("写周报", "写 周报"),
])
def test_normalization_is_not_fuzzy(left, right):
    assert normalize_title(left) != normalize_title(right)


def test_normalize_title_is_idempotent_and_handles_empty():
    assert normalize_title(None) == ""
    assert normalize_title("   ") == ""
    once = normalize_title("  Write   Report  ")
    assert normalize_title(once) == once


def test_history_since_is_timezone_aware_and_offsets_by_days():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    assert history_since(180, now=now) == datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    assert history_since(0, now=now).tzinfo is not None


# ── 数据源：真实完成事件 ────────────────────────────────────────

def test_a_single_completion_is_a_real_event_but_does_not_qualify(store):
    task = _complete(store, "写周报")
    assert _done_event_count(store, task.id) == 1
    rows = store.list_completed_task_history(user_id="alice", since=history_since())
    assert [row["task_id"] for row in rows] == [task.id]
    assert rows[0]["completed_at"].tzinfo is not None
    assert aggregate_frequent_tasks(rows) == []


def test_reopen_then_recomplete_is_still_one_distinct_task(store):
    task = _complete(store, "写周报")
    store.reopen_task(task.id, user_id="alice")
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    assert _done_event_count(store, task.id) == 2
    rows = store.list_completed_task_history(user_id="alice", since=history_since())
    assert len(rows) == 2
    assert aggregate_frequent_tasks(rows) == [], "同一任务 ID 的重复完成不得重复加权"


def test_two_distinct_task_ids_with_the_same_title_qualify(store):
    first = _complete(store, "写周报", minutes=30, priority=Priority.LOW, tags=["工作"])
    second = _complete(store, "写周报", minutes=45, priority=Priority.HIGH, tags=["例行"])
    _backdate(store, first.id, 3)
    groups = _groups(store)
    assert len(groups) == 1
    group = groups[0]
    assert group.completion_count == 2
    assert group.title == "写周报"
    assert group.priority == "high", "展示字段必须来自最近一次完成的那条"
    assert group.estimated_minutes == 45
    assert group.tags == ["例行"]
    assert group.task_ids == sorted([first.id, second.id])
    assert second.id in group.task_ids and first.id in group.task_ids


def test_repeat_completions_across_three_tasks_count_three(store):
    first = _complete(store, "复盘")
    store.reopen_task(first.id, user_id="alice")
    store.update_status(first.id, TaskStatus.DONE, user_id="alice")
    _complete(store, "复盘")
    _complete(store, "复盘")
    groups = _groups(store)
    assert len(groups) == 1
    assert groups[0].completion_count == 3


def test_recurring_tasks_are_excluded(store):
    _complete(store, "每日站会", recurrence="daily")
    _complete(store, "每日站会", recurrence="weekly")
    assert _groups(store) == []


def test_history_is_scoped_to_the_owning_user(store):
    _complete(store, "写周报", user_id="alice")
    _complete(store, "写周报", user_id="bob")
    assert _groups(store, user_id="alice") == []
    assert _groups(store, user_id="bob") == []
    _complete(store, "写周报", user_id="alice")
    alice = _groups(store, user_id="alice")
    assert len(alice) == 1
    assert alice[0].completion_count == 2
    assert _groups(store, user_id="bob") == [], "bob 不能看到 alice 的完成历史"


def test_window_is_a_bound_parameter(store):
    old = _complete(store, "整理桌面")
    recent = _complete(store, "整理桌面")
    _backdate(store, old.id, 181)
    _backdate(store, recent.id, 10)
    assert _groups(store, days=180) == [], "181 天前的完成事件必须落在窗口外"
    groups = _groups(store, days=200)
    assert len(groups) == 1 and groups[0].completion_count == 2


def test_results_sort_by_most_recent_completion_desc(store):
    older_a = _complete(store, "整理桌面")
    older_b = _complete(store, "整理桌面")
    newer_a = _complete(store, "写周报")
    newer_b = _complete(store, "写周报")
    for task in (older_a, older_b):
        _backdate(store, task.id, 5)
    for task in (newer_a, newer_b):
        _backdate(store, task.id, 1)
    assert [group.title for group in _groups(store)] == ["写周报", "整理桌面"]


def test_limit_truncates_after_sorting(store):
    for _ in range(3):
        _complete(store, "任务甲")
    for _ in range(3):
        _complete(store, "任务乙")
    rows = store.list_completed_task_history(user_id="alice", since=history_since())
    assert len(aggregate_frequent_tasks(rows, limit=1)) == 1


def test_aggregate_ignores_malformed_rows():
    rows = [
        {"task_id": None, "title": "写周报", "completed_at": "2026-10-01T00:00:00+00:00"},
        {"task_id": 1, "title": "写周报", "completed_at": "not-a-date"},
        {"task_id": 2, "title": "", "completed_at": "2026-10-01T00:00:00+00:00"},
        "not-a-row",
        None,
    ]
    assert aggregate_frequent_tasks(rows) == []


def test_aggregate_accepts_naive_timestamps_and_string_tags():
    rows = [
        {"task_id": 1, "title": "写周报", "completed_at": "2026-10-01T00:00:00", "tags": "工作, 例行"},
        {"task_id": 2, "title": "写周报", "completed_at": datetime(2026, 10, 2, 3, 0), "tags": None},
    ]
    groups = aggregate_frequent_tasks(rows)
    assert len(groups) == 1
    assert groups[0].completion_count == 2
    assert groups[0].tags is None
    assert groups[0].to_dict()["tags"] == []
    assert groups[0].last_completed_at == datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc)


def test_find_frequent_task_uses_normalized_key(store):
    _complete(store, "写周报")
    _complete(store, "写周报")
    rows = store.list_completed_task_history(user_id="alice", since=history_since())
    assert find_frequent_task(rows, "  写周报  ") is not None
    assert find_frequent_task(rows, "写周报2") is None
    assert find_frequent_task(rows, "") is None


def test_store_history_returns_only_status_changed_done_events(store):
    task = store.create_task("写周报", user_id="alice")
    store.update_status(task.id, TaskStatus.DOING, user_id="alice")
    assert store.list_completed_task_history(user_id="alice", since=history_since()) == []
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    rows = store.list_completed_task_history(user_id="alice", since=history_since())
    assert len(rows) == 1
    assert rows[0]["title"] == "写周报"

