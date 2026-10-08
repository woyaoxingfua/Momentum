"""对话历史持久化：重启与多 worker 之后仍然记得上下文。

归档的「会话存储边界」指出历史只在进程内存里，重启即丢。这里验证落库后的行为，
并确认清空操作同时清掉内存与存储。
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from momentum_agent import agent_app
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import MomentumHandler, _store_cache


@pytest.fixture
def store():
    return SQLiteTaskStore(pathlib.Path(tempfile.mkdtemp()) / "history.sqlite3")


@pytest.fixture(autouse=True)
def clean_memory_cache():
    agent_app._conversation_history.clear()
    yield
    agent_app._conversation_history.clear()


def test_history_survives_a_process_restart(store):
    history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在的"}]
    agent_app._save_history("alice", history, store)

    # 模拟重启：内存缓存全空，只剩数据库
    agent_app._conversation_history.clear()

    assert agent_app._get_history("alice", store) == history


def test_history_is_truncated_to_the_cap(store):
    history = [{"role": "user", "content": f"第{index}条"} for index in range(agent_app.MAX_HISTORY_ITEMS + 25)]
    agent_app._save_history("alice", history, store)
    agent_app._conversation_history.clear()

    restored = agent_app._get_history("alice", store)
    assert len(restored) == agent_app.MAX_HISTORY_ITEMS
    assert restored[-1]["content"] == history[-1]["content"]


def test_history_is_per_user(store):
    agent_app._save_history("alice", [{"role": "user", "content": "alice 的"}], store)
    agent_app._save_history("bob", [{"role": "user", "content": "bob 的"}], store)
    agent_app._conversation_history.clear()

    assert agent_app._get_history("alice", store)[0]["content"] == "alice 的"
    assert agent_app._get_history("bob", store)[0]["content"] == "bob 的"


def test_clear_removes_memory_and_storage(store):
    agent_app._save_history("alice", [{"role": "user", "content": "将被清空"}], store)
    agent_app.clear_conversation_history("alice", store=store)
    agent_app._conversation_history.clear()

    assert agent_app._get_history("alice", store) == []
    assert store.get_memory(agent_app.CHAT_HISTORY_MEMORY_KEY, user_id="alice") in ("", None)


def test_without_a_store_it_still_works_in_memory():
    agent_app._save_history("alice", [{"role": "user", "content": "仅内存"}])
    assert agent_app._get_history("alice")[0]["content"] == "仅内存"
    agent_app.clear_conversation_history("alice")
    assert agent_app._get_history("alice") == []


def test_corrupted_persisted_history_degrades_to_empty(store):
    store.set_memory(agent_app.CHAT_HISTORY_MEMORY_KEY, "{not json", user_id="alice")
    assert agent_app._get_history("alice", store) == []
    store.set_memory(agent_app.CHAT_HISTORY_MEMORY_KEY, json.dumps({"unexpected": "shape"}), user_id="alice")
    assert agent_app._get_history("alice", store) == []


# ── HTTP：/api/chat/clear 必须连持久化历史一起清 ────────────────

def http_call(base_url, method, path, *, token=None, payload=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as response:
        return response.code, json.loads(response.read().decode("utf-8", "replace") or "{}")


def test_http_chat_clear_also_clears_persisted_history(tmp_path, monkeypatch):
    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    database_path = tmp_path / "history-http.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    store = SQLiteTaskStore(database_path)
    handler_type = type("HistoryHttpHandler", (MomentumHandler,), {"database_url": database_url})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        user_id = f"history-{uuid.uuid4().hex}"
        password = f"pw-{uuid.uuid4().hex}"
        status, _ = http_call(base_url, "POST", "/api/register",
                              payload={"user_id": user_id, "display_name": user_id, "password": password})
        assert status == HTTPStatus.OK
        status, payload = http_call(base_url, "POST", "/api/login",
                                    payload={"user_id": user_id, "password": password})
        assert status == HTTPStatus.OK, payload
        token = payload["token"]

        store.set_memory(agent_app.CHAT_HISTORY_MEMORY_KEY,
                         json.dumps([{"role": "user", "content": "持久化历史"}]), user_id=user_id)
        agent_app._conversation_history[user_id] = [{"role": "user", "content": "持久化历史"}]

        status, payload = http_call(base_url, "POST", "/api/chat/clear", token=token)
        assert status == HTTPStatus.OK, payload
        assert agent_app._get_history(user_id, store) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)

