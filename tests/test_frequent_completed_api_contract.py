"""历史常完成任务 — HTTP 契约测试（隔离临时库 + 真实 HTTP 服务）。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from http.server import ThreadingHTTPServer

from momentum_agent.auth import hash_password
from momentum_agent.models import Priority, TaskRelationType, TaskStatus
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import MomentumHandler, _store_cache

PASSWORD = "frequent-api-password"
ALLOWED_KEYS = {"key", "title", "completion_count", "last_completed_at", "priority", "estimated_minutes", "tags"}


@pytest.fixture
def api(tmp_path):
    database_path = tmp_path / "frequent-api.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    store = SQLiteTaskStore(database_path)
    for user_id in ("alice", "bob"):
        store.register_user(user_id, user_id, hash_password(PASSWORD))
    tokens = {user: store.login_user(user, PASSWORD) for user in ("alice", "bob")}
    assert all(tokens.values())
    handler_type = type("FrequentContractHandler", (MomentumHandler,), {"database_url": database_url})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield store, f"http://127.0.0.1:{server.server_port}", tokens
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)


def _request(base_url, token, method, path, payload=None):
    body = json.dumps(payload or {}).encode("utf-8") if method in {"PUT", "POST"} else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as response:
        return response.code, json.loads(response.read().decode("utf-8"))


def _complete(store, title, *, user_id, priority=Priority.MEDIUM, minutes=None, tags=None, recurrence=None, due_at=None):
    task = store.create_task(
        title, priority=priority, estimated_minutes=minutes, tags=tags,
        recurrence=recurrence, due_at=due_at, user_id=user_id,
    )
    store.update_status(task.id, TaskStatus.DONE, user_id=user_id)
    return task


def _event_count(store, task_id):
    with store._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0]


def test_routes_are_registered():
    assert "frequent" in MomentumHandler.GET_PREFIX_ROUTES
    assert "/api/frequent/recreate" in MomentumHandler.POST_EXACT_ROUTES


def test_get_requires_authentication(api):
    _store, base, _tokens = api
    status, payload = _request(base, None, "GET", "/api/tasks/frequent")
    assert status == 401
    assert "error" in payload


def test_post_requires_authentication(api):
    _store, base, _tokens = api
    status, _payload = _request(base, None, "POST", "/api/frequent/recreate", {"key": "写周报"})
    assert status == 401


def test_get_returns_only_qualified_groups_with_public_keys(api):
    store, base, tokens = api
    _complete(store, "写周报", user_id="alice", minutes=30, priority=Priority.HIGH, tags=["工作"])
    _complete(store, "写周报", user_id="alice", minutes=45, priority=Priority.LOW)
    _complete(store, "只完成一次", user_id="alice")
    status, payload = _request(base, tokens["alice"], "GET", "/api/tasks/frequent")
    assert status == 200
    assert payload["window_days"] == 180
    assert payload["min_distinct_tasks"] == 2
    assert [item["title"] for item in payload["tasks"]] == ["写周报"]
    item = payload["tasks"][0]
    assert set(item) == ALLOWED_KEYS, item.keys()
    assert item["completion_count"] == 2
    assert item["priority"] == "low", "字段必须来自最近完成的那条"
    assert item["estimated_minutes"] == 45
    assert item["tags"] == []


def test_get_never_leaks_another_users_history(api):
    store, base, tokens = api
    _complete(store, "alice 的私事", user_id="alice")
    _complete(store, "alice 的私事", user_id="alice")
    _complete(store, "bob 的私事", user_id="bob")
    _complete(store, "bob 的私事", user_id="bob")
    status, payload = _request(base, tokens["alice"], "GET", "/api/tasks/frequent")
    assert status == 200
    titles = [item["title"] for item in payload["tasks"]]
    assert titles == ["alice 的私事"]
    status, payload = _request(base, tokens["bob"], "GET", "/api/tasks/frequent")
    assert [item["title"] for item in payload["tasks"]] == ["bob 的私事"]


def test_post_recreates_with_exactly_four_copied_fields(api):
    from datetime import datetime, timedelta, timezone

    store, base, tokens = api
    due = datetime.now(timezone.utc) + timedelta(days=3)
    parent = store.create_task("父任务", user_id="alice")
    _complete(store, "写周报", user_id="alice", minutes=20, priority=Priority.LOW, tags=["旧"])
    source = store.create_task(
        "写周报", priority=Priority.HIGH, estimated_minutes=35, tags=["工作", "例行"],
        due_at=due, notes="备注不该被复制", parent_task_id=parent.id, user_id="alice",
    )
    store.add_task_relation(source.id, parent.id, TaskRelationType.RELATES_TO)
    store.update_status(source.id, TaskStatus.DONE, user_id="alice")
    events_before = _event_count(store, source.id)

    status, payload = _request(base, tokens["alice"], "POST", "/api/frequent/recreate", {"key": "写周报"})
    assert status == 200, payload
    created = store._get_task(payload["task"]["id"])
    assert created.id != source.id, "必须是新建任务，不是重开旧任务"
    assert created.title == "写周报"
    assert created.priority == Priority.HIGH
    assert created.estimated_minutes == 35
    assert set(created.tags or []) == {"工作", "例行"}
    assert created.status == TaskStatus.TODO
    assert created.due_at is None
    assert created.recurrence is None
    assert created.notes is None
    assert created.parent_task_id is None
    with store._connect() as conn:
        relations = conn.execute(
            "SELECT COUNT(*) FROM task_relations WHERE source_task_id = ? OR target_task_id = ?",
            (created.id, created.id),
        ).fetchone()[0]
    assert relations == 0
    assert _event_count(store, source.id) == events_before, "不得改写历史事件"
    assert store._get_task(source.id).status == TaskStatus.DONE, "旧任务必须保持已完成"


def test_post_rejects_unknown_and_unqualified_keys(api):
    store, base, tokens = api
    _complete(store, "只完成一次", user_id="alice")
    _complete(store, "每日站会", user_id="alice", recurrence="daily")
    _complete(store, "每日站会", user_id="alice", recurrence="weekly")
    for key in ("不存在的标题", "只完成一次", "每日站会"):
        status, payload = _request(base, tokens["alice"], "POST", "/api/frequent/recreate", {"key": key})
        assert status == 404, (key, status, payload)
    status, _payload = _request(base, tokens["alice"], "POST", "/api/frequent/recreate", {})
    assert status == 400


def test_post_cannot_touch_another_users_title(api):
    store, base, tokens = api
    _complete(store, "alice 的私事", user_id="alice")
    _complete(store, "alice 的私事", user_id="alice")
    status, _payload = _request(base, tokens["bob"], "POST", "/api/frequent/recreate", {"key": "alice 的私事"})
    assert status == 404


def test_recreate_normalizes_the_incoming_key(api):
    store, base, tokens = api
    _complete(store, "写周报", user_id="alice")
    _complete(store, "写周报", user_id="alice")
    status, payload = _request(base, tokens["alice"], "POST", "/api/frequent/recreate", {"key": "  写周报  "})
    assert status == 200, payload
    assert payload["task"]["title"] == "写周报"


def test_recreate_needs_no_idempotency_header(api):
    store, base, tokens = api
    _complete(store, "写周报", user_id="alice")
    _complete(store, "写周报", user_id="alice")
    status, _payload = _request(base, tokens["alice"], "POST", "/api/frequent/recreate", {"key": "写周报"})
    assert status == 200

