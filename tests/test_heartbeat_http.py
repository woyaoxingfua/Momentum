"""心跳提醒的 HTTP 契约与间隔语义。

前端 heartbeat.js 每 60 秒轮询一次 /api/heartbeat/suggestion；服务端此前无条件调用
update_last_heartbeat()，于是「间隔」永远攒不满：首次之后 should_trigger 恒为 False，
README 里承诺的「起止小时 / 间隔」提醒实际上只会出现一次。这里把它钉死。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import MomentumHandler, _store_cache


def call(base_url, method, path, *, token=None, payload=None, timeout=20):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as response:
        return response.code, json.loads(response.read().decode("utf-8", "replace") or "{}")


@pytest.fixture
def heartbeat_http(tmp_path, monkeypatch):
    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    database_path = tmp_path / "heartbeat-http.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    SQLiteTaskStore(database_path)

    handler_type = type("HeartbeatHttpHandler", (MomentumHandler,), {"database_url": database_url})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        user_id = f"heartbeat-{uuid.uuid4().hex}"
        password = f"pw-{uuid.uuid4().hex}"
        status, _ = call(base_url, "POST", "/api/register",
                         payload={"user_id": user_id, "display_name": user_id, "password": password})
        assert status == HTTPStatus.OK
        status, payload = call(base_url, "POST", "/api/login",
                               payload={"user_id": user_id, "password": password})
        assert status == HTTPStatus.OK, payload
        yield SimpleNamespace(
            base_url=base_url, token=payload["token"], user_id=user_id,
            store=SQLiteTaskStore(database_path),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)


def enable(heartbeat_http, *, interval_hours=1, user_id=None):
    return call(
        heartbeat_http.base_url, "POST", "/api/heartbeat/config", token=heartbeat_http.token,
        payload={"enabled": True, "start_hour": 0, "end_hour": 23, "interval_hours": interval_hours},
    )


def test_config_defaults_and_clamping(heartbeat_http):
    status, payload = call(heartbeat_http.base_url, "GET", "/api/heartbeat/config", token=heartbeat_http.token)
    assert status == HTTPStatus.OK
    assert payload["config"]["enabled"] is False
    assert payload["config"]["interval_hours"] == 4

    status, payload = call(
        heartbeat_http.base_url, "POST", "/api/heartbeat/config", token=heartbeat_http.token,
        payload={"enabled": True, "start_hour": 99, "end_hour": -5, "interval_hours": 99},
    )
    assert status == HTTPStatus.OK, payload
    config = payload["config"]
    assert payload["status"] == "已启用"
    assert config["start_hour"] == 23 and config["end_hour"] == 0
    assert config["interval_hours"] == 24


def test_suggestion_advances_the_timer_only_when_it_actually_triggers(heartbeat_http):
    assert enable(heartbeat_http)[0] == HTTPStatus.OK

    status, first = call(heartbeat_http.base_url, "GET", "/api/heartbeat/suggestion", token=heartbeat_http.token)
    assert status == HTTPStatus.OK, first
    assert first["should_trigger"] is True, "首次应触发"
    assert first["suggestion"]
    stamp_after_first = first["config"]["last_heartbeat_at"]
    assert stamp_after_first, "触发后应记录时间"

    # 立刻再问一次（模拟前端 60 秒轮询）：不得触发，且计时器不得被推后
    status, second = call(heartbeat_http.base_url, "GET", "/api/heartbeat/suggestion", token=heartbeat_http.token)
    assert status == HTTPStatus.OK, second
    assert second["should_trigger"] is False, "间隔未到不应重复触发"
    assert second["config"]["last_heartbeat_at"] == stamp_after_first, (
        "未触发时不得更新 last_heartbeat_at，否则间隔永远攒不满"
    )


def test_suggestion_triggers_again_after_the_interval(heartbeat_http):
    assert enable(heartbeat_http, interval_hours=1)[0] == HTTPStatus.OK
    call(heartbeat_http.base_url, "GET", "/api/heartbeat/suggestion", token=heartbeat_http.token)

    # 把上次心跳时间拨回到 2 小时前，等价于「间隔已过」
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    config = heartbeat_http.store.get_heartbeat_config(user_id=heartbeat_http.user_id)
    config["last_heartbeat_at"] = past
    heartbeat_http.store.set_memory("heartbeat_config", json.dumps(config), user_id=heartbeat_http.user_id)

    status, payload = call(heartbeat_http.base_url, "GET", "/api/heartbeat/suggestion", token=heartbeat_http.token)
    assert status == HTTPStatus.OK, payload
    assert payload["should_trigger"] is True, "间隔已过应再次触发"
    assert payload["config"]["last_heartbeat_at"] != past


def test_suggestion_stays_quiet_when_disabled(heartbeat_http):
    status, first = call(heartbeat_http.base_url, "GET", "/api/heartbeat/suggestion", token=heartbeat_http.token)
    assert status == HTTPStatus.OK, first
    assert first["should_trigger"] is False, "未启用时不应触发"
    assert first["config"]["last_heartbeat_at"] is None

