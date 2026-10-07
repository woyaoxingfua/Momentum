from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from momentum_agent.auth import hash_password
from momentum_agent.models import Priority, TaskRelationType, TaskStatus
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.storage.backup import (
    BackupNotEmptyError,
    BackupValidationError,
    UNATTRIBUTED_EVENTS_REASON,
    EXCLUDED_CREDENTIAL_MEMORY_REASON,
)


def _register(store: SQLiteTaskStore, user_id: str) -> None:
    store.register_user(user_id, user_id.title(), hash_password("backup-test-password"))


def _full_source_backup(tmp_path) -> tuple[SQLiteTaskStore, dict]:
    store = SQLiteTaskStore(tmp_path / "source.sqlite3")
    _register(store, "source")
    _register(store, "other")
    parent = store.create_task(
        "完整父任务",
        due_at=datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc),
        priority=Priority.HIGH,
        estimated_minutes=73,
        notes="保留备注",
        recurrence="weekly",
        tags=["work", "urgent"],
        user_id="source",
    )
    child = store.create_task(
        "子任务",
        parent_task_id=parent.id,
        priority=Priority.LOW,
        notes="子项",
        tags=["home"],
        user_id="source",
    )
    store.update_status(parent.id, TaskStatus.DONE, user_id="source")
    relation = store.add_task_relation(
        parent.id, child.id, TaskRelationType.DEPENDS_ON, user_id="source"
    )
    assert relation is not None
    store.create_task("其他用户任务", user_id="other")
    store.set_memory("theme", "sage", user_id="source")
    store.set_memory("api_key", "test-secret-not-for-export", user_id="source")
    store.set_memory("unrelated", "other-user-memory", user_id="other")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (NULL, ?, ?, ?)",
            ("legacy_unowned", "old event", "2026-01-01T00:00:00+00:00"),
        )
    return store, store.export_user_data(user_id="source")


def _snapshot(store: SQLiteTaskStore) -> dict[str, list[tuple]]:
    tables = ("users", "sessions", "tasks", "task_relations", "task_events", "user_memory")
    with store._connect() as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in tables
        }


def _table_rows(store: SQLiteTaskStore, table: str, where: str, args: tuple) -> list[tuple]:
    with store._connect() as conn:
        return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY 1", args)]


def test_v2_exports_complete_safe_user_data_and_exclusions(tmp_path):
    store, backup = _full_source_backup(tmp_path)

    assert backup["version"] == "2.0"
    assert backup["user_id"] == "source"  # informational only; never selects restore target
    assert {"id", "status", "priority", "due_at", "estimated_minutes", "notes", "parent_task_id", "recurrence", "created_at", "updated_at", "tags"} <= set(backup["tasks"][0])
    assert len(backup["tasks"]) == 2
    assert all("user_id" not in task for task in backup["tasks"])
    assert backup["memory"] == {"theme": "sage"}
    assert backup["memory_updated_at"].keys() == backup["memory"].keys()
    assert backup["excluded_memory"] == {"count": 1, "reason": EXCLUDED_CREDENTIAL_MEMORY_REASON}
    assert backup["excluded_events"] == {"count": None, "reason": UNATTRIBUTED_EVENTS_REASON}
    assert "数量未知" in backup["excluded_events"]["reason"]
    assert "不代表为零" in backup["excluded_events"]["reason"]
    assert all(event["task_id"] is not None for event in backup["events"])
    # The real child transition now has its own status_changed/done event.
    assert len(backup["events"]) == 5
    assert len(backup["relations"]) == 1
    assert "test-secret-not-for-export" not in repr(backup)
    assert "unrelated" not in backup["memory"]
    assert store.list_tasks(status=None, user_id="other")[0].title == "其他用户任务"


def test_v2_round_trip_remaps_ids_and_preserves_fields_and_other_users(tmp_path):
    source, backup = _full_source_backup(tmp_path)
    source_tasks = {task["id"]: task for task in backup["tasks"]}
    target = SQLiteTaskStore(tmp_path / "target.sqlite3")
    _register(target, "recipient")
    _register(target, "other")
    _register(target, "unrelated")
    other_parent = target.create_task("别人的父任务", user_id="other")
    other_child = target.create_task("别人的子任务", parent_task_id=other_parent.id, user_id="other")
    target.add_dependency(other_parent.id, other_child.id, user_id="other")
    target.set_memory("keep", "unchanged", user_id="other")
    session = target.login_user("recipient", "backup-test-password")
    assert session
    users_before = _table_rows(target, "users", "id != ?", ("recipient",))
    sessions_before = _table_rows(target, "sessions", "token = ?", (session,))
    other_tasks_before = _table_rows(target, "tasks", "user_id = ?", ("other",))
    other_relations_before = _table_rows(target, "task_relations", "user_id = ?", ("other",))
    other_memory_before = _table_rows(target, "user_memory", "user_id = ?", ("other",))

    result = target.restore_user_data(backup, user_id="recipient")

    assert result["imported_tasks"] == 2
    assert result["excluded_events"] == backup["excluded_events"]
    assert result["excluded_memory"] == backup["excluded_memory"]
    id_map = result["id_map"]
    assert set(id_map) == set(source_tasks)
    assert all(old_id != new_id for old_id, new_id in id_map.items())
    restored = {task.id: task for task in target.list_tasks(status=None, user_id="recipient")}
    assert len(restored) == 2
    source_parent = next(task for task in backup["tasks"] if task["parent_task_id"] is None)
    source_child = next(task for task in backup["tasks"] if task["parent_task_id"] is not None)
    parent = restored[id_map[source_parent["id"]]]
    child = restored[id_map[source_child["id"]]]
    assert parent.title == source_parent["title"]
    assert parent.status.value == source_parent["status"] == "done"
    assert parent.priority.value == source_parent["priority"] == "high"
    assert parent.due_at.isoformat() == source_parent["due_at"]
    assert parent.estimated_minutes == source_parent["estimated_minutes"] == 73
    assert parent.notes == source_parent["notes"] == "保留备注"
    assert parent.recurrence == source_parent["recurrence"] == "weekly"
    assert parent.created_at.isoformat() == source_parent["created_at"]
    assert parent.updated_at.isoformat() == source_parent["updated_at"]
    assert parent.tags == source_parent["tags"] == ["urgent", "work"]
    assert child.parent_task_id == parent.id
    assert child.tags == ["home"]
    relation = target.get_task_relations(parent.id, user_id="recipient")[0]
    assert relation.source_task_id == parent.id
    assert relation.target_task_id == child.id
    assert relation.relation_type == TaskRelationType.DEPENDS_ON

    with target._connect() as conn:
        restored_events = conn.execute(
            "SELECT e.task_id, e.event_type, e.payload, e.created_at FROM task_events e "
            "JOIN tasks t ON t.id = e.task_id WHERE t.user_id = ? ORDER BY e.id",
            ("recipient",),
        ).fetchall()
        restored_memory = conn.execute(
            "SELECT `key`, value, updated_at FROM user_memory WHERE user_id = ?",
            ("recipient",),
        ).fetchall()
    expected_events = {
        (id_map[event["task_id"]], event["event_type"], event["payload"], event["created_at"])
        for event in backup["events"]
    }
    assert {tuple(row) for row in restored_events} == expected_events
    assert [(row["key"], row["value"], row["updated_at"]) for row in restored_memory] == [
        ("theme", "sage", backup["memory_updated_at"]["theme"])
    ]
    assert _table_rows(target, "users", "id != ?", ("recipient",)) == users_before
    assert _table_rows(target, "sessions", "token = ?", (session,)) == sessions_before
    assert _table_rows(target, "tasks", "user_id = ?", ("other",)) == other_tasks_before
    assert _table_rows(target, "task_relations", "user_id = ?", ("other",)) == other_relations_before
    assert _table_rows(target, "user_memory", "user_id = ?", ("other",)) == other_memory_before
    assert source.list_tasks(status=None, user_id="source")  # source database was not mutated


def test_v1_import_remains_a_merge_and_ignores_source_identity(tmp_path):
    store = SQLiteTaskStore(tmp_path / "v1.sqlite3")
    _register(store, "alice")
    _register(store, "victim")
    existing = store.create_task("既有任务", user_id="alice")
    victim_task = store.create_task("victim 保留", user_id="victim")
    before_victim = _table_rows(store, "tasks", "user_id = ?", ("victim",))
    old_backup = {
        "version": "1.0",
        "user_id": "victim",
        "tasks": [{"title": "旧版导入任务", "priority": "high", "status": "done"}],
        "memory": {"legacy_pref": "kept"},
    }

    assert store.import_user_data(old_backup, user_id="alice") == 1

    tasks = store.list_tasks(status=None, user_id="alice")
    assert {task.title for task in tasks} == {"既有任务", "旧版导入任务"}
    assert store._get_task(existing.id).title == "既有任务"
    imported = next(task for task in tasks if task.title == "旧版导入任务")
    assert imported.status == TaskStatus.TODO  # preserve legacy importer semantics
    assert store.get_memory("legacy_pref", user_id="alice") == "kept"
    assert _table_rows(store, "tasks", "user_id = ?", ("victim",)) == before_victim
    assert store._get_task(victim_task.id).title == "victim 保留"


def test_v1_import_filters_credential_memory_shapes_and_reports_count(tmp_path):
    store = SQLiteTaskStore(tmp_path / "v1-credential-filter.sqlite3")
    _register(store, "recipient")
    memory = {
        "theme": "sage",
        "API-Key": "credential-value-a",
        "database.password": "credential-value-b",
        "refreshToken": "credential-value-c",
        "client_secret": "credential-value-d",
        "private_key": "credential-value-e",
        "access-key": "credential-value-f",
        "auth_token": "credential-value-g",
        "working_hours_start": "09:00",
    }
    package = {"version": "1.0", "tasks": [], "memory": memory}

    result = store.import_user_data_with_summary(package, user_id="recipient")

    assert result == {
        "imported_tasks": 0,
        "excluded_memory": {"count": 7, "reason": EXCLUDED_CREDENTIAL_MEMORY_REASON},
    }
    assert store.get_memory("theme", user_id="recipient") == "sage"
    assert store.get_memory("working_hours_start", user_id="recipient") == "09:00"
    for key in memory:
        if key not in {"theme", "working_hours_start"}:
            assert store.get_memory(key, user_id="recipient") is None
    stored = _table_rows(store, "user_memory", "user_id = ?", ("recipient",))
    assert {row[1] for row in stored} == {"theme", "working_hours_start"}
    assert not any(value in repr(result) for value in memory.values())


def test_v2_restore_ignores_events_not_owned_by_current_user(tmp_path):
    _, backup = _full_source_backup(tmp_path)
    store = SQLiteTaskStore(tmp_path / "events-not-owned.sqlite3")
    _register(store, "recipient")
    _register(store, "other")
    other_task = store.create_task("其他用户任务", user_id="other")
    with store._connect() as conn:
        conn.executemany(
            "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
            [
                (other_task.id, "other_user_event", "belongs to other", "2026-01-02T00:00:00+00:00"),
                (None, "legacy_unowned", "no task", "2026-01-03T00:00:00+00:00"),
                (999_999_999, "orphan_event", "missing task", "2026-01-04T00:00:00+00:00"),
            ],
        )
    with store._connect() as conn:
        events_before = [tuple(row) for row in conn.execute("SELECT * FROM task_events ORDER BY id")]

    result = store.restore_user_data(backup, user_id="recipient")

    assert result["imported_tasks"] == 2
    with store._connect() as conn:
        events_after = [tuple(row) for row in conn.execute("SELECT * FROM task_events ORDER BY id")]
    assert events_after[:len(events_before)] == events_before
    assert store.list_tasks(status=None, user_id="other")[0].title == "其他用户任务"


def test_v2_restore_rejects_events_on_current_users_existing_task(tmp_path):
    _, backup = _full_source_backup(tmp_path)
    store = SQLiteTaskStore(tmp_path / "owned-task-event.sqlite3")
    _register(store, "target")
    existing = store.create_task("当前用户已有任务", user_id="target")
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (existing.id,)
        ).fetchone()[0] > 0
    before = _snapshot(store)

    with pytest.raises(BackupNotEmptyError):
        store.restore_user_data(backup, user_id="target")

    assert _snapshot(store) == before


@pytest.mark.parametrize(
    "case",
    ["nonempty", "malformed", "unknown_version", "numeric_event_count", "null_memory_count"],
)
def test_v2_rejections_make_zero_database_changes(tmp_path, case):
    _, valid_backup = _full_source_backup(tmp_path)
    store = SQLiteTaskStore(tmp_path / f"{case}.sqlite3")
    _register(store, "target")
    _register(store, "other")
    if case == "nonempty":
        store.create_task("当前用户已有任务", user_id="target")
        backup = valid_backup
        expected = BackupNotEmptyError
    elif case == "malformed":
        backup = deepcopy(valid_backup)
        backup["tasks"][0]["tags"] = "not-an-array"
        expected = BackupValidationError
    elif case == "unknown_version":
        backup = deepcopy(valid_backup)
        backup["version"] = "91.0"
        expected = BackupValidationError
    elif case == "numeric_event_count":
        backup = deepcopy(valid_backup)
        backup["excluded_events"]["count"] = 0
        expected = BackupValidationError
    else:
        backup = deepcopy(valid_backup)
        backup["excluded_memory"]["count"] = None
        expected = BackupValidationError
    before = _snapshot(store)

    with pytest.raises(expected):
        store.restore_user_data(backup, user_id="target")

    assert _snapshot(store) == before


def test_v2_write_failure_rolls_back_every_table(tmp_path, monkeypatch):
    _, backup = _full_source_backup(tmp_path)
    store = SQLiteTaskStore(tmp_path / "rollback-v2.sqlite3")
    _register(store, "target")
    before = _snapshot(store)
    import momentum_agent.storage.backup as backup_module

    original = backup_module._insert_v2_event

    def insert_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected event insert failure")

    monkeypatch.setattr(backup_module, "_insert_v2_event", insert_then_fail)
    with pytest.raises(RuntimeError, match="injected event insert failure"):
        store.restore_user_data(backup, user_id="target")
    assert _snapshot(store) == before


def test_v1_write_failure_rolls_back_tasks_events_and_memory(tmp_path, monkeypatch):
    store = SQLiteTaskStore(tmp_path / "rollback-v1.sqlite3")
    _register(store, "target")
    before = _snapshot(store)
    import momentum_agent.storage.backup as backup_module

    original = backup_module._write_memory

    def write_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected memory write failure")

    monkeypatch.setattr(backup_module, "_write_memory", write_then_fail)
    package = {"version": "1.0", "tasks": [{"title": "must roll back"}], "memory": {"k": "v"}}
    with pytest.raises(RuntimeError, match="injected memory write failure"):
        store.import_user_data(package, user_id="target")
    assert _snapshot(store) == before
