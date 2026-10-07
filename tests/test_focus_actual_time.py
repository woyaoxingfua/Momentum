import json
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from momentum_agent.storage import TaskStore


class MockHandler:
    def __init__(self, payload=None):
        self._status = HTTPStatus.OK
        self._body = b""
        self._payload = payload or {}
        self.store = MagicMock()

    def read_json(self):
        return self._payload

    def send_json(self, payload, status=HTTPStatus.OK):
        self._status = status
        self._body = json.dumps(payload).encode()


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "focus.db")


def test_actual_focus_record_preserves_seconds_and_is_idempotent(store):
    task = store.create_task("可计时任务")
    started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    fields = {
        "user_id": "default",
        "actual_seconds": 73,
        "planned_minutes": 25,
        "started_at": started_at,
        "ended_at": started_at + timedelta(seconds=73),
        "outcome": "stopped",
        "session_id": "a" * 32,
    }

    store.record_focus_session(task.id, 25, **fields)
    store.record_focus_session(task.id, 25, **fields)

    sessions = store.get_focus_sessions(user_id="default")
    assert len(sessions) == 1
    session = sessions[0]
    assert session["actual_seconds"] == 73
    assert session["duration_minutes"] == pytest.approx(73 / 60)
    assert session["planned_minutes"] == 25
    assert session["outcome"] == "stopped"
    assert session["is_actual"] is True
    assert session["started_at"].replace(microsecond=0) == started_at.astimezone().replace(microsecond=0)
    assert session["ended_at"] is not None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"actual_seconds": True}, id="bool-seconds"),
        pytest.param({"actual_seconds": 73.0}, id="float-seconds"),
        pytest.param({"actual_seconds": "73"}, id="string-seconds"),
        pytest.param({"actual_seconds": -1}, id="negative-seconds"),
        pytest.param({"actual_seconds": 25 * 60 + 1}, id="over-plan"),
        pytest.param({"outcome": "paused"}, id="invalid-outcome"),
        pytest.param({"outcome": "completed"}, id="completed-before-plan"),
        pytest.param({"ended_at": datetime(2026, 1, 1)}, id="naive-ended-at"),
    ],
)
def test_storage_rejects_invalid_direct_focus_payloads(store, overrides):
    task = store.create_task("拒绝无效直调参数")
    started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    fields = {
        "user_id": "default",
        "actual_seconds": 73,
        "planned_minutes": 25,
        "started_at": started_at,
        "ended_at": started_at + timedelta(seconds=73),
        "outcome": "stopped",
        "session_id": "7" * 32,
    }
    fields.update(overrides)

    with pytest.raises(ValueError):
        store.record_focus_session(task.id, 25, **fields)

    assert store.get_focus_sessions(user_id="default") == []


def test_legacy_planned_focus_record_remains_readable_but_not_actual(store):
    task = store.create_task("旧格式任务")
    store.record_focus_session(task.id, 25, user_id="default")

    session = store.get_focus_sessions(user_id="default")[0]
    assert session["duration_minutes"] == 25
    assert session["planned_minutes"] == 25
    assert session["actual_seconds"] is None
    assert session["is_actual"] is False
    assert session["outcome"] == "legacy"


def test_start_focus_returns_session_without_persisting_planned_time():
    from momentum_agent.web.handlers import handle_start_focus

    handler = MockHandler({"task_id": 9, "duration_minutes": 25})
    handler.store.list_tasks.return_value = [SimpleNamespace(id=9)]

    handle_start_focus(handler, "user-a")

    payload = json.loads(handler._body)
    assert handler._status == HTTPStatus.OK
    assert len(payload["session_id"]) == 32
    assert payload["duration_minutes"] == 25
    handler.store.record_focus_session.assert_not_called()


def test_finish_focus_persists_real_seconds_for_owned_task():
    from momentum_agent.web.handlers import handle_finish_focus

    payload = {
        "task_id": 9,
        "session_id": "b" * 32,
        "started_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "planned_minutes": 25,
        "actual_seconds": 73,
        "outcome": "stopped",
    }
    handler = MockHandler(payload)
    handler.store.list_tasks.return_value = [SimpleNamespace(id=9)]

    handle_finish_focus(handler, "user-a")

    result = json.loads(handler._body)
    assert handler._status == HTTPStatus.OK
    assert result["actual_seconds"] == 73
    assert result["outcome"] == "stopped"
    handler.store.record_focus_session.assert_called_once()
    args, kwargs = handler.store.record_focus_session.call_args
    assert args == (9, 25)
    assert kwargs["user_id"] == "user-a"
    assert kwargs["actual_seconds"] == 73
    assert kwargs["outcome"] == "stopped"


def test_concurrent_finish_focus_records_same_session_once(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from momentum_agent.web.handlers import handle_finish_focus

    task = store.create_task("并发完成专注任务")
    payload = {
        "task_id": task.id,
        "session_id": "1" * 32,
        "started_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "planned_minutes": 25,
        "actual_seconds": 73,
        "outcome": "stopped",
    }
    concurrent_calls = 12
    barrier = Barrier(concurrent_calls)

    def finish_request():
        handler = MockHandler(payload)
        handler.store = store
        barrier.wait(timeout=5)
        handle_finish_focus(handler, "default")
        return handler._status, json.loads(handler._body)

    with ThreadPoolExecutor(max_workers=concurrent_calls) as pool:
        results = list(pool.map(lambda _index: finish_request(), range(concurrent_calls)))

    assert all(status == HTTPStatus.OK for status, _body in results)
    assert all(body["session_id"] == payload["session_id"] for _status, body in results)
    sessions = store.get_focus_sessions(user_id="default")
    assert len([session for session in sessions if session["session_id"] == payload["session_id"]]) == 1


def test_concurrent_different_finish_payloads_have_one_winner_and_one_conflict(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from momentum_agent.web.handlers import handle_finish_focus

    task = store.create_task("并发冲突专注任务")
    started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    ended_at = datetime.now(timezone.utc)
    base = {
        "task_id": task.id,
        "session_id": "8" * 32,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "planned_minutes": 25,
        "outcome": "stopped",
    }
    payloads = [
        {**base, "actual_seconds": 73},
        {**base, "actual_seconds": 74},
    ]
    barrier = Barrier(2)

    def finish(payload):
        handler = MockHandler(payload)
        handler.store = store
        barrier.wait(timeout=5)
        handle_finish_focus(handler, "default")
        return handler._status, json.loads(handler._body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, payloads))

    assert sorted(status for status, _body in results) == [HTTPStatus.OK, HTTPStatus.CONFLICT]
    successful = next(body for status, body in results if status == HTTPStatus.OK)
    stored = [
        session for session in store.get_focus_sessions(user_id="default")
        if session["session_id"] == base["session_id"]
    ]
    assert len(stored) == 1
    assert stored[0]["actual_seconds"] == successful["actual_seconds"]
    with store._connect() as conn:
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND event_type = 'focus_session'",
            (task.id,),
        ).fetchone()[0]
    assert event_count == 1


def test_finish_focus_rejects_early_completed_outcome_and_foreign_task():
    from momentum_agent.web.handlers import handle_finish_focus

    payload = {
        "task_id": 9,
        "session_id": "c" * 32,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "planned_minutes": 25,
        "actual_seconds": 60,
        "outcome": "completed",
    }
    handler = MockHandler(payload)
    handler.store.list_tasks.return_value = [SimpleNamespace(id=10)]

    handle_finish_focus(handler, "user-a")

    assert handler._status == HTTPStatus.BAD_REQUEST
    handler.store.record_focus_session.assert_not_called()

    payload["actual_seconds"] = 25 * 60
    handler = MockHandler(payload)
    handler.store.list_tasks.return_value = [SimpleNamespace(id=10)]
    handle_finish_focus(handler, "user-a")
    assert handler._status == HTTPStatus.NOT_FOUND
    handler.store.record_focus_session.assert_not_called()


def test_focus_stats_response_is_json_serializable_and_reports_real_seconds(store):
    from momentum_agent.web.handlers import handle_get_focus_stats

    task = store.create_task("统计 JSON 序列化任务")
    store.record_focus_session(
        task.id,
        1,
        actual_seconds=6,
        planned_minutes=1,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=8),
        ended_at=datetime.now(timezone.utc),
        outcome="stopped",
        session_id="e" * 32,
    )
    handler = MockHandler()
    handler.store = store

    handle_get_focus_stats(handler, "default")

    result = json.loads(handler._body)
    assert handler._status == HTTPStatus.OK
    assert result["total_sessions_week"] == 1
    assert result["total_minutes_today"] == pytest.approx(0.1)
    assert result["total_minutes_week"] == pytest.approx(0.1)
    assert result["sessions"][0]["actual_seconds"] == 6
    assert result["sessions"][0]["outcome"] == "stopped"
    assert isinstance(result["sessions"][0]["started_at"], str)
    assert isinstance(result["sessions"][0]["ended_at"], str)


def test_dashboard_focus_chart_preserves_subminute_actual_time(store):
    from momentum_agent.web.handlers import handle_get_stats

    task = store.create_task("统计图短时长任务")
    store.record_focus_session(
        task.id,
        1,
        actual_seconds=6,
        planned_minutes=1,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=8),
        ended_at=datetime.now(timezone.utc),
        outcome="stopped",
        session_id="f" * 32,
    )
    handler = MockHandler()
    handler.store = store

    handle_get_stats(handler, "default")

    result = json.loads(handler._body)
    assert handler._status == HTTPStatus.OK
    assert 0.1 in result["focus"]["minutes"]
