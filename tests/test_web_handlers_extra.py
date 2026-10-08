"""web/handlers.py 里此前没有任何测试覆盖的 HTTP 处理器。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import MomentumHandler, _store_cache


def call(base_url, method, path, *, token=None, payload=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as response:
        return response.code, json.loads(response.read().decode("utf-8", "replace") or "{}")


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    database_path = tmp_path / "handlers-extra.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    store = SQLiteTaskStore(database_path)
    handler_type = type("ExtraHandlers", (MomentumHandler,), {"database_url": database_url})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        user_id = f"extra-{uuid.uuid4().hex}"
        password = f"pw-{uuid.uuid4().hex}"
        call(base_url, "POST", "/api/register",
             payload={"user_id": user_id, "display_name": user_id, "password": password})
        status, payload = call(base_url, "POST", "/api/login",
                                payload={"user_id": user_id, "password": password})
        assert status == HTTPStatus.OK, payload
        yield SimpleNamespace(base_url=base_url, token=payload["token"], store=store, user_id=user_id)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)

GUARDED_GET_ROUTES = ["/api/tags", "/api/config", "/api/preferences", "/api/user/location", "/api/focus/stats", "/api/notifications/upcoming", "/api/export"]


@pytest.mark.parametrize("path", GUARDED_GET_ROUTES)
def test_protected_routes_require_authentication(api, path):
    status, _payload = call(api.base_url, "GET", path)
    assert status == HTTPStatus.UNAUTHORIZED, path


def test_tags_reflect_created_tasks(api):
    api.store.create_task("带标签的任务", tags=["工作", "紧急"], user_id=api.user_id)
    status, payload = call(api.base_url, "GET", "/api/tags", token=api.token)
    assert status == HTTPStatus.OK, payload
    assert set(payload["tags"]) >= {"工作", "紧急"}, payload


def test_config_get_and_set_round_trip(api):
    status, payload = call(api.base_url, "GET", "/api/config", token=api.token)
    assert status == HTTPStatus.OK and "config" in payload, payload

    status, payload = call(api.base_url, "POST", "/api/config", token=api.token,
                           payload={"key": "daily_capacity_minutes", "value": "300"})
    assert status == HTTPStatus.OK, payload
    status, payload = call(api.base_url, "GET", "/api/config", token=api.token)
    assert "300" in json.dumps(payload, ensure_ascii=False), payload

    status, payload = call(api.base_url, "POST", "/api/config", token=api.token, payload={"key": ""})
    assert status == HTTPStatus.BAD_REQUEST, payload


def test_preferences_validation_and_round_trip(api):
    status, payload = call(api.base_url, "GET", "/api/preferences", token=api.token)
    assert status == HTTPStatus.OK and "theme" in payload and "background" in payload, payload

    status, payload = call(api.base_url, "POST", "/api/preferences", token=api.token,
                           payload={"theme": "sage"})
    assert status == HTTPStatus.OK, payload
    status, payload = call(api.base_url, "GET", "/api/preferences", token=api.token)
    assert payload["theme"] == "sage", payload

    status, payload = call(api.base_url, "POST", "/api/preferences", token=api.token,
                           payload={"theme": "bogus"})
    assert status == HTTPStatus.BAD_REQUEST, payload
    status, payload = call(api.base_url, "POST", "/api/preferences", token=api.token,
                           payload={"unexpected": 1})
    assert status == HTTPStatus.BAD_REQUEST, payload


def test_location_default_then_update(api):
    status, payload = call(api.base_url, "GET", "/api/user/location", token=api.token)
    assert status == HTTPStatus.OK and payload["is_default"] is True, payload
    assert payload["city"], payload

    status, payload = call(api.base_url, "POST", "/api/user/location", token=api.token,
                           payload={"city": "上海", "country": "中国", "latitude": 31.23, "longitude": 121.47})
    assert status == HTTPStatus.OK, payload
    status, payload = call(api.base_url, "GET", "/api/user/location", token=api.token)
    assert payload["city"] == "上海" and payload["is_default"] is False, payload

    status, payload = call(api.base_url, "POST", "/api/user/location", token=api.token, payload={"city": ""})
    assert status == HTTPStatus.BAD_REQUEST, payload


def test_focus_stats_shape_without_sessions(api):
    status, payload = call(api.base_url, "GET", "/api/focus/stats", token=api.token)
    assert status == HTTPStatus.OK, payload
    assert isinstance(payload, dict) and payload, payload


def test_upcoming_notifications_empty_without_due_tasks(api):
    api.store.create_task("没有截止日的任务", user_id=api.user_id)
    status, payload = call(api.base_url, "GET", "/api/notifications/upcoming", token=api.token)
    assert status == HTTPStatus.OK, payload
    assert payload["notifications"] == [], payload


def test_export_includes_created_tasks(api):
    api.store.create_task("导出里的任务", user_id=api.user_id)
    status, payload = call(api.base_url, "GET", "/api/export", token=api.token)
    assert status == HTTPStatus.OK, payload
    titles = [task.get("title") for task in payload.get("tasks", [])]
    assert "导出里的任务" in titles, payload


def test_batch_endpoints_apply_to_the_given_tasks(api):
    first = api.store.create_task("批量一", user_id=api.user_id)
    second = api.store.create_task("批量二", user_id=api.user_id)

    status, payload = call(api.base_url, "POST", "/api/batch/add-tags", token=api.token,
                           payload={"task_ids": [first.id, second.id], "tags": ["批处理"]})
    assert status == HTTPStatus.OK, payload
    assert all("批处理" in (api.store.get_task_for_user(task_id, api.user_id).tags or [])
               for task_id in (first.id, second.id))

    status, payload = call(api.base_url, "POST", "/api/batch/update-status", token=api.token,
                           payload={"task_ids": [first.id, second.id], "status": "done"})
    assert status == HTTPStatus.OK, payload
    assert all(api.store.get_task_for_user(task_id, api.user_id).status.value == "done"
               for task_id in (first.id, second.id))

    status, payload = call(api.base_url, "POST", "/api/batch/update-status", token=api.token,
                           payload={"task_ids": [], "status": "done"})
    assert status == HTTPStatus.BAD_REQUEST, payload

