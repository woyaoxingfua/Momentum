from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from momentum_agent.models import TaskStatus
from momentum_agent.storage import TaskStore
from momentum_agent.storage.errors import TaskCannotBePostponed


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "task-update-postpone.sqlite3")


def _event_count(store, task_id: int, event_type: str | None = None) -> int:
    with store._connect() as conn:
        if event_type is None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND event_type = ?",
                (task_id, event_type),
            ).fetchone()
    return row["n"]


def test_update_task_is_owner_scoped_and_does_not_write_foreign_events(store, monkeypatch):
    foreign = store.create_task("foreign title", user_id="bob")
    before_events = _event_count(store, foreign.id)

    def forbidden_unscoped_read(_task_id):
        raise AssertionError("owner-scoped update must not use an unscoped task read")

    monkeypatch.setattr(store, "_get_task", forbidden_unscoped_read)
    assert store.update_task(foreign.id, title="attempted leak", user_id="alice") is None
    assert store.update_task(foreign.id, user_id="alice") is None

    still_foreign = store.get_task_for_user(foreign.id, "bob")
    assert still_foreign is not None
    assert still_foreign.title == "foreign title"
    assert _event_count(store, foreign.id) == before_events


def test_update_task_returns_only_owned_task_and_writes_success_event(store):
    owned = store.create_task("before", user_id="alice")
    foreign = store.create_task("other user's task", user_id="bob")

    updated = store.update_task(owned.id, title="after", user_id="alice")

    assert updated is not None
    assert updated.user_id == "alice"
    assert updated.title == "after"
    assert store.get_task_for_user(foreign.id, "alice") is None
    assert store.get_task_for_user(foreign.id, "bob").title == "other user's task"
    assert _event_count(store, owned.id, "updated") == 1


@pytest.mark.parametrize("status,due_at", [(TaskStatus.TODO, None), (TaskStatus.DONE, datetime(2026, 1, 2, tzinfo=timezone.utc))])
def test_postpone_ineligible_owned_task_changes_nothing(store, status, due_at):
    task = store.create_task("not postponable", due_at=due_at, user_id="alice")
    if status != TaskStatus.TODO:
        store.update_status(task.id, status, user_id="alice")
    before = store.get_task_for_user(task.id, "alice")
    before_events = _event_count(store, task.id)

    with pytest.raises(TaskCannotBePostponed):
        store.postpone_task(task.id, 1, user_id="alice")

    after = store.get_task_for_user(task.id, "alice")
    assert after.due_at == before.due_at
    assert after.status == before.status
    assert after.updated_at == before.updated_at
    assert _event_count(store, task.id) == before_events


def test_postpone_foreign_task_is_missing_and_success_adds_exactly_one_day(store):
    due = datetime(2026, 1, 2, 15, 30, tzinfo=timezone.utc)
    foreign = store.create_task("foreign deadline", due_at=due, user_id="bob")
    foreign_events = _event_count(store, foreign.id)
    assert store.postpone_task(foreign.id, 1, user_id="alice") is None
    assert store.get_task_for_user(foreign.id, "bob").due_at == due
    assert _event_count(store, foreign.id) == foreign_events

    owned = store.create_task("owned deadline", due_at=due, user_id="alice")
    result = store.postpone_task(owned.id, 1, user_id="alice")
    assert result is not None
    assert result.due_at == due + timedelta(days=1)
    assert result.title == "owned deadline"
    assert _event_count(store, owned.id, "updated") == 1
