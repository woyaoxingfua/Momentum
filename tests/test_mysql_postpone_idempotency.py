from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from threading import Lock

import pytest

from momentum_agent.storage.mysql import MySQLTaskStore, SCHEMA


KEY = "00000000-0000-4000-8000-000000000001"


def _fingerprint(days):
    canonical = json.dumps(
        {"method": "POST", "task_id": 41, "days": days},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


FINGERPRINT = _fingerprint(1)


def _simulated_mysql_store(monkeypatch, *, fail_after_event=False):
    store = MySQLTaskStore.__new__(MySQLTaskStore)
    due = datetime(2026, 1, 2, 15, 30, tzinfo=timezone.utc).isoformat()
    state = {
        "tasks": {
            41: {
                "id": 41,
                "title": "MySQL mock task",
                "status": "todo",
                "priority": "medium",
                "due_at": due,
                "estimated_minutes": None,
                "notes": None,
                "parent_task_id": None,
                "recurrence": None,
                "user_id": "alice",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "tags": None,
            }
        },
        "events": [],
        "idempotency": {},
        "query_log": [],
        "fail_after_event": fail_after_event,
        "rollbacks": 0,
    }
    named_lock = Lock()

    class Cursor:
        def __init__(self, one=None, rowcount=0):
            self.one = deepcopy(one)
            self.rowcount = rowcount

        def fetchone(self):
            return deepcopy(self.one)

    class Connection:
        def __init__(self):
            self.snapshot = None
            self.has_named_lock = False

        def begin(self):
            self.snapshot = {
                name: deepcopy(state[name])
                for name in ("tasks", "events", "idempotency")
            }

        def commit(self):
            self.snapshot = None

        def rollback(self):
            state["rollbacks"] += 1
            if self.snapshot is not None:
                for name, value in self.snapshot.items():
                    state[name] = deepcopy(value)
                self.snapshot = None

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
            if conn.has_named_lock:
                named_lock.release()
                conn.has_named_lock = False

    def fake_execute(conn, sql, params=None):
        normalized = " ".join(sql.split())
        params = tuple(params or ())
        state["query_log"].append((normalized, params))
        if normalized.startswith("SELECT GET_LOCK("):
            lock_name, timeout = params
            conn.has_named_lock = named_lock.acquire(timeout=timeout)
            return Cursor({"acquired": int(conn.has_named_lock)})
        if normalized.startswith("SELECT RELEASE_LOCK("):
            assert conn.has_named_lock
            named_lock.release()
            conn.has_named_lock = False
            return Cursor({"released": 1})
        if normalized.startswith("SELECT request_fingerprint, response_status, response_json FROM task_postpone_idempotency"):
            user_id, key = params
            return Cursor(state["idempotency"].get((user_id, key)))
        if normalized.startswith("SELECT * FROM tasks WHERE id = %s AND user_id = %s"):
            task_id, user_id = params
            row = state["tasks"].get(task_id)
            return Cursor(row if row and row["user_id"] == user_id else None)
        if normalized.startswith("UPDATE tasks SET due_at = %s, updated_at = %s WHERE id = %s AND user_id = %s"):
            due_at, updated_at, task_id, user_id = params
            row = state["tasks"].get(task_id)
            if row and row["user_id"] == user_id:
                row["due_at"] = due_at
                row["updated_at"] = updated_at
                return Cursor(rowcount=1)
            return Cursor(rowcount=0)
        if normalized.startswith("INSERT INTO task_events"):
            state["events"].append(params)
            if state["fail_after_event"]:
                state["fail_after_event"] = False
                raise RuntimeError("simulated MySQL event write failure")
            return Cursor(rowcount=1)
        if normalized.startswith("INSERT INTO task_postpone_idempotency"):
            user_id, key, fingerprint, status, response_json, created_at = params
            state["idempotency"][(user_id, key)] = {
                "request_fingerprint": fingerprint,
                "response_status": status,
                "response_json": response_json,
                "created_at": created_at,
            }
            return Cursor(rowcount=1)
        raise AssertionError(f"unexpected SQL: {normalized}")

    monkeypatch.setattr(store, "_connect", fake_connect)
    monkeypatch.setattr(store, "_execute", fake_execute)
    return store, state


def test_mysql_postpone_idempotency_uses_innodb_keyed_lock_and_replays_saved_response(monkeypatch):
    store, state = _simulated_mysql_store(monkeypatch)

    first = store.postpone_task_idempotent(
        41, 1, user_id="alice", idempotency_key=KEY, request_fingerprint=FINGERPRINT
    )
    due_after_first = state["tasks"][41]["due_at"]
    replay = store.postpone_task_idempotent(
        41, 1, user_id="alice", idempotency_key=KEY, request_fingerprint=FINGERPRINT
    )
    conflict = store.postpone_task_idempotent(
        41, 2, user_id="alice", idempotency_key=KEY,
        request_fingerprint='{"days":2,"method":"POST","task_id":41}',
    )

    assert first == replay
    assert first[0] == 200
    assert first[1] == {"message": "任务 #41「MySQL mock task」已推迟至 2026-01-03 15:30"}
    assert conflict == (409, {"error": "idempotency_conflict"})
    assert state["tasks"][41]["due_at"] == due_after_first
    assert state["tasks"][41]["due_at"] == (
        datetime(2026, 1, 2, 15, 30, tzinfo=timezone.utc) + timedelta(days=1)
    ).isoformat()
    assert len(state["events"]) == 1
    assert len(state["idempotency"]) == 1
    stored_fingerprint = state["idempotency"][("alice", KEY)]["request_fingerprint"]
    assert stored_fingerprint == FINGERPRINT
    assert len(stored_fingerprint) == 64

    queries = [sql for sql, _params in state["query_log"]]
    assert "ENGINE=InnoDB" in SCHEMA
    assert any(sql.startswith("SELECT GET_LOCK(") for sql in queries)
    assert any(sql.startswith("SELECT RELEASE_LOCK(") for sql in queries)
    assert sum(sql.startswith("INSERT INTO task_events") for sql in queries) == 1
    assert sum(sql.startswith("INSERT INTO task_postpone_idempotency") for sql in queries) == 1
    task_reads = [sql for sql in queries if sql.startswith("SELECT * FROM tasks")]
    assert len(task_reads) == 2  # qualification read plus success-response read
    assert all("user_id = %s" in sql for sql in task_reads)
    first_lock_index = queries.index(next(sql for sql in queries if sql.startswith("SELECT GET_LOCK(")))
    first_key_read_index = queries.index(
        next(sql for sql in queries if sql.startswith("SELECT request_fingerprint"))
    )
    assert first_lock_index < first_key_read_index


@pytest.mark.parametrize("days", [True, 1.0, "1", None, 0, -1])
def test_mysql_invalid_days_returns_400_without_task_or_idempotency_writes(monkeypatch, days):
    store, state = _simulated_mysql_store(monkeypatch)
    original_task = deepcopy(state["tasks"][41])

    result = store.postpone_task_idempotent(
        41, days, user_id="alice", idempotency_key=KEY,
        request_fingerprint=_fingerprint(days),
    )

    assert result == (400, {"error": "days_invalid"})
    assert state["tasks"][41] == original_task
    assert state["events"] == []
    assert state["idempotency"] == {}
    queries = [sql for sql, _params in state["query_log"]]
    assert not any(sql.startswith("SELECT * FROM tasks") for sql in queries)
    assert not any(sql.startswith(("UPDATE tasks", "INSERT INTO task_events", "INSERT INTO task_postpone_idempotency")) for sql in queries)


def test_mysql_extremely_large_days_returns_out_of_range_without_writes(monkeypatch):
    store, state = _simulated_mysql_store(monkeypatch)
    original_task = deepcopy(state["tasks"][41])

    result = store.postpone_task_idempotent(
        41, 10**1000, user_id="alice", idempotency_key=KEY,
        request_fingerprint=_fingerprint(10**1000),
    )

    assert result == (400, {"error": "days_out_of_range"})
    assert state["tasks"][41] == original_task
    assert state["events"] == []
    assert state["idempotency"] == {}
    assert not any(sql.startswith(("UPDATE tasks", "INSERT INTO task_events", "INSERT INTO task_postpone_idempotency")) for sql, _params in state["query_log"])


def test_mysql_datetime_max_boundary_and_unoccupied_out_of_range_key(monkeypatch):
    store, state = _simulated_mysql_store(monkeypatch)
    due = datetime.max.replace(tzinfo=timezone.utc) - timedelta(days=1)
    state["tasks"][41]["due_at"] = due.isoformat()

    too_far = store.postpone_task_idempotent(
        41, 2, user_id="alice", idempotency_key=KEY,
        request_fingerprint=_fingerprint(2),
    )

    assert too_far == (400, {"error": "days_out_of_range"})
    assert state["tasks"][41]["due_at"] == due.isoformat()
    assert state["events"] == []
    assert state["idempotency"] == {}

    exactly_max = store.postpone_task_idempotent(
        41, 1, user_id="alice", idempotency_key=KEY,
        request_fingerprint=_fingerprint(1),
    )

    assert exactly_max[0] == 200
    assert state["tasks"][41]["due_at"] == datetime.max.replace(tzinfo=timezone.utc).isoformat()
    assert len(state["events"]) == 1
    assert len(state["idempotency"]) == 1


def test_mysql_existing_key_replay_precedes_eligibility_and_float_conflicts(monkeypatch):
    store, state = _simulated_mysql_store(monkeypatch)

    first = store.postpone_task_idempotent(
        41, 1, user_id="alice", idempotency_key=KEY, request_fingerprint=FINGERPRINT
    )
    due_after_success = state["tasks"][41]["due_at"]
    state["tasks"][41]["status"] = "done"
    replay = store.postpone_task_idempotent(
        41, 1, user_id="alice", idempotency_key=KEY, request_fingerprint=FINGERPRINT
    )
    float_conflict = store.postpone_task_idempotent(
        41, 1.0, user_id="alice", idempotency_key=KEY,
        request_fingerprint=_fingerprint(1.0),
    )

    assert replay == first
    assert float_conflict == (409, {"error": "idempotency_conflict"})
    assert state["tasks"][41]["due_at"] == due_after_success
    assert state["tasks"][41]["status"] == "done"
    assert len(state["events"]) == 1
    assert len(state["idempotency"]) == 1


@pytest.mark.parametrize(
    "case,expected_status,expected_error",
    [
        ("missing", 404, "没有找到这个任务。"),
        ("no_due", 409, "该任务当前无法顺延截止日。"),
        ("ineligible", 409, "该任务当前无法顺延截止日。"),
    ],
)
def test_mysql_300_digit_days_cache_and_replay_deterministic_errors(
    monkeypatch, case, expected_status, expected_error
):
    store, state = _simulated_mysql_store(monkeypatch)
    huge_days = 10**300
    if case == "missing":
        state["tasks"].pop(41)
        original_task = None
    else:
        if case == "no_due":
            state["tasks"][41]["due_at"] = None
        else:
            state["tasks"][41]["status"] = "done"
        original_task = deepcopy(state["tasks"][41])

    first = store.postpone_task_idempotent(
        41, huge_days, user_id="alice", idempotency_key=KEY,
        request_fingerprint=_fingerprint(huge_days),
    )
    replay = store.postpone_task_idempotent(
        41, huge_days, user_id="alice", idempotency_key=KEY,
        request_fingerprint=_fingerprint(huge_days),
    )

    expected = (expected_status, {"error": expected_error})
    assert first == replay == expected
    assert state["tasks"].get(41) == original_task
    assert state["events"] == []
    assert len(state["idempotency"]) == 1
    saved = state["idempotency"][("alice", KEY)]
    assert saved["request_fingerprint"] == _fingerprint(huge_days)
    assert len(saved["request_fingerprint"]) == 64
    assert saved["response_status"] == expected_status
    assert json.loads(saved["response_json"]) == expected[1]
    queries = [sql for sql, _params in state["query_log"]]
    assert not any(sql.startswith(("UPDATE tasks", "INSERT INTO task_events")) for sql in queries)
    assert sum(sql.startswith("INSERT INTO task_postpone_idempotency") for sql in queries) == 1


def test_mysql_postpone_failure_rolls_back_and_same_key_can_retry(monkeypatch):
    store, state = _simulated_mysql_store(monkeypatch, fail_after_event=True)
    original_due = state["tasks"][41]["due_at"]

    try:
        store.postpone_task_idempotent(
            41, 1, user_id="alice", idempotency_key=KEY, request_fingerprint=FINGERPRINT
        )
    except RuntimeError as exc:
        assert str(exc) == "simulated MySQL event write failure"
    else:
        raise AssertionError("expected simulated transaction failure")

    assert state["tasks"][41]["due_at"] == original_due
    assert state["events"] == []
    assert state["idempotency"] == {}
    assert state["rollbacks"] >= 1

    retried = store.postpone_task_idempotent(
        41, 1, user_id="alice", idempotency_key=KEY, request_fingerprint=FINGERPRINT
    )

    assert retried[0] == 200
    assert len(state["events"]) == 1
    assert len(state["idempotency"]) == 1
    assert state["tasks"][41]["due_at"] == (
        datetime(2026, 1, 2, 15, 30, tzinfo=timezone.utc) + timedelta(days=1)
    ).isoformat()
