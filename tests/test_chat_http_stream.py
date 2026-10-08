"""HTTP 层对话端到端：真实 dispatcher + 真实 agent_app + 假 provider。

此前没有任何用例穿过「HTTP 入口 → agent_app → SDK → 工具 → 数据库」这一整条链：
Web 侧测试把 agent_app 整个 mock 掉，agent 侧测试又不经过 HTTP 与鉴权。
而前端（含移动端）是按 SSE 的 data: 帧来解析的，这条契约必须被真正验证。

这里用进程内 ThreadingHTTPServer 起真实 handler，provider 指向本地假 provider，
因此离线可跑、不会打到真实 API。
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

from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import MomentumHandler, _store_cache
from stub_provider import stub_provider  # noqa: F401  pytest fixture


def call(base_url, method, path, *, token=None, payload=None, timeout=40):
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


def task_titles(database_path) -> list[str]:
    import sqlite3

    with sqlite3.connect(str(database_path)) as connection:
        return [row[0] for row in connection.execute("SELECT title FROM tasks")]


@pytest.fixture
def chat_http(tmp_path, monkeypatch, stub_provider):
    monkeypatch.setenv("MOMENTUM_API_KEY", "stub-key")
    monkeypatch.setenv("MOMENTUM_BASE_URL", stub_provider.base_url)
    monkeypatch.setenv("MOMENTUM_MODEL", "stub-model")
    monkeypatch.setenv("MOMENTUM_DISABLE_TRACING", "true")
    monkeypatch.setenv("MOMENTUM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)

    from momentum_agent.config import load_provider_config

    resolved = load_provider_config({})
    assert resolved.base_url and resolved.base_url.startswith(stub_provider.base_url), (
        "拒绝在非本地 stub 上跑对话测试：" + str(resolved.base_url)
    )

    database_path = tmp_path / "chat-http.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    SQLiteTaskStore(database_path)

    handler_type = type("ChatHttpHandler", (MomentumHandler,), {"database_url": database_url})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        user_id = f"chat-http-{uuid.uuid4().hex}"
        password = f"pw-{uuid.uuid4().hex}"
        status, raw = call(base_url, "POST", "/api/register",
                           payload={"user_id": user_id, "display_name": user_id, "password": password})
        assert status == HTTPStatus.OK, raw
        status, raw = call(base_url, "POST", "/api/login",
                           payload={"user_id": user_id, "password": password})
        assert status == HTTPStatus.OK, raw
        token = json.loads(raw)["token"]
        yield SimpleNamespace(
            base_url=base_url, token=token, database_path=database_path,
            user_id=user_id, server=stub_provider.server,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)


def test_chat_endpoint_requires_authentication(chat_http):
    status, raw = call(chat_http.base_url, "POST", "/api/chat", payload={"message": "你好"})
    assert status == HTTPStatus.UNAUTHORIZED, (status, raw)


def test_chat_endpoint_rejects_an_empty_message(chat_http):
    status, raw = call(chat_http.base_url, "POST", "/api/chat", token=chat_http.token, payload={"message": "   "})
    assert status == HTTPStatus.BAD_REQUEST, (status, raw)
    assert "error" in json.loads(raw)


def test_chat_endpoint_returns_the_agent_reply(chat_http):
    chat_http.server.script = [{"text": "来自假 provider 的 HTTP 回复"}]
    status, raw = call(chat_http.base_url, "POST", "/api/chat", token=chat_http.token, payload={"message": "你好"})
    assert status == HTTPStatus.OK, raw
    assert json.loads(raw) == {"message": "来自假 provider 的 HTTP 回复"}


def test_chat_stream_endpoint_emits_the_sse_contract(chat_http):
    chat_http.server.script = [{"text": "流式回复内容", "pieces": ["流式", "回复", "内容"]}]
    status, raw = call(chat_http.base_url, "POST", "/api/chat/stream", token=chat_http.token, payload={"message": "说点什么"})
    assert status == HTTPStatus.OK, raw
    events = parse_sse(raw)
    assert events, "SSE 没有任何 data: 帧：" + raw[:200]
    kinds = [event.get("type") for event in events]
    assert kinds[-1] == "done", kinds
    text = "".join(event.get("text", "") for event in events if event.get("type") == "chunk")
    assert text == "流式回复内容", text
    assert kinds.count("chunk") >= 2, "必须是增量推送，而不是一次性：" + str(kinds)


def test_chat_stream_tool_call_reaches_the_database(chat_http):
    chat_http.server.script = [
        {"tool": {"name": "create_task", "arguments": {"title": "HTTP 链路上的任务"}}},
        {"text": "已创建"},
    ]
    status, raw = call(chat_http.base_url, "POST", "/api/chat/stream", token=chat_http.token, payload={"message": "建个任务"})
    assert status == HTTPStatus.OK, raw
    events = parse_sse(raw)
    kinds = [event.get("type") for event in events]
    assert "tool_start" in kinds and "tool_end" in kinds, kinds
    tool_events = [event for event in events if event.get("type") == "tool_start"]
    assert any(event.get("name") == "create_task" for event in tool_events), tool_events
    assert "HTTP 链路上的任务" in task_titles(chat_http.database_path)


def test_chat_stream_reports_provider_failure_as_an_error_event(chat_http):
    chat_http.server.script = [
        {"tool": {"name": "create_task", "arguments": {"title": "只应写一次"}}},
        {"abort": True},
    ]
    status, raw = call(chat_http.base_url, "POST", "/api/chat/stream", token=chat_http.token, payload={"message": "建一个"})
    assert status == HTTPStatus.OK, raw
    events = parse_sse(raw)
    kinds = [event.get("type") for event in events]
    assert "error" in kinds, kinds
    assert "done" in kinds, "即使失败也要给前端一个结束信号：" + str(kinds)
    assert task_titles(chat_http.database_path).count("只应写一次") == 1

