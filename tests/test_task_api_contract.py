from __future__ import annotations

import json
import socket
import threading
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from http import HTTPStatus

import pytest

from momentum_agent.auth import hash_password
from momentum_agent.models import TaskStatus
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import MomentumHandler, _store_cache


@pytest.fixture
def task_api(tmp_path):
    database_path = tmp_path / "task-api-contract.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    store = SQLiteTaskStore(database_path)
    for user_id in ("alice", "bob"):
        store.register_user(user_id, user_id, hash_password("task-api-contract-password"))
    tokens = {
        user_id: store.login_user(user_id, "task-api-contract-password")
        for user_id in ("alice", "bob")
    }
    assert all(tokens.values())

    handler_type = type("TaskApiContractHandler", (MomentumHandler,), {"database_url": database_url})
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


def _request(base_url, token, method, path, payload=None, idempotency_key=None):
    body = json.dumps(payload or {}).encode("utf-8") if method in {"PUT", "POST"} else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if method == "POST" and path.endswith("/postpone"):
        headers["Idempotency-Key"] = (
            idempotency_key if idempotency_key is not None else str(uuid.uuid4())
        )
    request = urllib.request.Request(
        base_url + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as response:
        return response.code, json.loads(response.read().decode("utf-8"))


def _event_count(store, task_id, event_type=None):
    with store._connect() as conn:
        if event_type is None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND event_type = ?",
                (task_id, event_type),
            ).fetchone()
    return row["n"]


def test_put_missing_and_foreign_tasks_have_same_private_404(task_api):
    store, base_url, tokens = task_api
    due_at = datetime(2026, 3, 4, tzinfo=timezone.utc)
    task_a = store.create_task(
        "PRIVATE A TITLE",
        due_at=due_at,
        user_id="alice",
    )
    before_task = store.get_task_for_user(task_a.id, "alice")
    before_events = _event_count(store, task_a.id)

    foreign_status, foreign_body = _request(
        base_url, tokens["bob"], "PUT", f"/api/tasks/{task_a.id}", {"title": "attacker edit"}
    )
    missing_status, missing_body = _request(
        base_url, tokens["bob"], "PUT", "/api/tasks/99999999", {"title": "missing edit"}
    )

    assert foreign_status == missing_status == HTTPStatus.NOT_FOUND
    assert foreign_body == missing_body == {"error": "没有找到这个任务。"}
    serialized_response = json.dumps(foreign_body, ensure_ascii=False)
    assert "PRIVATE A TITLE" not in serialized_response
    assert due_at.isoformat() not in serialized_response
    assert due_at.strftime("%Y-%m-%d") not in serialized_response
    assert store.get_task_for_user(task_a.id, "alice") == before_task
    assert _event_count(store, task_a.id) == before_events
    assert _event_count(store, task_a.id, "updated") == 0


def test_put_owned_task_keeps_success_contract(task_api):
    store, base_url, tokens = task_api
    task = store.create_task("before", user_id="alice")

    status, body = _request(
        base_url, tokens["alice"], "PUT", f"/api/tasks/{task.id}", {"title": "after"}
    )

    assert status == HTTPStatus.OK
    assert set(body) == {"message"}
    assert "after" in body["message"]
    assert store.get_task_for_user(task.id, "alice").title == "after"
    assert _event_count(store, task.id, "updated") == 1


def test_postpone_missing_and_foreign_tasks_return_private_404_without_events(task_api):
    store, base_url, tokens = task_api
    due_at = datetime(2026, 4, 5, tzinfo=timezone.utc)
    task_a = store.create_task(
        "PRIVATE A DEADLINE TITLE",
        due_at=due_at,
        user_id="alice",
    )
    before_task = store.get_task_for_user(task_a.id, "alice")
    before_events = _event_count(store, task_a.id)

    foreign_status, foreign_body = _request(
        base_url, tokens["bob"], "POST", f"/api/tasks/{task_a.id}/postpone", {"days": 1}
    )
    missing_status, missing_body = _request(
        base_url, tokens["bob"], "POST", "/api/tasks/99999999/postpone", {"days": 1}
    )

    assert foreign_status == missing_status == HTTPStatus.NOT_FOUND
    assert foreign_body == missing_body == {"error": "没有找到这个任务。"}
    serialized_response = json.dumps(foreign_body, ensure_ascii=False)
    assert "PRIVATE A DEADLINE TITLE" not in serialized_response
    assert due_at.isoformat() not in serialized_response
    assert due_at.strftime("%Y-%m-%d") not in serialized_response
    assert store.get_task_for_user(task_a.id, "alice") == before_task
    assert _event_count(store, task_a.id) == before_events
    assert _event_count(store, task_a.id, "updated") == 0


@pytest.mark.parametrize("has_due,status", [(False, "todo"), (True, "done")])
def test_postpone_owned_ineligible_task_returns_409_without_writes(task_api, has_due, status):
    store, base_url, tokens = task_api
    due = datetime(2026, 5, 6, 12, tzinfo=timezone.utc) if has_due else None
    task = store.create_task("owned but ineligible", due_at=due, user_id="alice")
    if status == "done":
        store.update_status(task.id, TaskStatus.DONE, user_id="alice")
    before = store.get_task_for_user(task.id, "alice")
    before_events = _event_count(store, task.id)

    response_status, body = _request(
        base_url, tokens["alice"], "POST", f"/api/tasks/{task.id}/postpone", {"days": 1}
    )

    after = store.get_task_for_user(task.id, "alice")
    assert response_status == HTTPStatus.CONFLICT
    assert body == {"error": "该任务当前无法顺延截止日。"}
    assert after.due_at == before.due_at
    assert after.status == before.status
    assert after.updated_at == before.updated_at
    assert _event_count(store, task.id) == before_events


def test_postpone_owned_open_task_adds_requested_day(task_api):
    store, base_url, tokens = task_api
    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("owned deadline", due_at=due, user_id="alice")
    store.update_status(task.id, TaskStatus.DOING, user_id="alice")

    status, body = _request(
        base_url, tokens["alice"], "POST", f"/api/tasks/{task.id}/postpone", {"days": 1}
    )

    assert status == HTTPStatus.OK
    assert set(body) == {"message"}
    assert "2026-06-08 09:15" in body["message"]
    updated = store.get_task_for_user(task.id, "alice")
    assert updated.due_at == due + timedelta(days=1)
    assert updated.status == TaskStatus.DOING
    assert _event_count(store, task.id, "updated") == 1


def test_import_eof_truncated_json_returns_400_without_database_writes(task_api):
    store, base_url, tokens = task_api
    existing = store.create_task("existing alice task", user_id="alice")
    assert existing.id

    def database_snapshot():
        with store._connect() as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                ).fetchall()
            ]
            return {
                table: tuple(
                    sorted(
                        (tuple(row) for row in conn.execute(f' SELECT * FROM "{table}"')),
                        key=repr,
                    )
                )
                for table in tables
            }

    before = database_snapshot()
    port = int(base_url.rsplit(":", 1)[1])
    truncated_body = b'{"data":{"version":"2.0","tasks":['
    declared_length = len(truncated_body) + 16
    request = (
        f"POST /api/import HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {tokens['alice']}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {declared_length}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + truncated_body

    response_parts = []
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.settimeout(5)
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response_parts.append(chunk)

    raw_response = b"".join(response_parts)
    response_headers, response_body = raw_response.split(b"\r\n\r\n", 1)
    status_line = response_headers.split(b"\r\n", 1)[0]
    assert int(status_line.split()[1]) == HTTPStatus.BAD_REQUEST
    assert json.loads(response_body.decode("utf-8")) == {"error": "请提供 JSON 数据。"}
    assert database_snapshot() == before
