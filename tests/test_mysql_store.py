"""Tests for the MySQL storage backend.

These tests require a running MySQL instance. Set the environment variable
``MOMENTUM_TEST_MYSQL_URL`` to enable them, e.g.::

    export MOMENTUM_TEST_MYSQL_URL="mysql://root:0000@localhost:3306/momentum_test"
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest

from momentum_agent.models import Priority, TaskStatus
from momentum_agent.storage import MySQLTaskStore


MYSQL_URL = os.environ.get("MOMENTUM_TEST_MYSQL_URL")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"actual_seconds": True}, id="bool-seconds"),
        pytest.param({"actual_seconds": 73.0}, id="float-seconds"),
        pytest.param({"actual_seconds": "73"}, id="string-seconds"),
        pytest.param({"actual_seconds": -1}, id="negative-seconds"),
        pytest.param({"actual_seconds": 25 * 60 + 1}, id="over-plan"),
        pytest.param({"outcome": "invalid"}, id="invalid-outcome"),
        pytest.param({"outcome": "completed"}, id="completed-before-plan"),
        pytest.param({"ended_at": datetime(2026, 1, 1)}, id="naive-ended-at"),
    ],
)
def test_mysql_focus_direct_input_validation_does_not_connect(monkeypatch, overrides):
    from datetime import datetime, timedelta, timezone

    store = MySQLTaskStore.__new__(MySQLTaskStore)

    def unexpected_connect():
        pytest.fail("input validation must run before any MySQL connection")

    monkeypatch.setattr(store, "_connect", unexpected_connect)
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    fields = {
        "actual_seconds": 73,
        "planned_minutes": 25,
        "started_at": started,
        "ended_at": started + timedelta(seconds=73),
        "outcome": "stopped",
        "session_id": "6" * 32,
    }
    fields.update(overrides)

    with pytest.raises(ValueError):
        store.record_focus_session(1, 25, user_id="alice", **fields)


def _simulated_mysql_import_store(monkeypatch, state):
    from contextlib import contextmanager
    from copy import deepcopy

    store = MySQLTaskStore.__new__(MySQLTaskStore)

    class Connection:
        def __init__(self):
            self.snapshot = deepcopy(state)
            self.last_insert_id = 0
            self.begun = False

        def begin(self):
            self.begun = True

        def insert_id(self):
            return self.last_insert_id

        def commit(self):
            assert self.begun

        def rollback(self):
            state.clear()
            state.update(deepcopy(self.snapshot))

    class Cursor:
        pass

    @contextmanager
    def fake_connect():
        conn = Connection()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def fake_execute(conn, sql, params=None):
        if sql.startswith("INSERT INTO tasks "):
            conn.last_insert_id += 1
            state["tasks"].append(tuple(params))
        elif sql.startswith("INSERT INTO task_events "):
            state["events"].append(tuple(params))
        elif sql.startswith("INSERT INTO user_memory "):
            state["memory"][params[1]] = params[2]
            if state.get("fail_after_memory_write"):
                raise RuntimeError("simulated MySQL memory write failure")
        else:
            raise AssertionError(f"unexpected SQL in MySQL simulation: {sql}")
        return Cursor()

    monkeypatch.setattr(store, "_connect", fake_connect)
    monkeypatch.setattr(store, "_execute", fake_execute)
    return store


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


def test_mysql_focus_session_lock_serializes_concurrent_duplicates(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from datetime import datetime, timezone
    from threading import Barrier, Lock

    state = {"lock": Lock(), "events": []}
    store = MySQLTaskStore.__new__(MySQLTaskStore)

    class Cursor:
        def __init__(self, *, one=None, rows=None):
            self.one = one
            self.rows = rows or []

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self):
            self.has_lock = False

        def begin(self):
            assert self.has_lock

        def commit(self):
            assert self.has_lock

        def rollback(self):
            pass

        def close(self):
            if self.has_lock:
                state["lock"].release()

    @contextmanager
    def fake_connect():
        conn = Connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fake_execute(conn, sql, params=None):
        if "GET_LOCK" in sql:
            conn.has_lock = state["lock"].acquire(timeout=params[1])
            return Cursor(one={"acquired": 1 if conn.has_lock else 0})
        if sql.lstrip().startswith("SELECT id FROM tasks"):
            return Cursor(one={"id": 1})
        if sql.lstrip().startswith("SELECT e.task_id, e.payload FROM task_events"):
            assert conn.has_lock
            return Cursor(rows=[{"task_id": 1, "payload": payload} for payload in state["events"]])
        if sql.lstrip().startswith("INSERT INTO task_events"):
            assert conn.has_lock
            state["events"].append(params[2])
            return Cursor()
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(store, "_connect", fake_connect)
    monkeypatch.setattr(store, "_execute", fake_execute)
    session_id = "3" * 32
    started_at = datetime.now(timezone.utc)
    ended_at = datetime.now(timezone.utc)
    barrier = Barrier(12)

    def record():
        barrier.wait(timeout=10)
        store.record_focus_session(
            1,
            25,
            actual_seconds=73,
            planned_minutes=25,
            started_at=started_at,
            ended_at=ended_at,
            outcome="stopped",
            session_id=session_id,
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _index: record(), range(12)))

    assert len(state["events"]) == 1


def test_mysql_focus_idempotency_conflicts_and_isolates_users_in_simulation(monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime, timedelta, timezone
    from threading import Lock

    from momentum_agent.storage.errors import IdempotencyConflict

    state = {"lock": Lock(), "owners": {1: "alice", 2: "bob"}, "events": []}
    store = MySQLTaskStore.__new__(MySQLTaskStore)

    class Cursor:
        def __init__(self, *, one=None, rows=None):
            self.one = one
            self.rows = rows or []

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self):
            self.has_lock = False

        def begin(self):
            assert self.has_lock

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            if self.has_lock:
                state["lock"].release()

    @contextmanager
    def fake_connect():
        conn = Connection()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fake_execute(conn, sql, params=None):
        if "GET_LOCK" in sql:
            conn.has_lock = state["lock"].acquire(timeout=params[1])
            return Cursor(one={"acquired": int(conn.has_lock)})
        if sql.lstrip().startswith("SELECT id FROM tasks"):
            task_id, user_id = params
            return Cursor(one={"id": task_id} if state["owners"].get(task_id) == user_id else None)
        if sql.lstrip().startswith("SELECT e.task_id, e.payload FROM task_events"):
            user_id = params[1]
            return Cursor(rows=[
                {"task_id": task_id, "payload": payload}
                for task_id, payload in state["events"]
                if state["owners"].get(task_id) == user_id
            ])
        if sql.lstrip().startswith("INSERT INTO task_events"):
            assert conn.has_lock
            state["events"].append((params[0], params[2]))
            return Cursor()
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(store, "_connect", fake_connect)
    monkeypatch.setattr(store, "_execute", fake_execute)
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    ended = datetime.now(timezone.utc)
    fields = {
        "actual_seconds": 0,
        "planned_minutes": 25,
        "started_at": started,
        "ended_at": ended,
        "outcome": "stopped",
        "session_id": "4" * 32,
    }

    original = store.record_focus_session(1, 25, user_id="alice", **fields)
    replay = store.record_focus_session(1, 25, user_id="alice", **fields)
    assert replay == original
    with pytest.raises(IdempotencyConflict):
        store.record_focus_session(1, 25, user_id="alice", **{**fields, "actual_seconds": 1})

    bob_result = store.record_focus_session(2, 25, user_id="bob", **fields)
    assert bob_result["session_id"] == fields["session_id"]
    assert len(state["events"]) == 2


def test_mysql_done_transition_cascades_events_atomically_in_simulation(monkeypatch):
    from contextlib import contextmanager
    from copy import deepcopy
    from datetime import datetime, timezone

    state = {
        "tasks": {
            task_id: {
                "id": task_id,
                "title": title,
                "status": "todo",
                "priority": "medium",
                "due_at": None,
                "estimated_minutes": None,
                "notes": None,
                "parent_task_id": parent_id,
                "recurrence": None,
                "user_id": "alice",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "tags": None,
            }
            for task_id, title, parent_id in ((1, "parent", None), (2, "child 1", 1), (3, "child 2", 1))
        },
        "events": [],
    }
    store = MySQLTaskStore.__new__(MySQLTaskStore)

    class Cursor:
        def __init__(self, *, one=None, rows=None, rowcount=0):
            self.one = one
            self.rows = rows or []
            self.rowcount = rowcount

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self):
            self.begun = False

        def begin(self):
            self.begun = True

        def commit(self):
            assert self.begun

        def rollback(self):
            pass

    @contextmanager
    def fake_connect():
        conn = Connection()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def fake_execute(conn, sql, params=None):
        sql = " ".join(sql.split())
        if sql.startswith("SELECT * FROM tasks WHERE id = %s AND user_id = %s"):
            task = state["tasks"].get(params[0])
            return Cursor(one=deepcopy(task) if task and task["user_id"] == params[1] else None)
        if sql.startswith("SELECT * FROM tasks WHERE id = %s"):
            return Cursor(one=deepcopy(state["tasks"].get(params[0])))
        if sql.startswith("SELECT parent_task_id FROM tasks WHERE id = %s"):
            task = state["tasks"].get(params[0])
            if task and len(params) > 1 and task["user_id"] != params[1]:
                task = None
            return Cursor(one={"parent_task_id": task["parent_task_id"]} if task else None)
        if sql.startswith("SELECT id FROM tasks WHERE parent_task_id = %s"):
            parent_id, done_status, owner = params
            rows = [
                {"id": task["id"]}
                for task in state["tasks"].values()
                if task["parent_task_id"] == parent_id and task["status"] != done_status and task["user_id"] == owner
            ]
            return Cursor(rows=rows)
        if sql.startswith("SELECT status FROM tasks WHERE id = %s"):
            parent = state["tasks"].get(params[0])
            if parent and len(params) > 1 and parent["user_id"] != params[1]:
                parent = None
            return Cursor(one={"status": parent["status"]} if parent else None)
        if sql.startswith("SELECT status FROM tasks WHERE parent_task_id = %s"):
            parent_id, owner = params
            return Cursor(rows=[
                {"status": task["status"]}
                for task in state["tasks"].values()
                if task["parent_task_id"] == parent_id and task["user_id"] == owner
            ])
        if sql.startswith("UPDATE tasks SET status = %s"):
            new_status, updated_at, task_id = params[:3]
            task = state["tasks"].get(task_id)
            if task and task["status"] != new_status and (len(params) < 5 or task["user_id"] == params[4]):
                task["status"] = new_status
                task["updated_at"] = updated_at
                return Cursor(rowcount=1)
            return Cursor(rowcount=0)
        if sql.startswith("INSERT INTO task_events"):
            state["events"].append(tuple(params))
            return Cursor(rowcount=1)
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(store, "_connect", fake_connect)
    monkeypatch.setattr(store, "_execute", fake_execute)
    store.update_status(1, TaskStatus.DONE, user_id="alice")
    store.update_status(1, TaskStatus.DONE, user_id="alice")

    done_events = [event for event in state["events"] if event[1:3] == ("status_changed", "done")]
    assert {event[0] for event in done_events} == {1, 2, 3}
    assert len(done_events) == 3
    assert all(task["status"] == "done" for task in state["tasks"].values())


def test_mysql_review_data_uses_one_owner_scoped_snapshot(monkeypatch):
    from contextlib import contextmanager

    store = MySQLTaskStore.__new__(MySQLTaskStore)
    state = {"begun": False, "queries": []}

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def begin(self):
            state["begun"] = True

        def commit(self):
            assert state["begun"]

    @contextmanager
    def fake_connect():
        conn = Connection()
        yield conn
        conn.commit()

    def fake_execute(conn, sql, params=None):
        assert state["begun"]
        assert params == ("alice",)
        state["queries"].append(sql)
        if "e.event_type = 'status_changed'" in sql:
            return Cursor([{
                "event_id": 17,
                "task_id": 4,
                "status_value": "done",
                "completed_at": "2026-01-01T10:00:00Z",
                "title": "completed",
                "estimated_minutes_reference": 25,
            }])
        if "e.event_type = 'focus_session'" in sql:
            return Cursor([{"task_id": 4, "payload": '{"actual_seconds":12,"ended_at":"2026-01-01T10:01:00Z"}', "created_at": "2026-01-01T10:02:00Z"}])
        raise AssertionError(f"unexpected review query: {sql}")

    monkeypatch.setattr(store, "_connect", fake_connect)
    monkeypatch.setattr(store, "_execute", fake_execute)
    result = store.get_review_data(user_id="alice")

    assert len(state["queries"]) == 2
    assert all("t.user_id = %s" in query for query in state["queries"])
    assert result["status_events"][0]["task_id"] == 4
    assert result["focus_sessions"][0]["task_id"] == 4


def test_mysql_v1_import_simulation_filters_credentials_and_returns_summary(monkeypatch):
    state = {"tasks": [], "events": [], "memory": {}}
    store = _simulated_mysql_import_store(monkeypatch, state)
    package = {
        "version": "1.0",
        "tasks": [{"title": "legacy task"}],
        "memory": {
            "API-Key": "mysql-api-key-marker",
            "service.password": "mysql-password-marker",
            "refreshToken": "mysql-token-marker",
            "theme": "sage",
        },
    }

    result = store.import_user_data_with_summary(package, user_id="recipient")

    assert result["imported_tasks"] == 1
    assert result["excluded_memory"]["count"] == 3
    assert result["excluded_memory"]["reason"]
    assert len(state["tasks"]) == 1
    assert len(state["events"]) == 1
    assert state["memory"] == {"theme": "sage"}
    excluded_values = (
        package["memory"]["API-Key"],
        package["memory"]["service.password"],
        package["memory"]["refreshToken"],
    )
    assert all(value not in repr(state) for value in excluded_values)


def test_mysql_v1_import_simulation_rolls_back_every_write_on_memory_failure(monkeypatch):
    state = {"tasks": [], "events": [], "memory": {}, "fail_after_memory_write": True}
    store = _simulated_mysql_import_store(monkeypatch, state)
    package = {
        "version": "1.0",
        "tasks": [{"title": "must roll back"}],
        "memory": {"ordinary_pref": "safe"},
    }

    with pytest.raises(RuntimeError, match="simulated MySQL memory write failure"):
        store.import_user_data(package, user_id="recipient")

    assert state["tasks"] == []
    assert state["events"] == []
    assert state["memory"] == {}


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

    def test_concurrent_focus_session_is_recorded_once(self, mysql_store):
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime, timezone
        from threading import Barrier

        task = mysql_store.create_task("并发专注记账")
        session_id = "2" * 32
        fields = {
            "actual_seconds": 73,
            "planned_minutes": 25,
            "started_at": datetime.now(timezone.utc),
            "ended_at": datetime.now(timezone.utc),
            "outcome": "stopped",
            "session_id": session_id,
        }
        concurrent_calls = 12
        barrier = Barrier(concurrent_calls)

        def record_session():
            barrier.wait(timeout=10)
            mysql_store.record_focus_session(task.id, 25, **fields)

        with ThreadPoolExecutor(max_workers=concurrent_calls) as pool:
            list(pool.map(lambda _index: record_session(), range(concurrent_calls)))

        sessions = mysql_store.get_focus_sessions(user_id="default")
        assert len([session for session in sessions if session["session_id"] == session_id]) == 1



def _simulated_mysql_task_store(monkeypatch, initial_row):
    from contextlib import contextmanager
    from copy import deepcopy

    store = MySQLTaskStore.__new__(MySQLTaskStore)
    state = {"row": deepcopy(initial_row), "events": [], "queries": [], "begin": False}

    class Cursor:
        def __init__(self, *, one=None, rowcount=0):
            self.one = deepcopy(one)
            self.rowcount = rowcount

        def fetchone(self):
            return deepcopy(self.one)

    class Connection:
        def begin(self):
            state["begin"] = True

        def commit(self):
            pass

        def rollback(self):
            pass

    @contextmanager
    def fake_connect():
        conn = Connection()
        yield conn
        conn.commit()

    def fake_execute(_conn, sql, params=None):
        state["queries"].append((sql, tuple(params or ())))
        if sql.startswith("SELECT id FROM tasks WHERE id = %s AND user_id = %s"):
            task_id, owner = params
            row = state["row"]
            return Cursor(one={"id": row["id"]} if row and row["id"] == task_id and row["user_id"] == owner else None)
        if sql.startswith("SELECT * FROM tasks WHERE id = %s AND user_id = %s"):
            task_id, owner = params
            row = state["row"]
            return Cursor(one=row if row and row["id"] == task_id and row["user_id"] == owner else None)
        if sql.startswith("UPDATE tasks SET "):
            assignments = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0].split(", ")
            values, task_id, owner = params[:-2], params[-2], params[-1]
            row = state["row"]
            if row and row["id"] == task_id and row["user_id"] == owner:
                for assignment, value in zip(assignments, values):
                    row[assignment.split(" = ", 1)[0]] = value
            # Deliberately simulate a zero affected-row result; existence must
            # come from the owner-scoped check, not this driver-dependent count.
            return Cursor(rowcount=0)
        if sql.startswith("INSERT INTO task_events "):
            state["events"].append(tuple(params))
            return Cursor(rowcount=1)
        raise AssertionError(f"unexpected SQL in task-store simulation: {sql}")

    monkeypatch.setattr(store, "_connect", fake_connect)
    monkeypatch.setattr(store, "_execute", fake_execute)
    return store, state


def _mysql_task_row(*, owner="alice", status="todo", due_at="2026-01-02T15:30:00+00:00"):
    return {
        "id": 41,
        "title": "original",
        "status": status,
        "priority": "medium",
        "due_at": due_at,
        "estimated_minutes": None,
        "notes": None,
        "parent_task_id": None,
        "recurrence": None,
        "user_id": owner,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "tags": None,
    }


def test_mysql_update_task_uses_owner_check_not_changed_rows_for_existence(monkeypatch):
    from momentum_agent.models import Task

    store, state = _simulated_mysql_task_store(monkeypatch, _mysql_task_row())

    updated = store.update_task(41, title="original", user_id="alice")

    assert isinstance(updated, Task)
    assert updated.user_id == "alice"
    assert updated.title == "original"
    assert len(state["events"]) == 1
    assert state["begin"]
    assert all("user_id = %s" in query for query, _params in state["queries"] if query.startswith("SELECT"))


def test_mysql_update_task_foreign_owner_returns_none_without_update_or_event(monkeypatch):
    store, state = _simulated_mysql_task_store(monkeypatch, _mysql_task_row(owner="bob"))

    assert store.update_task(41, title="attempted", user_id="alice") is None

    assert state["row"]["title"] == "original"
    assert state["events"] == []
    assert not any(query.startswith("UPDATE tasks") or query.startswith("INSERT INTO task_events") for query, _params in state["queries"])
    assert all("user_id = %s" in query for query, _params in state["queries"] if query.startswith("SELECT"))


@pytest.mark.parametrize(
    "row",
    [_mysql_task_row(due_at=None), _mysql_task_row(status="done")],
    ids=["missing-due", "closed-status"],
)
def test_mysql_postpone_ineligible_task_is_atomic_and_event_free(monkeypatch, row):
    from copy import deepcopy

    from momentum_agent.storage.errors import TaskCannotBePostponed

    original = deepcopy(row)
    store, state = _simulated_mysql_task_store(monkeypatch, row)

    with pytest.raises(TaskCannotBePostponed):
        store.postpone_task(41, 1, user_id="alice")

    assert state["row"] == original
    assert state["events"] == []
    assert not any(query.startswith("UPDATE tasks") or query.startswith("INSERT INTO task_events") for query, _params in state["queries"])


def test_mysql_postpone_success_updates_only_owned_due_date_and_writes_event(monkeypatch):
    from datetime import datetime, timedelta, timezone

    store, state = _simulated_mysql_task_store(monkeypatch, _mysql_task_row())
    original_due = datetime(2026, 1, 2, 15, 30, tzinfo=timezone.utc)

    updated = store.postpone_task(41, 1, user_id="alice")

    assert updated is not None
    assert updated.due_at == original_due + timedelta(days=1)
    assert updated.title == "original"
    assert state["row"]["user_id"] == "alice"
    assert len(state["events"]) == 1
    assert all("user_id = %s" in query for query, _params in state["queries"] if query.startswith("SELECT"))
