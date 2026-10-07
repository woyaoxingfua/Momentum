from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from urllib.parse import urlencode

import pytest

from momentum_agent.auth import hash_password
from momentum_agent.models import Priority, TaskStatus
from momentum_agent.storage import TaskStore


class Handler:
    def __init__(self, store, *, path="/", payload=None):
        self.store = store
        self.path = path
        self._payload = payload or {}
        self._status = HTTPStatus.OK
        self._body = b""

    def read_json(self):
        return self._payload

    def send_json(self, payload, status=HTTPStatus.OK):
        self._status = status
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @property
    def body(self):
        return json.loads(self._body)


@pytest.fixture
def store(tmp_path):
    db = TaskStore(tmp_path / "isolated-review-focus.db")
    db.register_user("alice", "Alice", hash_password("alice-password"))
    db.register_user("bob", "Bob", hash_password("bob-password"))
    return db


def _event_count(store, task_id: int, event_type="status_changed", payload="done"):
    with store._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND event_type = ? AND payload = ?",
            (task_id, event_type, payload),
        ).fetchone()[0]


def _review(store, zone: str, day: str, user_id="alice"):
    from momentum_agent.web.handlers import handle_review

    handler = Handler(store, path="/api/review?" + urlencode({"timeZone": zone, "localDate": day}))
    handle_review(handler, user_id)
    return handler


def _finish(store, payload, user_id="alice"):
    from momentum_agent.web.handlers import handle_finish_focus

    handler = Handler(store, path="/api/focus/finish", payload=payload)
    handle_finish_focus(handler, user_id)
    return handler


def _focus_payload(task_id: int, session_id: str, *, started_at: datetime, ended_at: datetime, actual_seconds=73):
    return {
        "task_id": task_id,
        "session_id": session_id,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "planned_minutes": 25,
        "actual_seconds": actual_seconds,
        "outcome": "stopped",
        "user_id": "bob",  # Must never be used as authorization input.
    }


def test_advice_includes_todo_and_doing_with_stable_real_id(store):
    from momentum_agent.agent_app import local_advice
    from momentum_agent.web.handlers import handle_advice

    doing = store.create_task("高优先级进行中", priority=Priority.HIGH, estimated_minutes=35, user_id="alice")
    store.start_task(doing.id, user_id="alice")
    todo = store.create_task("低优先级待办", priority=Priority.LOW, estimated_minutes=20, user_id="alice")
    closed = store.create_task("已完成任务", priority=Priority.HIGH, user_id="alice")
    store.update_status(closed.id, TaskStatus.DONE, user_id="alice")

    first = Handler(store)
    handle_advice(first, "alice")
    second = Handler(store)
    handle_advice(second, "alice")

    assert first.body["advice"] == local_advice(store, user_id="alice")
    assert first.body["suggestion"] == {
        "task_id": doing.id,
        "title": doing.title,
        "status": "doing",
        "estimated_minutes": 35,
    }
    assert second.body["suggestion"] == first.body["suggestion"]
    assert first.body["suggestion"]["task_id"] in {doing.id, todo.id}
    assert first.body["suggestion"]["task_id"] != closed.id
    empty = Handler(store)
    handle_advice(empty, "bob")
    assert empty.body["suggestion"] is None


def test_focus_start_requires_owned_open_task_and_does_not_change_plan_or_status(store):
    from momentum_agent.web.handlers import handle_start_focus

    task = store.create_task("只能执行但不改变任务", estimated_minutes=40, user_id="alice")
    before = store.get_task_for_user(task.id, "alice")
    before_events = store.get_review_data(user_id="alice")
    handler = Handler(store, payload={"task_id": task.id, "duration_minutes": 25, "user_id": "bob"})
    handle_start_focus(handler, "alice")
    after = store.get_task_for_user(task.id, "alice")
    after_events = store.get_review_data(user_id="alice")

    assert handler._status == HTTPStatus.OK
    assert handler.body["task_id"] == task.id
    assert after.status == before.status == TaskStatus.TODO
    assert after.estimated_minutes == before.estimated_minutes == 40
    assert after_events == before_events

    foreign = Handler(store, payload={"task_id": task.id})
    handle_start_focus(foreign, "bob")
    assert foreign._status == HTTPStatus.NOT_FOUND
    missing = Handler(store, payload={"task_id": 999999})
    handle_start_focus(missing, "alice")
    assert missing._status == HTTPStatus.NOT_FOUND

    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    closed = Handler(store, payload={"task_id": task.id})
    handle_start_focus(closed, "alice")
    assert closed._status == HTTPStatus.CONFLICT
    assert closed.body["error"] == "task_not_open"

    dropped = store.create_task("已放弃", user_id="alice")
    store.update_status(dropped.id, TaskStatus.DROPPED, user_id="alice")
    dropped_handler = Handler(store, payload={"task_id": dropped.id})
    handle_start_focus(dropped_handler, "alice")
    assert dropped_handler._status == HTTPStatus.CONFLICT


def test_done_events_are_real_transitions_and_parent_cascade_records_each_child(store):
    task = store.create_task("reopen 任务", user_id="alice")
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    assert _event_count(store, task.id) == 1
    assert store.batch_update_status([task.id], TaskStatus.DONE, user_id="alice") == 0
    assert _event_count(store, task.id) == 1

    store.reopen_task(task.id, user_id="alice")
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    assert _event_count(store, task.id) == 2

    parent = store.create_task("父任务", user_id="alice")
    child_a = store.create_subtask(parent.id, "子任务 A", user_id="alice")
    child_b = store.create_subtask(parent.id, "子任务 B", user_id="alice")
    store.update_status(parent.id, TaskStatus.DONE, user_id="alice")
    store.update_status(parent.id, TaskStatus.DONE, user_id="alice")

    assert store.get_task_for_user(parent.id, "alice").status == TaskStatus.DONE
    assert store.get_task_for_user(child_a.id, "alice").status == TaskStatus.DONE
    assert store.get_task_for_user(child_b.id, "alice").status == TaskStatus.DONE
    assert _event_count(store, parent.id) == 1
    assert _event_count(store, child_a.id) == 1
    assert _event_count(store, child_b.id) == 1
    data = store.get_review_data(user_id="alice")
    assert sum(event["task_id"] in {parent.id, child_a.id, child_b.id} for event in data["status_events"]) == 3


def test_review_uses_offset_completion_times_and_dst_day_bounds(store):
    from momentum_agent.web.handlers import handle_review

    task = store.create_task("事件时间不是 updated_at", user_id="alice")
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    store.update_task(task.id, title="当前任务标题", user_id="alice")
    with store._connect() as conn:
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND event_type = 'status_changed' AND payload = 'done'",
            ("2026-04-01T08:00:00+02:00", task.id),
        )

    result = _review(store, "America/Los_Angeles", "2026-03-31")
    assert result._status == HTTPStatus.OK
    body = result.body
    assert body["timeZone"] == "America/Los_Angeles"
    assert body["localDate"] == "2026-03-31"
    assert body["dayStartUtc"] == "2026-03-31T07:00:00+00:00"
    assert body["dayEndUtcExclusive"] == "2026-04-01T07:00:00+00:00"
    assert body["completed_count"] == 1
    assert body["completed_events"] == [{
        "task_id": task.id,
        "completed_at": "2026-04-01T06:00:00+00:00",
        "title": "当前任务标题",
        "estimated_minutes_reference": None,
    }]

    # On spring-forward and fall-back days New York's local calendar days are 23h and 25h.
    spring = _review(store, "America/New_York", "2026-03-08").body
    fall = _review(store, "America/New_York", "2026-11-01").body
    assert spring["dayStartUtc"] == "2026-03-08T05:00:00+00:00"
    assert spring["dayEndUtcExclusive"] == "2026-03-09T04:00:00+00:00"
    assert fall["dayStartUtc"] == "2026-11-01T04:00:00+00:00"
    assert fall["dayEndUtcExclusive"] == "2026-11-02T05:00:00+00:00"
    assert (
        datetime.fromisoformat(spring["dayEndUtcExclusive"])
        - datetime.fromisoformat(spring["dayStartUtc"])
    ) == timedelta(hours=23)
    assert (
        datetime.fromisoformat(fall["dayEndUtcExclusive"])
        - datetime.fromisoformat(fall["dayStartUtc"])
    ) == timedelta(hours=25)


def test_review_local_day_includes_start_and_excludes_end_exactly(store):
    start_task = store.create_task("本地日 start 边界", user_id="alice")
    end_task = store.create_task("本地日 end 边界", user_id="alice")
    store.update_status(start_task.id, TaskStatus.DONE, user_id="alice")
    store.update_status(end_task.id, TaskStatus.DONE, user_id="alice")

    day_start = datetime.fromisoformat("2026-03-31T07:00:00+00:00")
    day_end = datetime.fromisoformat("2026-04-01T07:00:00+00:00")
    with store._connect() as conn:
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND event_type = 'status_changed' AND payload = 'done'",
            (day_start.isoformat(), start_task.id),
        )
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND event_type = 'status_changed' AND payload = 'done'",
            (day_end.isoformat(), end_task.id),
        )

    store.record_focus_session(
        start_task.id, 25, user_id="alice", actual_seconds=10, planned_minutes=25,
        started_at=day_start - timedelta(seconds=1), ended_at=day_start,
        outcome="stopped", session_id="a" * 32,
    )
    store.record_focus_session(
        end_task.id, 25, user_id="alice", actual_seconds=20, planned_minutes=25,
        started_at=day_end - timedelta(seconds=1), ended_at=day_end,
        outcome="stopped", session_id="b" * 32,
    )

    body = _review(store, "America/Los_Angeles", "2026-03-31").body
    assert body["dayStartUtc"] == day_start.isoformat()
    assert body["dayEndUtcExclusive"] == day_end.isoformat()
    assert body["completed_count"] == 1
    assert [event["task_id"] for event in body["completed_events"]] == [start_task.id]
    assert body["today_focus_actual_seconds"] == 10
    assert body["today_focus_session_count"] == 1


def test_review_treats_legacy_naive_event_timestamp_as_utc(store):
    task = store.create_task("无 offset 的旧完成事件", user_id="alice")
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    with store._connect() as conn:
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND event_type = 'status_changed' AND payload = 'done'",
            ("2026-03-31T23:30:00", task.id),
        )

    body = _review(store, "America/Los_Angeles", "2026-03-31").body
    assert body["completed_count"] == 1
    assert body["completed_events"][0]["completed_at"] == "2026-03-31T23:30:00+00:00"


def test_review_ignores_legacy_repeated_done_but_counts_reopen_then_done(store):
    task = store.create_task("旧数据重复 done", user_id="alice")
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, 'status_changed', 'done', ?)",
            (task.id, "2026-06-01T10:01:00Z"),
        )
    store.reopen_task(task.id, user_id="alice")
    store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND event_type = 'status_changed' ORDER BY id",
            (task.id,),
        ).fetchall()
        for row, timestamp in zip(rows, (
            "2026-06-01T10:00:00Z",
            "2026-06-01T10:01:00Z",
            "2026-06-01T10:02:00Z",
            "2026-06-01T10:03:00Z",
        )):
            conn.execute("UPDATE task_events SET created_at = ? WHERE id = ?", (timestamp, row["id"]))

    body = _review(store, "UTC", "2026-06-01").body
    assert body["completed_count"] == 2
    assert [event["completed_at"] for event in body["completed_events"]] == [
        "2026-06-01T10:00:00+00:00",
        "2026-06-01T10:03:00+00:00",
    ]


def test_review_focus_uses_ended_at_actual_seconds_and_user_scope(store):
    task = store.create_task("未完成也可记实际专注", estimated_minutes=25, user_id="alice")
    started_cross_midnight = datetime.fromisoformat("2026-03-31T06:30:00+00:00")
    ended_local_day = datetime.fromisoformat("2026-04-01T08:30:00+02:00")  # 06:30Z, March 31 in LA.
    store.record_focus_session(
        task.id, 25, user_id="alice", actual_seconds=73, planned_minutes=25,
        started_at=started_cross_midnight, ended_at=ended_local_day,
        outcome="stopped", session_id="a" * 32,
    )
    # Zero seconds is still a recorded session and contributes to count.
    store.record_focus_session(
        task.id, 25, user_id="alice", actual_seconds=0, planned_minutes=25,
        started_at=datetime.fromisoformat("2026-04-01T06:00:00+00:00"),
        ended_at=datetime.fromisoformat("2026-04-01T06:45:00+00:00"),
        outcome="stopped", session_id="b" * 32,
    )
    # Ended after the requested day: count neither seconds nor session.
    store.record_focus_session(
        task.id, 25, user_id="alice", actual_seconds=120, planned_minutes=25,
        started_at=datetime.fromisoformat("2026-04-01T06:50:00+00:00"),
        ended_at=datetime.fromisoformat("2026-04-01T07:30:00+00:00"),
        outcome="stopped", session_id="c" * 32,
    )
    bob_task = store.create_task("Bob 的记录", estimated_minutes=90, user_id="bob")
    store.record_focus_session(
        bob_task.id, 90, user_id="bob", actual_seconds=999, planned_minutes=90,
        started_at=started_cross_midnight, ended_at=ended_local_day,
        outcome="stopped", session_id="d" * 32,
    )
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, 'focus_session', ?, ?)",
            (task.id, json.dumps({"duration_minutes": 25, "actual_seconds": None, "ended_at": "2026-04-01T06:20:00Z"}), "2026-04-01T06:20:01Z"),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, 'focus_session', ?, ?)",
            (task.id, json.dumps({"actual_seconds": 500}), "2026-04-01T06:20:02Z"),
        )

    body = _review(store, "America/Los_Angeles", "2026-03-31").body
    assert body["today_focus_actual_seconds"] == 73
    assert body["today_focus_session_count"] == 2
    assert "不跨日拆分" in body["focus_attribution_note"]


def test_review_rejects_invalid_time_zone_and_noncanonical_or_invalid_dates(store):
    for zone, day in (
        ("Not/A_Real_Zone", "2026-01-01"),
        ("UTC", "2025-02-29"),
        ("UTC", "2026-1-01"),
        ("UTC", "2026-01-01T00:00:00"),
    ):
        handler = _review(store, zone, day)
        assert handler._status == HTTPStatus.BAD_REQUEST


def test_finish_requires_valid_timezone_aware_ended_at(store):
    task = store.create_task("结束时间校验", user_id="alice")
    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    valid_end = datetime.now(timezone.utc)
    base = _focus_payload(task.id, "e" * 32, started_at=started, ended_at=valid_end)
    cases = [
        {key: value for key, value in base.items() if key != "ended_at"},
        {**base, "ended_at": "2026-10-04T00:00:00"},
        {**base, "ended_at": "not-a-date"},
        {**base, "ended_at": (started - timedelta(seconds=1)).isoformat()},
        {**base, "ended_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    ]
    for payload in cases:
        handler = _finish(store, payload)
        assert handler._status == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize(
    "case",
    ["missing", "null", "bool", "float", "string", "negative", "over-plan"],
)
def test_finish_rejects_non_integer_or_out_of_plan_actual_seconds(store, case):
    task = store.create_task("HTTP actual_seconds 输入边界", user_id="alice")
    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    payload = _focus_payload(
        task.id, f"{len(case):032x}", started_at=started, ended_at=datetime.now(timezone.utc)
    )
    values = {
        "null": None,
        "bool": True,
        "float": 73.0,
        "string": "73",
        "negative": -1,
        "over-plan": 25 * 60 + 1,
    }
    if case == "missing":
        payload.pop("actual_seconds")
    else:
        payload["actual_seconds"] = values[case]

    handler = _finish(store, payload)

    assert handler._status == HTTPStatus.BAD_REQUEST
    assert store.get_focus_sessions(user_id="alice") == []


def test_finish_same_payload_is_idempotent_and_conflicting_payload_is_409(store):
    task = store.create_task("重试冻结", user_id="alice")
    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    ended = datetime.now(timezone.utc)
    payload = _focus_payload(task.id, "f" * 32, started_at=started, ended_at=ended, actual_seconds=73)

    first = _finish(store, payload)
    second = _finish(store, dict(payload))
    assert first._status == second._status == HTTPStatus.OK
    assert first.body == second.body
    assert first.body["task_id"] == task.id
    assert first.body["ended_at"] == ended.astimezone(timezone.utc).isoformat()
    assert _event_count(store, task.id, event_type="focus_session", payload="") == 0
    sessions = [s for s in store.get_focus_sessions(user_id="alice") if s["session_id"] == payload["session_id"]]
    assert len(sessions) == 1

    conflict = _finish(store, {**payload, "actual_seconds": 74})
    assert conflict._status == HTTPStatus.CONFLICT
    assert conflict.body["error"] == "idempotency_conflict"
    other_task = store.create_task("另一个任务", user_id="alice")
    task_conflict = _finish(store, {**payload, "task_id": other_task.id})
    assert task_conflict._status == HTTPStatus.CONFLICT
    started_conflict = _finish(store, {**payload, "started_at": (started + timedelta(seconds=1)).isoformat()})
    assert started_conflict._status == HTTPStatus.CONFLICT
    planned_conflict = _finish(store, {**payload, "planned_minutes": 26})
    assert planned_conflict._status == HTTPStatus.CONFLICT
    ended_conflict = _finish(store, {**payload, "ended_at": (ended + timedelta(seconds=1)).isoformat()})
    assert ended_conflict._status == HTTPStatus.CONFLICT

    completed_payload = _focus_payload(
        task.id, "1" * 32, started_at=started, ended_at=ended, actual_seconds=25 * 60
    )
    completed_payload["outcome"] = "completed"
    assert _finish(store, completed_payload)._status == HTTPStatus.OK
    outcome_conflict = _finish(store, {**completed_payload, "outcome": "stopped"})
    assert outcome_conflict._status == HTTPStatus.CONFLICT
    assert len([s for s in store.get_focus_sessions(user_id="alice") if s["session_id"] == payload["session_id"]]) == 1


def test_finish_idempotency_and_review_are_scoped_to_authenticated_user(store):
    from momentum_agent.web.handlers import handle_review

    alice_task = store.create_task("Alice task", user_id="alice")
    bob_task = store.create_task("Bob task", user_id="bob")
    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    ended = datetime.now(timezone.utc)
    shared_session_id = "9" * 32
    alice_result = _finish(store, _focus_payload(alice_task.id, shared_session_id, started_at=started, ended_at=ended), "alice")
    bob_result = _finish(store, _focus_payload(bob_task.id, shared_session_id, started_at=started, ended_at=ended), "bob")
    assert alice_result._status == bob_result._status == HTTPStatus.OK

    store.update_status(alice_task.id, TaskStatus.DONE, user_id="alice")
    store.update_status(bob_task.id, TaskStatus.DONE, user_id="bob")
    with store._connect() as conn:
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id IN (?, ?) AND event_type = 'status_changed' AND payload = 'done'",
            ("2026-05-04T10:00:00Z", alice_task.id, bob_task.id),
        )
    alice_review = Handler(store, path="/api/review?" + urlencode({"timeZone": "UTC", "localDate": "2026-05-04"}))
    bob_review = Handler(store, path="/api/review?" + urlencode({"timeZone": "UTC", "localDate": "2026-05-04"}))
    handle_review(alice_review, "alice")
    handle_review(bob_review, "bob")
    assert {e["task_id"] for e in alice_review.body["completed_events"]} == {alice_task.id}
    assert {e["task_id"] for e in bob_review.body["completed_events"]} == {bob_task.id}
    alice_sessions = store.get_focus_sessions(user_id="alice")
    bob_sessions = store.get_focus_sessions(user_id="bob")
    assert [s["task_id"] for s in alice_sessions if s["session_id"] == shared_session_id] == [alice_task.id]
    assert [s["task_id"] for s in bob_sessions if s["session_id"] == shared_session_id] == [bob_task.id]
