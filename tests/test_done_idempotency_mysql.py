"""完成幂等的关键语义，在真实 MySQL 上再验一遍。

tests/test_done_idempotency.py 深度绑定 SQLite（直接用 sqlite3 造老库、PRAGMA、sqlite3.IntegrityError、
以及 conn.execute 之类的检查），不适合参数化；而线上跑的是 MySQL，完成/重复提交/递归任务这条
路径又是最关键的正确性要求，因此在真实 MySQL 上单独复核一遍这些语义。

未配置 MOMENTUM_TEST_MYSQL_URL 时整体跳过（本地与 test job 不受影响，mysql-integration job 会跑）。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from threading import Barrier

import pytest

from backend_fixtures import fetch_scalar, fresh_mysql_store
from momentum_agent.models import TaskStatus
from momentum_agent.web.handlers import handle_done_task

KEY = "00000000-0000-4000-8000-0000000000a1"
OTHER_KEY = "00000000-0000-4000-8000-0000000000a2"
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


@pytest.fixture
def store():
    return fresh_mysql_store()


def call(store, task_id, *, user_id="alice", key=KEY):
    handler = DirectDoneHandler(store, key=key)
    handle_done_task(handler, f"/api/tasks/{task_id}/done", user_id)
    return handler.status, handler.body, json.loads(handler.body)


def rows(store, table):
    return int(fetch_scalar(store, f"SELECT COUNT(*) FROM {table}") or 0)


def events(store, task_id, event_type="status_changed"):
    return int(fetch_scalar(
        store,
        "SELECT COUNT(*) FROM task_events WHERE task_id = {ph} AND event_type = {ph}",
        (task_id, event_type),
    ) or 0)


def new_recurring(store, title="mysql daily", *, user_id="alice"):
    return store.create_task(title, due_at=DUE, recurrence="daily", user_id=user_id)


def test_missing_key_writes_nothing(store):
    task = new_recurring(store)
    status, _body, payload = call(store, task.id, key=None)
    assert status == HTTPStatus.BAD_REQUEST, payload
    assert payload == {"error": "idempotency_key_required"}
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.TODO
    assert rows(store, "task_done_idempotency") == 0
    assert rows(store, "task_done_occurrences") == 0


def test_first_done_then_replay_is_idempotent(store):
    task = new_recurring(store, "mysql replay")
    first_status, first_body, first_payload = call(store, task.id)
    assert first_status == HTTPStatus.OK, first_payload
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.DONE
    assert events(store, task.id) == 1
    assert rows(store, "task_done_idempotency") == 1
    assert rows(store, "task_done_occurrences") == 1

    replay_status, replay_body, replay_payload = call(store, task.id)
    assert replay_status == HTTPStatus.OK
    assert replay_body == first_body, "重放必须逐字节返回同一结果"
    assert replay_payload == first_payload
    assert events(store, task.id) == 1, "重放不得再写事件"
    assert rows(store, "task_done_idempotency") == 1
    assert rows(store, "task_done_occurrences") == 1


def test_same_key_on_another_task_conflicts(store):
    first_task = new_recurring(store, "mysql first")
    second_task = new_recurring(store, "mysql second")
    assert call(store, first_task.id)[0] == HTTPStatus.OK
    conflict_status, _body, conflict = call(store, second_task.id)
    assert conflict_status == HTTPStatus.CONFLICT
    assert conflict == {"error": "idempotency_conflict"}
    assert store.get_task_for_user(second_task.id, "alice").status == TaskStatus.TODO
    assert events(store, second_task.id) == 0
    assert rows(store, "task_done_idempotency") == 1


def test_same_key_is_independent_across_users(store):
    alice_task = new_recurring(store, "alice mysql")
    bob_task = new_recurring(store, "bob mysql", user_id="bob")
    assert call(store, alice_task.id, user_id="alice")[0] == HTTPStatus.OK
    bob_status, _body, bob_payload = call(store, bob_task.id, user_id="bob", key=KEY)
    assert bob_status == HTTPStatus.OK, bob_payload
    assert rows(store, "task_done_idempotency") == 2


def test_concurrent_same_key_requests_write_once(store):
    task = new_recurring(store, "mysql parallel")
    count = 6
    barrier = Barrier(count)

    def send(_):
        barrier.wait(timeout=15)
        return call(store, task.id, key=KEY)

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(send, range(count)))

    statuses = [status for status, _body, _payload in results]
    assert statuses.count(HTTPStatus.OK) >= 1, statuses
    assert all(status in (HTTPStatus.OK, HTTPStatus.CONFLICT) for status in statuses), statuses
    assert events(store, task.id) == 1, "并发去重后只能有一条完成事件"
    assert rows(store, "task_done_occurrences") == 1
    assert rows(store, "task_done_idempotency") == 1

