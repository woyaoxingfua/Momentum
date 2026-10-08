"""本地模式（未配置 AI provider）的 HTTP 路径。

README 描述的「本地模式 · 尚未连接模型」是真实存在的用户分段：没有 API Key 时，
对话会走本地自然语言解析、计划与回顾分支。此前这些分支没有任何 HTTP 层用例。

隔离要求：仓库根的 .env 会被 load_provider_config 读入，因此这里显式屏蔽 load_dotenv
并清空所有 provider 变量，确保真的处于「未配置」状态。
"""
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

from momentum_agent import config as config_module
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import MomentumHandler, _store_cache

PROVIDER_VARS = (
    "MOMENTUM_API_KEY", "OPENAI_API_KEY", "MOMENTUM_BASE_URL", "OPENAI_BASE_URL",
    "MOMENTUM_MODEL", "OPENAI_MODEL", "MOMENTUM_PROVIDER", "MOMENTUM_TEST_MYSQL_URL",
)


def call(base_url, method, path, *, token=None, payload=None, timeout=30):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as response:
        return response.code, response.read().decode("utf-8", "replace")


def parse_sse(raw: str) -> list[dict]:
    events = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk:
                events.append(json.loads(chunk))
    return events


@pytest.fixture
def local_http(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: None)
    for name in PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)
    assert config_module.load_provider_config({}).is_configured is False, "必须处于未配置 provider 的本地模式"

    database_path = tmp_path / "local-mode.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    SQLiteTaskStore(database_path)

    handler_type = type("LocalModeHandler", (MomentumHandler,), {"database_url": database_url})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        user_id = f"local-{uuid.uuid4().hex}"
        password = f"pw-{uuid.uuid4().hex}"
        status, _ = call(base_url, "POST", "/api/register",
                         payload={"user_id": user_id, "display_name": user_id, "password": password})
        assert status == HTTPStatus.OK
        status, raw = call(base_url, "POST", "/api/login", payload={"user_id": user_id, "password": password})
        assert status == HTTPStatus.OK, raw
        yield SimpleNamespace(
            base_url=base_url, token=json.loads(raw)["token"], database_path=database_path,
            store=SQLiteTaskStore(database_path), user_id=user_id,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)


def titles(local_http) -> list[str]:
    return [task.title for task in local_http.store.list_tasks(status=None, user_id=local_http.user_id)]


def test_local_mode_turns_a_sentence_into_a_task(local_http):
    status, raw = call(local_http.base_url, "POST", "/api/chat", token=local_http.token,
                       payload={"message": "明天下午三点交周报"})
    assert status == HTTPStatus.OK, raw
    reply = json.loads(raw)["message"]
    assert reply and isinstance(reply, str)
    assert any("周报" in title for title in titles(local_http)), titles(local_http)


def test_local_mode_plan_branch(local_http):
    status, raw = call(local_http.base_url, "POST", "/api/chat", token=local_http.token,
                       payload={"message": "帮我规划一下下周的发布安排"})
    assert status == HTTPStatus.OK, raw
    reply = json.loads(raw)["message"]
    assert reply and isinstance(reply, str)


def test_local_mode_review_branch(local_http):
    local_http.store.create_task("本地模式的任务")
    status, raw = call(local_http.base_url, "POST", "/api/chat", token=local_http.token,
                       payload={"message": "总结一下今天的情况"})
    assert status == HTTPStatus.OK, raw
    reply = json.loads(raw)["message"]
    assert reply and isinstance(reply, str)


def test_local_mode_stream_still_emits_the_sse_contract(local_http):
    status, raw = call(local_http.base_url, "POST", "/api/chat/stream", token=local_http.token,
                       payload={"message": "写一份季度文档"})
    assert status == HTTPStatus.OK, raw
    events = parse_sse(raw)
    kinds = [event.get("type") for event in events]
    assert "chunk" in kinds and kinds[-1] == "done", (kinds, raw[:200])
    assert any("季度文档" in title for title in titles(local_http)), titles(local_http)


def test_plan_endpoint_returns_message_and_tasks(local_http):
    status, raw = call(local_http.base_url, "POST", "/api/plan", token=local_http.token,
                       payload={"text": "发布一个新版本"})
    assert status == HTTPStatus.OK, raw
    payload = json.loads(raw)
    assert payload.get("message")
    assert "tasks" in payload


def test_advice_and_review_endpoints_answer(local_http):
    local_http.store.create_task("用于建议的任务")
    status, raw = call(local_http.base_url, "GET", "/api/advice", token=local_http.token)
    assert status == HTTPStatus.OK, raw
    advice = json.loads(raw)
    assert advice

    status, raw = call(
        local_http.base_url, "GET",
        "/api/review?timeZone=Asia%2FShanghai&localDate=2026-10-08", token=local_http.token,
    )
    assert status == HTTPStatus.OK, raw
    assert json.loads(raw)

