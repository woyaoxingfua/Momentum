from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from threading import Barrier, Lock

import pytest

from momentum_agent.models import TaskStatus
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.storage.mysql import MySQLTaskStore, SCHEMA as MYSQL_SCHEMA
from momentum_agent.web.handlers import handle_done_task


KEY = "00000000-0000-4000-8000-000000000001"
OTHER_KEY = "00000000-0000-4000-8000-000000000002"
THIRD_KEY = "00000000-0000-4000-8000-000000000003"
DUE = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)


class DirectDoneHandler:
    def __init__(self, store, *, key=KEY):
        self.store = store
        self.headers = {} if key is None else {"Idempotency-Key": key}
        self.status = None
        self.body = None

    def send_json(self, payload, status=HTTPStatus.OK):
        self.status = int(status)
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _fingerprint(task_id):
    canonical = json.dumps(
        {"method": "POST", "task_id": task_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _call(store, task_id, *, user_id="alice", key=KEY):
    handler = DirectDoneHandler(store, key=key)
    handle_done_task(handler, f"/api/tasks/{task_id}/done", user_id)
    return handler.status, handler.body, json.loads(handler.body)


def _count(store, table, where="", params=()):
    with store._connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where}", params).fetchone()
    return int(row["n"])


def _events(store, task_id, event_type=None):
    where = "WHERE task_id = ?"
    params = [task_id]
    if event_type is not None:
        where += " AND event_type = ?"
        params.append(event_type)
    with store._connect() as conn:
        return conn.execute(
            f"SELECT id, event_type, payload FROM task_events {where} ORDER BY id", params
        ).fetchall()


@pytest.fixture
def store(tmp_path):
    return SQLiteTaskStore(tmp_path / "done-idempotency.sqlite3")


def _new_recurring(store, title="daily", *, user_id="alice"):
    return store.create_task(title, due_at=DUE, recurrence="daily", user_id=user_id)


@pytest.mark.parametrize(
    "key,expected",
    [(None, "idempotency_key_required"), ("not-a-uuid", "idempotency_key_invalid"),
     ("00000000-0000-1000-8000-000000000001", "idempotency_key_invalid")],
    ids=["missing", "malformed", "not-v4"],
)
def test_done_requires_a_uuid_v4_header_without_writes(store, key, expected):
    task = _new_recurring(store)
    handler = DirectDoneHandler(store, key=key)

    handle_done_task(handler, f"/api/tasks/{task.id}/done", "alice")

    assert handler.status == HTTPStatus.BAD_REQUEST
    assert json.loads(handler.body) == {"error": expected}
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.TODO
    assert _count(store, "task_done_idempotency") == 0
    assert _count(store, "task_done_occurrences") == 0
    assert _events(store, task.id, "status_changed") == []


def test_first_recurring_done_persists_one_event_next_mapping_and_canonical_fingerprint(store):
    task = _new_recurring(store, "daily review")

    status, body, payload = _call(store, task.id)

    assert status == HTTPStatus.OK
    assert payload["message"].startswith("已创建下一期任务 #")
    assert body == json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.DONE
    done_events = _events(store, task.id, "status_changed")
    assert len(done_events) == 1
    assert done_events[0]["payload"] == "done"
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", ("alice", "daily review")) == 2
    assert _count(store, "task_done_occurrences") == 1
    assert _count(store, "task_done_idempotency") == 1
    with store._connect() as conn:
        ledger = conn.execute(
            "SELECT request_fingerprint, response_status, response_json FROM task_done_idempotency WHERE user_id = ? AND idempotency_key = ?",
            ("alice", KEY),
        ).fetchone()
        occurrence = conn.execute("SELECT * FROM task_done_occurrences").fetchone()
    assert ledger["request_fingerprint"] == _fingerprint(task.id)
    assert ledger["response_status"] == 200
    assert json.loads(ledger["response_json"]) == payload
    assert occurrence["user_id"] == "alice"
    assert occurrence["source_task_id"] == task.id
    assert occurrence["source_done_event_id"] == done_events[0]["id"]
    assert occurrence["next_task_id"] is not None
    assert occurrence["response_status"] == 200
    assert json.loads(occurrence["response_json"]) == payload


def test_same_key_same_request_replays_exact_status_and_body_after_response_loss(store):
    task = _new_recurring(store, "response lost")

    first = _call(store, task.id)
    # Treat first response as lost: the client only retries after commit.
    retry = _call(store, task.id)

    assert retry == first
    assert _events(store, task.id, "status_changed")[0]["payload"] == "done"
    assert _count(store, "tasks", "WHERE user_id = ?", ("alice",)) == 2
    assert _count(store, "task_done_occurrences") == 1
    assert _count(store, "task_done_idempotency") == 1


def test_same_key_different_task_conflicts_and_ledger_is_user_scoped(store):
    first_task = _new_recurring(store, "first")
    second_task = _new_recurring(store, "second")
    first = _call(store, first_task.id, user_id="alice", key=KEY)
    conflict = _call(store, second_task.id, user_id="alice", key=KEY)

    assert first[0] == HTTPStatus.OK
    assert conflict[0] == HTTPStatus.CONFLICT
    assert conflict[2] == {"error": "idempotency_conflict"}
    assert store.get_task_for_user(second_task.id, "alice").status == TaskStatus.TODO
    assert _events(store, second_task.id, "status_changed") == []
    assert _count(store, "task_done_idempotency", "WHERE user_id = ?", ("alice",)) == 1

    # The same UUID is independent for another authenticated user.
    bob_task = _new_recurring(store, "bob task", user_id="bob")
    bob = _call(store, bob_task.id, user_id="bob", key=KEY)
    assert bob[0] == HTTPStatus.OK
    assert _count(store, "task_done_idempotency", "WHERE user_id = ?", ("bob",)) == 1
    assert _events(store, bob_task.id, "status_changed")[0]["payload"] == "done"


def test_foreign_task_and_missing_task_return_same_generic_404(store):
    foreign = _new_recurring(store, "private title", user_id="bob")
    foreign_result = _call(store, foreign.id, user_id="alice", key=KEY)
    missing_result = _call(store, foreign.id + 100, user_id="alice", key=OTHER_KEY)

    assert foreign_result[0] == missing_result[0] == HTTPStatus.NOT_FOUND
    assert foreign_result[2] == missing_result[2] == {"error": "没有找到这个任务。"}
    assert "private title" not in foreign_result[1].decode("utf-8")
    assert _events(store, foreign.id, "status_changed") == []
    assert _count(store, "task_done_idempotency") == 0

    alice_task = _new_recurring(store, "alice own task")
    same_key_owner_result = _call(store, alice_task.id, user_id="alice", key=KEY)
    assert same_key_owner_result[0] == HTTPStatus.OK
    with store._connect() as conn:
        alice_key = conn.execute(
            "SELECT user_id, idempotency_key FROM task_done_idempotency WHERE user_id = ? AND idempotency_key = ?",
            ("alice", KEY),
        ).fetchone()
    assert tuple(alice_key) == ("alice", KEY)
    assert len(_events(store, alice_task.id, "status_changed")) == 1


def test_same_key_concurrent_requests_replay_one_result(store):
    task = _new_recurring(store, "parallel same key")
    count = 8
    barrier = Barrier(count)

    def send(_):
        barrier.wait(timeout=10)
        return _call(store, task.id, key=KEY)

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(send, range(count)))

    assert all(result == results[0] for result in results)
    assert results[0][0] == HTTPStatus.OK
    assert len(_events(store, task.id, "status_changed")) == 1
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", ("alice", "parallel same key")) == 2
    assert _count(store, "task_done_occurrences") == 1
    assert _count(store, "task_done_idempotency") == 1


def test_different_keys_competing_for_same_occurrence_create_at_most_one_next(store):
    task = _new_recurring(store, "parallel different keys")
    keys = [KEY, OTHER_KEY, THIRD_KEY]
    barrier = Barrier(len(keys))

    def send(key):
        barrier.wait(timeout=10)
        return _call(store, task.id, key=key)

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        results = list(pool.map(send, keys))

    assert all(status == HTTPStatus.OK for status, _, _ in results)
    assert len(_events(store, task.id, "status_changed")) == 1
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", ("alice", "parallel different keys")) == 2
    assert _count(store, "task_done_occurrences") == 1
    assert _count(store, "task_done_idempotency") == len(keys)
    assert sum("已创建下一期任务" in result[2]["message"] for result in results) == 1
    assert sum(result[2] == {"message": "任务已完成。"} for result in results) == len(keys) - 1


def test_reopen_then_new_key_creates_a_new_done_event_and_next_but_old_key_replays(store):
    task = _new_recurring(store, "new occurrence")
    first = _call(store, task.id, key=KEY)
    done_event_ids = [row["id"] for row in _events(store, task.id, "status_changed")]
    store.reopen_task(task.id, user_id="alice")

    old_key_replay = _call(store, task.id, key=KEY)
    assert old_key_replay == first
    assert [row["id"] for row in _events(store, task.id, "status_changed") if row["payload"] == "done"] == done_event_ids

    second = _call(store, task.id, key=OTHER_KEY)
    second_done_ids = [row["id"] for row in _events(store, task.id, "status_changed") if row["payload"] == "done"]
    assert second[0] == HTTPStatus.OK
    assert len(second_done_ids) == 2
    assert second_done_ids[0] != second_done_ids[1]
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", ("alice", "new occurrence")) == 3
    assert _count(store, "task_done_occurrences") == 2
    assert _count(store, "task_done_idempotency") == 2
    with store._connect() as conn:
        mapped_ids = [row[0] for row in conn.execute(
            "SELECT source_done_event_id FROM task_done_occurrences WHERE source_task_id = ? ORDER BY source_done_event_id",
            (task.id,),
        )]
    assert mapped_ids == second_done_ids


def test_new_key_against_done_or_legacy_done_without_mapping_is_a_cached_noop(store):
    task = _new_recurring(store, "already done")
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    before_events = _events(store, task.id)
    before_tasks = _count(store, "tasks", "WHERE user_id = ?", ("alice",))

    result = _call(store, task.id, key=KEY)
    replay = _call(store, task.id, key=KEY)

    assert result == replay
    assert result[0] == HTTPStatus.OK
    assert result[2] == {"message": "任务已完成。"}
    assert _events(store, task.id) == before_events
    assert _count(store, "tasks", "WHERE user_id = ?", ("alice",)) == before_tasks
    assert _count(store, "task_done_occurrences") == 0
    assert _count(store, "task_done_idempotency") == 1


def test_ordinary_task_repeated_done_writes_one_transition_mapping_with_null_next(store):
    task = store.create_task("ordinary", user_id="alice")
    first = _call(store, task.id, key=KEY)
    second_key = _call(store, task.id, key=OTHER_KEY)
    replay = _call(store, task.id, key=KEY)

    assert first[0] == second_key[0] == HTTPStatus.OK
    assert first[2] == {"message": f"已完成任务 #{task.id}：ordinary"}
    assert second_key[2] == {"message": "任务已完成。"}
    assert replay == first
    assert len(_events(store, task.id, "status_changed")) == 1
    assert _count(store, "task_done_occurrences") == 1
    with store._connect() as conn:
        mapping = conn.execute("SELECT next_task_id FROM task_done_occurrences").fetchone()
    assert mapping["next_task_id"] is None


def test_done_parent_cascades_all_descendants_but_only_target_creates_next(store):
    parent = store.create_task("recurring parent", due_at=DUE, recurrence="daily", user_id="alice")
    child = store.create_task(
        "recurring child", due_at=DUE, recurrence="weekly", parent_task_id=parent.id, user_id="alice"
    )
    grandchild = store.create_task(
        "recurring grandchild", due_at=DUE, recurrence="weekly", parent_task_id=child.id, user_id="alice"
    )

    result = _call(store, parent.id, key=KEY)

    assert result[0] == HTTPStatus.OK
    assert "recurring parent" in result[2]["message"]
    assert all(store.get_task_for_user(task.id, "alice").status == TaskStatus.DONE
               for task in (parent, child, grandchild))
    for task in (parent, child, grandchild):
        assert [event["payload"] for event in _events(store, task.id, "status_changed")] == ["done"]
    assert len(_events(store, parent.id, "subtasks_completed")) == 1
    assert len(_events(store, child.id, "subtasks_completed")) == 1
    assert _events(store, grandchild.id, "subtasks_completed") == []
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", ("alice", "recurring parent")) == 2
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", ("alice", "recurring child")) == 1
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", ("alice", "recurring grandchild")) == 1
    with store._connect() as conn:
        mappings = conn.execute("SELECT source_task_id, next_task_id FROM task_done_occurrences").fetchall()
    assert len(mappings) == 1
    assert mappings[0]["source_task_id"] == parent.id
    assert mappings[0]["next_task_id"] is not None


def test_done_last_child_completes_recurring_parent_without_parent_next(store):
    parent = store.create_task("recurring parent", due_at=DUE, recurrence="daily", user_id="alice")
    already_done = store.create_task("already done child", parent_task_id=parent.id, user_id="alice")
    last_child = store.create_task(
        "recurring last child", due_at=DUE, recurrence="weekly", parent_task_id=parent.id, user_id="alice"
    )
    store.update_status(already_done.id, TaskStatus.DONE, user_id="alice")
    assert store.get_task_for_user(parent.id, "alice").status == TaskStatus.TODO

    result = _call(store, last_child.id, key=KEY)

    assert result[0] == HTTPStatus.OK
    assert store.get_task_for_user(last_child.id, "alice").status == TaskStatus.DONE
    assert store.get_task_for_user(parent.id, "alice").status == TaskStatus.DONE
    assert [event["payload"] for event in _events(store, last_child.id, "status_changed")] == ["done"]
    assert [event["payload"] for event in _events(store, parent.id, "status_changed")] == ["done"]
    assert len(_events(store, parent.id, "subtasks_completed")) == 0
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", ("alice", "recurring parent")) == 1
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", ("alice", "recurring last child")) == 2
    with store._connect() as conn:
        mapping = conn.execute("SELECT source_task_id, next_task_id FROM task_done_occurrences").fetchone()
    assert mapping["source_task_id"] == last_child.id
    assert mapping["next_task_id"] is not None


def test_sqlite_migration_preserves_legacy_completed_recurring_without_backfill(tmp_path):
    path = tmp_path / "legacy-done.sqlite3"
    user_id = f"legacy-{tmp_path.name}"
    title = f"legacy recurring {tmp_path.name}"
    task_id = 17
    task_columns = (
        "id, title, status, priority, due_at, estimated_minutes, notes, "
        "parent_task_id, recurrence, user_id, created_at, updated_at, tags"
    )
    user_columns = "id, display_name, password_hash, created_at"
    event_columns = "id, task_id, event_type, payload, created_at"
    legacy_task = (
        task_id, title, "done", "high", "2026-01-02T12:00:00+00:00", 25, "keep notes",
        None, "daily", user_id, "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00", '["legacy"]',
    )
    legacy_user = (user_id, "Legacy User", "legacy-password-hash", "2026-01-01T00:00:00+00:00")
    legacy_event = (29, task_id, "status_changed", "done", "2026-01-01T00:00:00+00:00")

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY, display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
            priority TEXT NOT NULL, due_at TEXT, estimated_minutes INTEGER,
            notes TEXT, parent_task_id INTEGER, recurrence TEXT,
            user_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, tags TEXT
        );
        CREATE TABLE sessions (
            token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY, task_id INTEGER, event_type TEXT NOT NULL,
            payload TEXT, created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
        legacy_user,
    )
    conn.execute(
        f"INSERT INTO tasks ({task_columns}) VALUES ({', '.join('?' for _ in legacy_task)})",
        legacy_task,
    )
    conn.execute(
        "INSERT INTO task_events (id, task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        legacy_event,
    )
    legacy_tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "task_done_idempotency" not in legacy_tables
    assert "task_done_occurrences" not in legacy_tables
    legacy_columns = {
        table: tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))
        for table in ("users", "tasks", "sessions", "task_events")
    }
    assert {"recurrence", "user_id"} <= set(legacy_columns["tasks"])
    conn.commit()
    conn.close()

    migrated = SQLiteTaskStore(path)

    def assert_legacy_rows_unchanged(expected_ledger_count):
        with migrated._connect() as db:
            tables = {
                row["name"]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert {"task_done_idempotency", "task_done_occurrences"} <= tables
            migrated_columns = {
                table: tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})"))
                for table in legacy_columns
            }
            assert migrated_columns == legacy_columns
            assert tuple(db.execute(f"SELECT {user_columns} FROM users").fetchone()) == legacy_user
            assert tuple(db.execute(f"SELECT {task_columns} FROM tasks WHERE id = ?", (task_id,)).fetchone()) == legacy_task
            assert [tuple(row) for row in db.execute(f"SELECT {event_columns} FROM task_events ORDER BY id")] == [legacy_event]
            assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
            assert db.execute("SELECT id FROM tasks WHERE id != ?", (task_id,)).fetchall() == []
            assert db.execute(
                "SELECT COUNT(*) FROM task_events WHERE event_type = 'status_changed' AND payload = 'done'"
            ).fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM task_done_idempotency").fetchone()[0] == expected_ledger_count
            assert db.execute("SELECT COUNT(*) FROM task_done_occurrences").fetchone()[0] == 0

    # Stage A: schema upgrade creates the ledgers but does not infer a historical occurrence.
    assert_legacy_rows_unchanged(expected_ledger_count=0)

    # Stage B: a valid fresh key gets the stable already-done response and only a request ledger row.
    expected_payload = {"message": "任务已完成。"}
    expected_body = json.dumps(expected_payload, ensure_ascii=False).encode("utf-8")
    first = _call(migrated, task_id, user_id=user_id, key=KEY)
    assert first == (HTTPStatus.OK, expected_body, expected_payload)
    assert_legacy_rows_unchanged(expected_ledger_count=1)
    with migrated._connect() as db:
        ledger = db.execute(
            "SELECT user_id, idempotency_key, request_fingerprint, response_status, response_json "
            "FROM task_done_idempotency"
        ).fetchone()
    assert tuple(ledger) == (
        user_id, KEY, _fingerprint(task_id), HTTPStatus.OK,
        json.dumps(expected_payload, ensure_ascii=False),
    )

    replay = _call(migrated, task_id, user_id=user_id, key=KEY)
    assert replay == first
    assert_legacy_rows_unchanged(expected_ledger_count=1)
    with migrated._connect() as db:
        replayed_ledger = db.execute(
            "SELECT user_id, idempotency_key, request_fingerprint, response_status, response_json "
            "FROM task_done_idempotency"
        ).fetchone()
    assert tuple(replayed_ledger) == tuple(ledger)


@pytest.mark.parametrize("failure_point", ["event", "next", "mapping", "ledger"])
def test_sqlite_write_failure_at_each_done_stage_rolls_back_everything(store, failure_point):
    task = _new_recurring(store, f"failure-{failure_point}")
    with store._connect() as conn:
        if failure_point == "event":
            conn.execute(
                "CREATE TRIGGER inject_done_event_failure BEFORE INSERT ON task_events "
                "WHEN NEW.task_id = %d AND NEW.event_type = 'status_changed' AND NEW.payload = 'done' "
                "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END" % task.id
            )
        elif failure_point == "next":
            conn.execute(
                "CREATE TRIGGER inject_next_task_failure BEFORE INSERT ON tasks "
                "WHEN NEW.title = '%s' AND NEW.status = 'todo' "
                "BEGIN SELECT RAISE(ABORT, 'injected next failure'); END" % f"failure-{failure_point}"
            )
        elif failure_point == "mapping":
            conn.execute(
                "CREATE TRIGGER inject_mapping_failure BEFORE INSERT ON task_done_occurrences "
                "BEGIN SELECT RAISE(ABORT, 'injected mapping failure'); END"
            )
        else:
            conn.execute(
                "CREATE TRIGGER inject_ledger_failure BEFORE INSERT ON task_done_idempotency "
                "BEGIN SELECT RAISE(ABORT, 'injected ledger failure'); END"
            )

    with pytest.raises(sqlite3.IntegrityError):
        _call(store, task.id, key=KEY)

    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.TODO
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", ("alice", f"failure-{failure_point}")) == 1
    assert _events(store, task.id, "status_changed") == []
    assert _count(store, "task_done_occurrences") == 0
    assert _count(store, "task_done_idempotency") == 0


# Pure in-memory MySQL simulation. No test below calls pymysql.connect or contacts a service.
def _mock_mysql_store(monkeypatch, *, recurring=True, fail_at=None, duplicate_ledger=None):
    store = MySQLTaskStore.__new__(MySQLTaskStore)
    task_id = 41
    state = {
        "tasks": {
            task_id: {
                "id": task_id, "title": "mysql task", "status": "todo", "priority": "medium",
                "due_at": DUE.isoformat(), "estimated_minutes": 20, "notes": None,
                "parent_task_id": None, "recurrence": "daily" if recurring else None,
                "user_id": "alice", "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00", "tags": None,
            }
        },
        "events": [], "occurrences": {}, "ledger": {}, "queries": [],
        "next_task_id": 100, "next_event_id": 500, "commits": 0, "rollbacks": 0,
        "fail_at": fail_at, "duplicate_ledger": duplicate_ledger,
        "external_winner": duplicate_ledger,
        "named_locks": {}, "row_locks": {}, "locks_guard": Lock(),
        "row_lock_queries": 0,
    }

    class Cursor:
        def __init__(self, *, one=None, rows=None, rowcount=0):
            self.one = one
            self.rows = rows or []
            self.rowcount = rowcount

        def fetchone(self):
            return self.one.copy() if isinstance(self.one, dict) else self.one

        def fetchall(self):
            return [row.copy() if isinstance(row, dict) else row for row in self.rows]

    class Connection:
        def __init__(self):
            self.snapshot = None
            self.last_insert_id = 0
            self.held_named_lock = None
            self.held_row_lock = None

        def begin(self):
            self.snapshot = deepcopy({
                name: state[name]
                for name in ("tasks", "events", "occurrences", "ledger", "next_task_id", "next_event_id")
            })

        def insert_id(self):
            return self.last_insert_id

        def commit(self):
            if self.snapshot is not None:
                self.snapshot = None
                state["commits"] += 1

        def rollback(self):
            if self.snapshot is not None:
                state["rollbacks"] += 1
                for name, value in self.snapshot.items():
                    state[name] = value
                if state["external_winner"] is not None:
                    winner = state["external_winner"]
                    state["ledger"][(winner["user_id"], winner["idempotency_key"])] = {
                        "request_fingerprint": winner["request_fingerprint"],
                        "response_status": winner["response_status"],
                        "response_json": winner["response_json"],
                    }
                self.snapshot = None

        def close(self):
            if self.held_row_lock is not None:
                self.held_row_lock.release()
                self.held_row_lock = None
            if self.held_named_lock is not None:
                self.held_named_lock.release()
                self.held_named_lock = None

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

    def _lock_for(collection, key):
        with state["locks_guard"]:
            return collection.setdefault(key, Lock())

    def fake_execute(conn, sql, params=None):
        normalized = " ".join(sql.split())
        params = tuple(params or ())
        state["queries"].append((normalized, params))

        if normalized.startswith("SELECT GET_LOCK("):
            name, timeout = params
            lock = _lock_for(state["named_locks"], name)
            acquired = lock.acquire(timeout=timeout)
            if acquired:
                conn.held_named_lock = lock
            return Cursor(one={"acquired": int(acquired)})
        if normalized.startswith("SELECT RELEASE_LOCK("):
            if conn.held_named_lock is not None:
                conn.held_named_lock.release()
                conn.held_named_lock = None
            return Cursor(one={"released": 1})
        if normalized.startswith("SELECT request_fingerprint, response_status, response_json FROM task_done_idempotency"):
            user_id, key = params
            return Cursor(one=state["ledger"].get((user_id, key)))
        if normalized.startswith("SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE"):
            task_id_, user_id = params
            row = state["tasks"].get(task_id_)
            return Cursor(one={"parent_task_id": row["parent_task_id"]} if row and row["user_id"] == user_id else None)
        if normalized.startswith("SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s"):
            task_id_, user_id = params
            row = state["tasks"].get(task_id_)
            return Cursor(one={"parent_task_id": row["parent_task_id"]} if row and row["user_id"] == user_id else None)
        if normalized.startswith("SELECT id FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE"):
            task_id_, user_id = params
            row = state["tasks"].get(task_id_)
            return Cursor(one={"id": task_id_} if row and row["user_id"] == user_id else None)
        if normalized.startswith("SELECT * FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE"):
            task_id_, user_id = params
            lock = _lock_for(state["row_locks"], task_id_)
            lock.acquire(timeout=10)
            conn.held_row_lock = lock
            state["row_lock_queries"] += 1
            row = state["tasks"].get(task_id_)
            return Cursor(one=row if row and row["user_id"] == user_id else None)
        if normalized.startswith("SELECT id FROM tasks WHERE parent_task_id = %s AND status != %s AND user_id = %s ORDER BY id FOR UPDATE"):
            parent_id, done_status, user_id = params
            return Cursor(rows=[
                {"id": row["id"]}
                for row in state["tasks"].values()
                if row["parent_task_id"] == parent_id and row["status"] != done_status and row["user_id"] == user_id
            ])
        if normalized.startswith("SELECT status FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE"):
            task_id_, user_id = params
            row = state["tasks"].get(task_id_)
            return Cursor(one={"status": row["status"]} if row and row["user_id"] == user_id else None)
        if normalized.startswith("SELECT status FROM tasks WHERE parent_task_id = %s AND user_id = %s FOR UPDATE"):
            parent_id, user_id = params
            return Cursor(rows=[
                {"status": row["status"]}
                for row in state["tasks"].values()
                if row["parent_task_id"] == parent_id and row["user_id"] == user_id
            ])
        if normalized.startswith("UPDATE tasks SET status = %s"):
            status, updated_at, task_id_ = params[:3]
            user_id = params[-1]
            row = state["tasks"].get(task_id_)
            if row and row["user_id"] == user_id and row["status"] != status:
                row["status"] = status
                row["updated_at"] = updated_at
                return Cursor(rowcount=1)
            return Cursor(rowcount=0)
        if normalized.startswith("INSERT INTO task_events"):
            state["events"].append(params)
            conn.last_insert_id = state["next_event_id"]
            state["next_event_id"] += 1
            if state["fail_at"] == "event":
                state["fail_at"] = None
                raise RuntimeError("injected MySQL event failure")
            return Cursor(rowcount=1)
        if normalized.startswith("SELECT next_task_id, response_status, response_json FROM task_done_occurrences"):
            return Cursor(one=state["occurrences"].get(params))
        if normalized.startswith("INSERT INTO tasks ("):
            (title, status, priority, due_at, estimated_minutes, notes, parent_task_id,
             recurrence, user_id, created_at, updated_at, tags) = params
            task_id_ = state["next_task_id"]
            state["next_task_id"] += 1
            state["tasks"][task_id_] = {
                "id": task_id_, "title": title, "status": status, "priority": priority,
                "due_at": due_at, "estimated_minutes": estimated_minutes, "notes": notes,
                "parent_task_id": parent_task_id, "recurrence": recurrence, "user_id": user_id,
                "created_at": created_at, "updated_at": updated_at, "tags": tags,
            }
            conn.last_insert_id = task_id_
            if state["fail_at"] == "next":
                state["fail_at"] = None
                raise RuntimeError("injected MySQL next-task failure")
            return Cursor(rowcount=1)
        if normalized.startswith("INSERT INTO task_done_occurrences"):
            user_id, source_id, event_id, next_id, status, response_json, created_at = params
            state["occurrences"][(user_id, source_id, event_id)] = {
                "next_task_id": next_id, "response_status": status, "response_json": response_json,
            }
            if state["fail_at"] == "mapping":
                state["fail_at"] = None
                raise RuntimeError("injected MySQL mapping failure")
            return Cursor(rowcount=1)
        if normalized.startswith("INSERT INTO task_done_idempotency"):
            user_id, key, fingerprint, status, response_json, created_at = params
            if state["duplicate_ledger"] is not None:
                raise Exception(1062, "simulated duplicate ledger key")
            state["ledger"][(user_id, key)] = {
                "request_fingerprint": fingerprint, "response_status": status,
                "response_json": response_json,
            }
            if state["fail_at"] == "ledger":
                state["fail_at"] = None
                raise RuntimeError("injected MySQL ledger failure")
            return Cursor(rowcount=1)
        raise AssertionError(f"unexpected SQL in MySQL simulation: {normalized}")

    monkeypatch.setattr(store, "_connect", fake_connect)
    monkeypatch.setattr(store, "_execute", fake_execute)
    return store, state


def test_mysql_mock_uses_innodb_key_lock_row_lock_commits_and_replays(monkeypatch):
    store, state = _mock_mysql_store(monkeypatch)

    first = store.complete_task_idempotent(41, user_id="alice", idempotency_key=KEY, request_fingerprint=_fingerprint(41))
    replay = store.complete_task_idempotent(41, user_id="alice", idempotency_key=KEY, request_fingerprint=_fingerprint(41))

    assert first == replay
    assert first[0] == 200 and "已创建下一期任务" in first[1]["message"]
    assert len(state["events"]) == 2  # source done plus next-task created
    assert len(state["tasks"]) == 2
    assert len(state["occurrences"]) == len(state["ledger"]) == 1
    assert state["commits"] == 2
    assert state["rollbacks"] == 0
    queries = [sql for sql, _ in state["queries"]]
    assert "ENGINE=InnoDB" in MYSQL_SCHEMA
    assert any(sql.startswith("SELECT GET_LOCK(") for sql in queries)
    assert any(sql.startswith("SELECT RELEASE_LOCK(") for sql in queries)
    assert any("FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE" in sql for sql in queries)
    assert any("task_done_idempotency" in sql and "FOR UPDATE" in sql for sql in queries)


def test_mysql_mock_foreign_or_missing_done_does_not_consume_key(monkeypatch):
    store, state = _mock_mysql_store(monkeypatch)
    state["tasks"][41]["user_id"] = "bob"
    before_tasks = deepcopy(state["tasks"])
    before_queries = len(state["queries"])

    foreign = store.complete_task_idempotent(
        41, user_id="alice", idempotency_key=KEY, request_fingerprint=_fingerprint(41)
    )
    missing = store.complete_task_idempotent(
        999, user_id="alice", idempotency_key=OTHER_KEY, request_fingerprint=_fingerprint(999)
    )

    assert foreign == missing == (HTTPStatus.NOT_FOUND, {"error": "没有找到这个任务。"})
    assert state["tasks"] == before_tasks
    assert state["events"] == []
    assert state["occurrences"] == {}
    assert state["ledger"] == {}
    rejected_queries = state["queries"][before_queries:]
    ledger_check = next(
        index for index, (sql, _params) in enumerate(rejected_queries)
        if sql.startswith("SELECT request_fingerprint, response_status, response_json FROM task_done_idempotency")
    )
    owner_check = next(
        index for index, (sql, _params) in enumerate(rejected_queries)
        if sql.startswith("SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s")
    )
    assert ledger_check < owner_check
    assert not any(sql.startswith("INSERT INTO task_done_idempotency") for sql, _params in rejected_queries)

    state["tasks"][41]["user_id"] = "alice"
    owner_result = store.complete_task_idempotent(
        41, user_id="alice", idempotency_key=KEY, request_fingerprint=_fingerprint(41)
    )
    assert owner_result[0] == HTTPStatus.OK
    assert state["ledger"][("alice", KEY)]["response_status"] == HTTPStatus.OK
    assert len(state["events"]) == 2
    assert len(state["occurrences"]) == 1


def test_mysql_mock_concurrent_same_key_rechecks_ledger_under_lock(monkeypatch):
    store, state = _mock_mysql_store(monkeypatch)
    count = 8
    barrier = Barrier(count)

    def call(_):
        barrier.wait(timeout=10)
        return store.complete_task_idempotent(41, user_id="alice", idempotency_key=KEY, request_fingerprint=_fingerprint(41))

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(call, range(count)))

    assert all(result == results[0] for result in results)
    assert len(state["events"]) == 2
    assert len(state["occurrences"]) == len(state["ledger"]) == 1
    assert sum(sql.startswith("SELECT * FROM tasks") for sql, _ in state["queries"]) == 1
    assert state["row_lock_queries"] == 1
    assert state["rollbacks"] == 0


def test_mysql_mock_different_keys_race_on_one_task_make_one_next(monkeypatch):
    store, state = _mock_mysql_store(monkeypatch)
    keys = [KEY, OTHER_KEY, THIRD_KEY]
    barrier = Barrier(len(keys))

    def call(key):
        barrier.wait(timeout=10)
        return store.complete_task_idempotent(41, user_id="alice", idempotency_key=key, request_fingerprint=_fingerprint(41))

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        results = list(pool.map(call, keys))

    assert all(status == 200 for status, _ in results)
    assert len(state["tasks"]) == 2
    assert len([event for event in state["events"] if event[1:3] == ("status_changed", "done")]) == 1
    assert len(state["occurrences"]) == 1
    assert len(state["ledger"]) == len(keys)
    assert sum("已创建下一期任务" in response["message"] for _, response in results) == 1
    assert sum(response == {"message": "任务已完成。"} for _, response in results) == len(keys) - 1
    assert state["row_lock_queries"] == len(keys)


def test_mysql_mock_done_cascades_descendants_but_creates_next_only_for_target(monkeypatch):
    store, state = _mock_mysql_store(monkeypatch)
    parent = state["tasks"][41]
    parent["title"] = "mysql parent"
    child = deepcopy(parent)
    child.update(id=42, title="mysql child", status="todo", parent_task_id=41, recurrence="weekly")
    grandchild = deepcopy(child)
    grandchild.update(id=43, title="mysql grandchild", parent_task_id=42)
    state["tasks"].update({42: child, 43: grandchild})

    result = store.complete_task_idempotent(41, user_id="alice", idempotency_key=KEY, request_fingerprint=_fingerprint(41))

    assert result[0] == 200 and "mysql parent" in result[1]["message"]
    assert [state["tasks"][task_id]["status"] for task_id in (41, 42, 43)] == ["done", "done", "done"]
    done_events = [event for event in state["events"] if event[1:3] == ("status_changed", "done")]
    assert {event[0] for event in done_events} == {41, 42, 43}
    summaries = [event[0] for event in state["events"] if event[1] == "subtasks_completed"]
    assert summaries == [41, 42]
    assert len(state["tasks"]) == 4
    assert state["tasks"][100]["title"] == "mysql parent"
    assert state["tasks"][100]["recurrence"] == "daily"
    assert sum(task["recurrence"] == "weekly" for task in state["tasks"].values()) == 2
    assert len(state["occurrences"]) == len(state["ledger"]) == 1


def test_mysql_mock_done_last_child_autocompletes_recurring_parent_without_parent_next(monkeypatch):
    store, state = _mock_mysql_store(monkeypatch, recurring=True)
    last_child = state["tasks"][41]
    last_child["title"] = "mysql last child"
    last_child["parent_task_id"] = 40
    last_child["recurrence"] = "weekly"
    parent = deepcopy(last_child)
    parent.update(id=40, title="mysql parent", status="todo", parent_task_id=None, recurrence="daily")
    sibling = deepcopy(last_child)
    sibling.update(id=42, title="mysql completed sibling", status="done", parent_task_id=40, recurrence=None)
    state["tasks"].update({40: parent, 42: sibling})

    result = store.complete_task_idempotent(41, user_id="alice", idempotency_key=KEY, request_fingerprint=_fingerprint(41))

    assert result[0] == 200 and "mysql last child" in result[1]["message"]
    assert state["tasks"][40]["status"] == state["tasks"][41]["status"] == "done"
    done_events = [event for event in state["events"] if event[1:3] == ("status_changed", "done")]
    assert {event[0] for event in done_events} == {40, 41}
    assert len(state["tasks"]) == 4
    assert state["tasks"][100]["title"] == "mysql last child"
    assert state["tasks"][100]["recurrence"] == "weekly"
    assert sum(task["title"] == "mysql parent" for task in state["tasks"].values()) == 1
    assert len(state["occurrences"]) == 1


@pytest.mark.parametrize("failure_point", ["event", "next", "mapping", "ledger"])
def test_mysql_mock_failures_rollback_all_done_writes(monkeypatch, failure_point):
    store, state = _mock_mysql_store(monkeypatch, fail_at=failure_point)

    with pytest.raises(RuntimeError, match="injected MySQL"):
        store.complete_task_idempotent(41, user_id="alice", idempotency_key=KEY, request_fingerprint=_fingerprint(41))

    assert state["tasks"][41]["status"] == "todo"
    assert len(state["tasks"]) == 1
    assert state["events"] == []
    assert state["occurrences"] == {}
    assert state["ledger"] == {}
    assert state["rollbacks"] >= 1


def test_mysql_duplicate_ledger_unique_conflict_rolls_back_then_reads_canonical_row(monkeypatch):
    canonical = {
        "user_id": "alice", "idempotency_key": KEY,
        "request_fingerprint": _fingerprint(999), "response_status": 200,
        "response_json": json.dumps({"message": "winner"}, ensure_ascii=False),
    }
    store, state = _mock_mysql_store(monkeypatch, duplicate_ledger=canonical)

    result = store.complete_task_idempotent(41, user_id="alice", idempotency_key=KEY, request_fingerprint=_fingerprint(41))

    assert result == (409, {"error": "idempotency_conflict"})
    assert state["tasks"][41]["status"] == "todo"
    assert len(state["tasks"]) == 1
    assert state["events"] == []
    assert state["occurrences"] == {}
    assert state["ledger"][("alice", KEY)]["request_fingerprint"] == _fingerprint(999)
    assert state["rollbacks"] >= 1
    assert any(
        sql.startswith("SELECT request_fingerprint") and "task_done_idempotency" in sql
        and "FOR UPDATE" not in sql
        for sql, _ in state["queries"]
    )
