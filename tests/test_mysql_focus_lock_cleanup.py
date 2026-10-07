from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType

import pytest

from momentum_agent.storage.mysql import MySQLTaskStore


@pytest.mark.parametrize("failure_point", ["insert", "commit"])
def test_mysql_focus_failure_rolls_back_closes_unlocks_and_same_session_retries(
    monkeypatch, failure_point
):
    """Exercise real _connect/record_focus_session against an in-memory driver fake."""
    database = {
        "committed_events": [],
        "connections": [],
        "lock_owner": None,
        "lock_attempts": [],
        "failure_consumed": False,
    }
    injected_error = RuntimeError(f"injected {failure_point} failure")
    store = MySQLTaskStore.__new__(MySQLTaskStore)
    store._connect_kwargs = {
        "host": "memory-only.invalid",
        "cursorclass": "pymysql.cursors.DictCursor",
        "autocommit": False,
    }

    class FakeDictCursor:
        pass

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
            self.pending_events = []
            self.in_transaction = False
            self.has_advisory_lock = False
            self.advisory_lock_name = None
            self.commit_calls = 0
            self.rollback_calls = 0
            self.close_calls = 0

        def begin(self):
            assert self.has_advisory_lock
            assert not self.in_transaction
            self.in_transaction = True

        def commit(self):
            self.commit_calls += 1
            assert self.in_transaction
            if failure_point == "commit" and not database["failure_consumed"]:
                database["failure_consumed"] = True
                raise injected_error
            database["committed_events"].extend(self.pending_events)
            self.pending_events.clear()
            self.in_transaction = False

        def rollback(self):
            self.rollback_calls += 1
            self.pending_events.clear()
            self.in_transaction = False

        def close(self):
            self.close_calls += 1
            if self.has_advisory_lock:
                assert database["lock_owner"] is self
                database["lock_owner"] = None
                self.has_advisory_lock = False
                self.advisory_lock_name = None

    def fake_connect(**kwargs):
        # This is the only connect implementation installed in sys.modules;
        # the test never constructs a DSN or opens a network connection.
        assert kwargs["host"] == "memory-only.invalid"
        assert kwargs["cursorclass"] is FakeDictCursor
        conn = Connection()
        database["connections"].append(conn)
        return conn

    pymysql_module = ModuleType("pymysql")
    pymysql_module.__path__ = []
    pymysql_module.connect = fake_connect
    cursors_module = ModuleType("pymysql.cursors")
    cursors_module.DictCursor = FakeDictCursor
    pymysql_module.cursors = cursors_module
    monkeypatch.setitem(sys.modules, "pymysql", pymysql_module)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", cursors_module)

    def fake_execute(conn, sql, params=None):
        normalized = " ".join(sql.split())
        params = tuple(params or ())
        if normalized.startswith("SELECT GET_LOCK("):
            lock_name, timeout = params
            assert timeout == 30
            acquired = database["lock_owner"] is None
            if acquired:
                database["lock_owner"] = conn
                conn.has_advisory_lock = True
                conn.advisory_lock_name = lock_name
            database["lock_attempts"].append((conn, lock_name, acquired))
            return Cursor(one={"acquired": int(acquired)})

        if normalized.startswith("SELECT id FROM tasks WHERE id = %s AND user_id = %s"):
            task_id, user_id = params
            assert (task_id, user_id) == (41, "alice")
            assert conn.in_transaction and conn.has_advisory_lock
            return Cursor(one={"id": task_id})

        if normalized.startswith("SELECT e.task_id, e.payload FROM task_events"):
            assert params == ("focus_session", "alice")
            assert conn.in_transaction and conn.has_advisory_lock
            return Cursor(
                rows=[
                    {"task_id": task_id, "payload": payload}
                    for task_id, event_type, payload, _created_at
                    in database["committed_events"]
                    if event_type == "focus_session"
                ]
            )

        if normalized.startswith("INSERT INTO task_events"):
            assert conn.in_transaction and conn.has_advisory_lock
            task_id, event_type, payload, created_at = params
            # INSERTs are transaction-local until commit. For the injected
            # INSERT error, stage the write first so rollback must discard it.
            conn.pending_events.append((task_id, event_type, payload, created_at))
            if failure_point == "insert" and not database["failure_consumed"]:
                database["failure_consumed"] = True
                raise injected_error
            return Cursor()

        raise AssertionError(f"unexpected SQL in fake MySQL driver: {normalized}")

    monkeypatch.setattr(store, "_execute", fake_execute)

    started_at = datetime.now(timezone.utc) - timedelta(seconds=73)
    ended_at = started_at + timedelta(seconds=73)
    fields = {
        "user_id": "alice",
        "actual_seconds": 73,
        "planned_minutes": 25,
        "started_at": started_at,
        "ended_at": ended_at,
        "outcome": "stopped",
        "session_id": "a" * 32,
    }

    with pytest.raises(RuntimeError) as raised:
        store.record_focus_session(41, 25, **fields)

    assert raised.value is injected_error
    assert len(database["connections"]) == 1
    failed_connection = database["connections"][0]
    assert failed_connection.rollback_calls == 1
    assert failed_connection.close_calls == 1
    assert failed_connection.commit_calls == (1 if failure_point == "commit" else 0)
    assert failed_connection.pending_events == []
    assert not failed_connection.in_transaction
    assert not failed_connection.has_advisory_lock
    assert database["lock_owner"] is None
    assert database["committed_events"] == []

    # The same user/session/payload must reacquire the connection-owned lock
    # and succeed after the failed transaction has been rolled back and closed.
    result = store.record_focus_session(41, 25, **fields)

    assert result["session_id"] == fields["session_id"]
    assert result["actual_seconds"] == 73
    assert len(database["connections"]) == 2
    retried_connection = database["connections"][1]
    assert retried_connection.commit_calls == 1
    assert retried_connection.rollback_calls == 0
    assert retried_connection.close_calls == 1
    assert retried_connection.pending_events == []
    assert not retried_connection.in_transaction
    assert not retried_connection.has_advisory_lock
    assert database["lock_owner"] is None

    assert len(database["lock_attempts"]) == 2
    assert all(acquired for _conn, _lock_name, acquired in database["lock_attempts"])
    assert database["lock_attempts"][0][1] == database["lock_attempts"][1][1]
    assert database["lock_attempts"][0][0] is failed_connection
    assert database["lock_attempts"][1][0] is retried_connection

    assert len(database["committed_events"]) == 1
    task_id, event_type, stored_payload, _created_at = database["committed_events"][0]
    assert task_id == 41
    assert event_type == "focus_session"
    assert json.loads(stored_payload)["actual_seconds"] == fields["actual_seconds"]
