from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from threading import Barrier

import pytest

from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.handlers import handle_postpone_task
from backend_fixtures import fetch_row, fetch_scalar, requires_sqlite


KEY = "00000000-0000-4000-8000-000000000001"
OTHER_KEY = "00000000-0000-4000-8000-000000000002"
_UNSET = object()


def _fingerprint(task_id, days):
    canonical = json.dumps(
        {"method": "POST", "task_id": task_id, "days": days},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class DirectPostponeHandler:
    """Call the real API handler without opening a listening HTTP server."""

    def __init__(self, store, *, user_id, key=None, payload=_UNSET):
        self.store = store
        self.headers = {} if key is None else {"Idempotency-Key": key}
        self.user_id = user_id
        self.payload = {"days": 1} if payload is _UNSET else payload
        self.status = None
        self.body = None

    def read_json(self):
        return self.payload

    def send_json(self, payload, status=HTTPStatus.OK):
        self.status = int(status)
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _call(store, task_id, *, user_id="alice", key=KEY, days=1, payload=_UNSET):
    handler = DirectPostponeHandler(
        store,
        user_id=user_id,
        key=key,
        payload={"days": days} if payload is _UNSET else payload,
    )
    handle_postpone_task(handler, f"/api/tasks/{task_id}/postpone", user_id)
    return handler.status, handler.body, json.loads(handler.body)


def _event_count(store, task_id, event_type=None):
    if event_type is None:
        return int(fetch_scalar(store, "SELECT COUNT(*) FROM task_events WHERE task_id = {ph}", (task_id,)) or 0)
    return int(fetch_scalar(
        store,
        "SELECT COUNT(*) FROM task_events WHERE task_id = {ph} AND event_type = {ph}",
        (task_id, event_type),
    ) or 0)


def _idempotency_count(store, user_id=None):
    if user_id is None:
        return int(fetch_scalar(store, "SELECT COUNT(*) FROM task_postpone_idempotency") or 0)
    return int(fetch_scalar(
        store,
        "SELECT COUNT(*) FROM task_postpone_idempotency WHERE user_id = {ph}",
        (user_id,),
    ) or 0)


@pytest.fixture(params=["sqlite", "mysql"])
def store(request, tmp_path):
    """同一批顺延幂等断言同时跑 SQLite 与 MySQL（这条链路的 SQL 两个后端不同）。"""
    if request.param == "sqlite":
        return SQLiteTaskStore(tmp_path / "postpone-idempotency.sqlite3")
    from backend_fixtures import fresh_mysql_store

    return fresh_mysql_store()


@pytest.mark.parametrize(
    "key,expected_error",
    [
        (None, "idempotency_key_required"),
        ("not-a-uuid", "idempotency_key_invalid"),
        ("00000000-0000-1000-8000-000000000001", "idempotency_key_invalid"),
    ],
    ids=["missing", "malformed", "not-v4"],
)
def test_missing_or_invalid_key_returns_400_without_writes(store, key, expected_error):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("private due task", due_at=due, user_id="alice")
    before = store.get_task_for_user(task.id, "alice")
    before_events = _event_count(store, task.id)
    handler = DirectPostponeHandler(store, user_id="alice", key=key)

    handle_postpone_task(handler, f"/api/tasks/{task.id}/postpone", "alice")

    assert handler.status == HTTPStatus.BAD_REQUEST
    assert json.loads(handler.body) == {"error": expected_error}
    assert store.get_task_for_user(task.id, "alice") == before
    assert _event_count(store, task.id) == before_events
    assert _idempotency_count(store) == 0


@pytest.mark.parametrize(
    "days",
    [True, 1.0, "1", None, 0, -1],
    ids=["bool", "float", "string", "null", "zero", "negative"],
)
def test_new_key_rejects_non_positive_or_non_integer_days_without_writes(store, days):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("strict days", due_at=due, user_id="alice")
    before = store.get_task_for_user(task.id, "alice")
    before_events = _event_count(store, task.id)

    result = _call(store, task.id, payload={"days": days})

    assert result[0] == HTTPStatus.BAD_REQUEST
    assert result[2] == {"error": "days_invalid"}
    assert store.get_task_for_user(task.id, "alice") == before
    assert _event_count(store, task.id) == before_events
    assert _idempotency_count(store) == 0


def test_sqlite_store_direct_call_rejects_float_days_without_writes(store):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("direct storage validation", due_at=due, user_id="alice")
    before = store.get_task_for_user(task.id, "alice")
    before_events = _event_count(store, task.id)
    fingerprint = _fingerprint(task.id, 1.0)

    result = store.postpone_task_idempotent(
        task.id,
        1.0,
        user_id="alice",
        idempotency_key=KEY,
        request_fingerprint=fingerprint,
    )

    assert result == (HTTPStatus.BAD_REQUEST, {"error": "days_invalid"})
    assert store.get_task_for_user(task.id, "alice") == before
    assert _event_count(store, task.id) == before_events
    assert _idempotency_count(store) == 0


def test_old_sqlite_database_gets_idempotency_table_without_changing_existing_data(tmp_path):
    requires_sqlite(store)
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
            priority TEXT NOT NULL, due_at TEXT, estimated_minutes INTEGER,
            notes TEXT, parent_task_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY, task_id INTEGER, event_type TEXT NOT NULL,
            payload TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO users (id, display_name, created_at) VALUES ('alice', 'Alice', '2026-01-01T00:00:00+00:00');
        INSERT INTO tasks (id, title, status, priority, due_at, estimated_minutes, notes, parent_task_id, created_at, updated_at)
        VALUES (17, 'existing task', 'todo', 'medium', '2026-01-02T12:00:00+00:00', 25, 'keep me', NULL,
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        INSERT INTO task_events (id, task_id, event_type, payload, created_at)
        VALUES (29, 17, 'created', NULL, '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    migrated = SQLiteTaskStore(path)

    with migrated._connect() as db:
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        task = db.execute("SELECT * FROM tasks WHERE id = 17").fetchone()
        events = db.execute("SELECT id, task_id, event_type, payload, created_at FROM task_events").fetchall()
    assert "task_postpone_idempotency" in tables
    assert (task["title"], task["status"], task["due_at"], task["notes"], task["user_id"]) == (
        "existing task", "todo", "2026-01-02T12:00:00+00:00", "keep me", "default"
    )
    assert [tuple(event) for event in events] == [
        (29, 17, "created", None, "2026-01-01T00:00:00+00:00")
    ]
    assert _idempotency_count(migrated) == 0


def test_first_success_and_sequential_replay_preserve_response_and_write_once(store):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("owned deadline", due_at=due, user_id="alice")

    first = _call(store, task.id)
    replay = _call(store, task.id)

    assert first[:2] == replay[:2]
    assert first[0] == HTTPStatus.OK
    assert first[2] == {"message": "任务 #1「owned deadline」已推迟至 2026-06-08 09:15"}
    assert store.get_task_for_user(task.id, "alice").due_at == due + timedelta(days=1)
    assert _event_count(store, task.id, "updated") == 1
    assert _idempotency_count(store, "alice") == 1


def test_omitted_days_defaults_to_three(store):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("default days", due_at=due, user_id="alice")

    result = _call(store, task.id, payload={})

    assert result[0] == HTTPStatus.OK
    assert store.get_task_for_user(task.id, "alice").due_at == due + timedelta(days=3)
    assert _event_count(store, task.id, "updated") == 1
    assert _idempotency_count(store, "alice") == 1


def test_successful_replay_precedes_current_task_eligibility_check(store):
    from momentum_agent.models import TaskStatus

    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("eligibility changed", due_at=due, user_id="alice")
    first = _call(store, task.id)
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    after_status_change = store.get_task_for_user(task.id, "alice")
    events_after_status_change = _event_count(store, task.id)

    replay = _call(store, task.id)

    assert replay[:2] == first[:2]
    assert store.get_task_for_user(task.id, "alice") == after_status_change
    assert _event_count(store, task.id) == events_after_status_change
    assert _idempotency_count(store, "alice") == 1


def test_extremely_large_days_returns_out_of_range_without_writes(store):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("huge days", due_at=due, user_id="alice")
    before = store.get_task_for_user(task.id, "alice")
    before_events = _event_count(store, task.id)

    result = _call(store, task.id, days=10**1000)

    assert result[0] == HTTPStatus.BAD_REQUEST
    assert result[2] == {"error": "days_out_of_range"}
    assert store.get_task_for_user(task.id, "alice") == before
    assert _event_count(store, task.id) == before_events
    assert _idempotency_count(store) == 0


def test_datetime_max_boundary_and_reusable_key_after_out_of_range(store):
    due = datetime.max.replace(tzinfo=timezone.utc) - timedelta(days=1)
    task = store.create_task("datetime boundary", due_at=due, user_id="alice")
    before = store.get_task_for_user(task.id, "alice")
    before_events = _event_count(store, task.id)

    too_far = _call(store, task.id, days=2)

    assert too_far[0] == HTTPStatus.BAD_REQUEST
    assert too_far[2] == {"error": "days_out_of_range"}
    assert store.get_task_for_user(task.id, "alice") == before
    assert _event_count(store, task.id) == before_events
    assert _idempotency_count(store) == 0

    exactly_max = _call(store, task.id, days=1)

    assert exactly_max[0] == HTTPStatus.OK
    assert store.get_task_for_user(task.id, "alice").due_at == datetime.max.replace(tzinfo=timezone.utc)
    assert _event_count(store, task.id, "updated") == 1
    assert _idempotency_count(store) == 1


def test_concurrent_same_key_replays_only_postpone_once(store):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("concurrent deadline", due_at=due, user_id="alice")
    count = 8
    barrier = Barrier(count)

    def send_once(_index):
        barrier.wait(timeout=10)
        return _call(store, task.id)

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(send_once, range(count)))

    assert all(result == results[0] for result in results)
    assert results[0][0] == HTTPStatus.OK
    assert store.get_task_for_user(task.id, "alice").due_at == due + timedelta(days=1)
    assert _event_count(store, task.id, "updated") == 1
    assert _idempotency_count(store, "alice") == 1


def test_same_key_with_different_task_or_days_conflicts_without_writes(store):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    first_task = store.create_task("first", due_at=due, user_id="alice")
    other_task = store.create_task("second", due_at=due, user_id="alice")
    assert _call(store, first_task.id)[0] == HTTPStatus.OK
    second_before = store.get_task_for_user(other_task.id, "alice")
    second_events = _event_count(store, other_task.id)
    first_after_success = store.get_task_for_user(first_task.id, "alice")
    first_events = _event_count(store, first_task.id)

    changed_task = _call(store, other_task.id)
    changed_days = _call(store, first_task.id, days=2)
    changed_float_days = _call(store, first_task.id, days=1.0)

    assert changed_task[0] == changed_days[0] == changed_float_days[0] == HTTPStatus.CONFLICT
    assert changed_task[2] == changed_days[2] == changed_float_days[2] == {"error": "idempotency_conflict"}
    assert store.get_task_for_user(other_task.id, "alice") == second_before
    assert _event_count(store, other_task.id) == second_events
    assert store.get_task_for_user(first_task.id, "alice") == first_after_success
    assert _event_count(store, first_task.id) == first_events
    assert _idempotency_count(store, "alice") == 1
    saved = fetch_row(
        store,
        "SELECT request_fingerprint FROM task_postpone_idempotency WHERE user_id = {ph} AND idempotency_key = {ph}",
        ("alice", KEY),
    )
    assert saved["request_fingerprint"] == _fingerprint(first_task.id, 1)
    assert len(saved["request_fingerprint"]) == 64


def test_new_key_is_a_new_intent_and_postpones_an_additional_day(store):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("repeat intent", due_at=due, user_id="alice")

    assert _call(store, task.id, key=KEY)[0] == HTTPStatus.OK
    assert _call(store, task.id, key=OTHER_KEY)[0] == HTTPStatus.OK

    assert store.get_task_for_user(task.id, "alice").due_at == due + timedelta(days=2)
    assert _event_count(store, task.id, "updated") == 2
    assert _idempotency_count(store, "alice") == 2


def test_foreign_and_missing_404_responses_are_cached_without_disclosing_task(store):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    foreign = store.create_task("SECRET TITLE", due_at=due, user_id="bob")
    before = store.get_task_for_user(foreign.id, "bob")
    before_events = _event_count(store, foreign.id)

    first = _call(store, foreign.id, user_id="alice", key=KEY)
    replay = _call(store, foreign.id, user_id="alice", key=KEY)
    missing = _call(store, foreign.id + 9000, user_id="alice", key=OTHER_KEY)

    assert first[:2] == replay[:2]
    assert first[0] == missing[0] == HTTPStatus.NOT_FOUND
    assert first[2] == missing[2] == {"error": "没有找到这个任务。"}
    assert b"SECRET TITLE" not in first[1]
    assert due.isoformat().encode() not in first[1]
    assert store.get_task_for_user(foreign.id, "bob") == before
    assert _event_count(store, foreign.id) == before_events
    assert _idempotency_count(store, "alice") == 2


def test_ineligible_409_is_cached_and_replayed_without_task_or_event_writes(store):
    task = store.create_task("no deadline", user_id="alice")
    before = store.get_task_for_user(task.id, "alice")
    before_events = _event_count(store, task.id)

    first = _call(store, task.id)
    replay = _call(store, task.id)

    assert first[:2] == replay[:2]
    assert first[0] == HTTPStatus.CONFLICT
    assert first[2] == {"error": "该任务当前无法顺延截止日。"}
    assert store.get_task_for_user(task.id, "alice") == before
    assert _event_count(store, task.id) == before_events
    assert _idempotency_count(store, "alice") == 1


@pytest.mark.parametrize(
    "case,expected_status,expected_body",
    [
        ("missing", HTTPStatus.NOT_FOUND, {"error": "没有找到这个任务。"}),
        ("no_due", HTTPStatus.CONFLICT, {"error": "该任务当前无法顺延截止日。"}),
        ("ineligible", HTTPStatus.CONFLICT, {"error": "该任务当前无法顺延截止日。"}),
    ],
)
def test_300_digit_days_cache_and_replay_deterministic_errors(
    store, case, expected_status, expected_body
):
    from momentum_agent.models import TaskStatus

    huge_days = 10**300
    if case == "missing":
        task_id = 999999
        before_task = None
        before_events = 0
    else:
        due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc) if case == "ineligible" else None
        task = store.create_task(f"huge days {case}", due_at=due, user_id="alice")
        if case == "ineligible":
            store.update_status(task.id, TaskStatus.DONE, user_id="alice")
        task_id = task.id
        before_task = store.get_task_for_user(task_id, "alice")
        before_events = _event_count(store, task_id)

    first = _call(store, task_id, key=KEY, days=huge_days)
    replay = _call(store, task_id, key=KEY, days=huge_days)

    assert first[:2] == replay[:2]
    assert first[0] == expected_status
    assert first[2] == expected_body
    assert (None if case == "missing" else store.get_task_for_user(task_id, "alice")) == before_task
    assert (0 if case == "missing" else _event_count(store, task_id)) == before_events
    assert _idempotency_count(store, "alice") == 1
    saved = fetch_row(
        store,
        "SELECT request_fingerprint, response_status, response_json FROM task_postpone_idempotency WHERE user_id = {ph} AND idempotency_key = {ph}",
        ("alice", KEY),
    )
    assert saved["request_fingerprint"] == _fingerprint(task_id, huge_days)
    assert len(saved["request_fingerprint"]) == 64
    assert int(saved["response_status"]) == expected_status
    assert json.loads(saved["response_json"]) == expected_body


def test_same_uuid_is_isolated_between_authenticated_users(store):
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    alice = store.create_task("alice task", due_at=due, user_id="alice")
    bob = store.create_task("bob task", due_at=due, user_id="bob")

    alice_response = _call(store, alice.id, user_id="alice", key=KEY)
    bob_response = _call(store, bob.id, user_id="bob", key=KEY)

    assert alice_response[0] == bob_response[0] == HTTPStatus.OK
    assert store.get_task_for_user(alice.id, "alice").due_at == due + timedelta(days=1)
    assert store.get_task_for_user(bob.id, "bob").due_at == due + timedelta(days=1)
    assert _idempotency_count(store, "alice") == _idempotency_count(store, "bob") == 1


def test_mid_transaction_failure_rolls_back_and_same_key_can_retry(store):
    requires_sqlite(store)
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("retry after failure", due_at=due, user_id="alice")
    before = store.get_task_for_user(task.id, "alice")
    before_events = _event_count(store, task.id)
    with store._connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_postpone_record BEFORE INSERT ON task_postpone_idempotency
            BEGIN SELECT RAISE(ABORT, 'simulated persistence failure'); END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated persistence failure"):
        _call(store, task.id)

    assert store.get_task_for_user(task.id, "alice") == before
    assert _event_count(store, task.id) == before_events
    assert _idempotency_count(store) == 0
    with store._connect() as conn:
        conn.execute("DROP TRIGGER fail_postpone_record")

    retried = _call(store, task.id)

    assert retried[0] == HTTPStatus.OK
    assert store.get_task_for_user(task.id, "alice").due_at == due + timedelta(days=1)
    assert _event_count(store, task.id, "updated") == 1
    assert _idempotency_count(store) == 1
