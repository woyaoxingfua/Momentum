"""破坏性操作的审批门禁：单元 + 工具层 + HTTP + CLI。

归档的「未贸然接入」里写着：当前放弃等写工具会直接执行，若产品要求高风险确认，
应设计显式审批流与可恢复状态。这里验证落地后的行为：被保护的工具不直接执行，
而是留下可恢复的待确认记录，用户批准后才真正执行（且只执行一次）。
"""
from __future__ import annotations

import asyncio
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
from agents.tool_context import ToolContext

from momentum_agent import approvals
from momentum_agent.agents.tools import create_task_tools
from momentum_agent.models import TaskStatus
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import MomentumHandler, _store_cache


@pytest.fixture
def store():
    return SQLiteTaskStore(pathlib.Path(tempfile.mkdtemp()) / "approvals.sqlite3")


def call_tool(store, user_id, name, arguments):
    tools = create_task_tools(store, user_id)
    tool = next(item for item in tools if getattr(item, "name", "") == name)
    context = ToolContext(
        context=None, tool_name=name, tool_call_id="call-1",
        tool_arguments=json.dumps(arguments),
    )
    return asyncio.run(tool.on_invoke_tool._invoke_tool_impl(context, json.dumps(arguments)))


def test_gated_tool_records_a_pending_operation_and_does_not_drop(store, monkeypatch):
    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    task = store.create_task("需要审批的任务", user_id="alice")

    message = call_tool(store, "alice", "drop_task", {"task_id": task.id})

    assert "需要你确认" in message, message
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.TODO, "未批准前不得放弃"
    pending = approvals.list_pending(store, "alice")
    assert len(pending) == 1 and pending[0]["tool"] == "drop_task"
    assert pending[0]["arguments"] == {"task_id": task.id}


def test_repeat_request_reuses_the_same_pending_entry(store, monkeypatch):
    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    task = store.create_task("重复请求", user_id="alice")
    first = call_tool(store, "alice", "drop_task", {"task_id": task.id})
    second = call_tool(store, "alice", "drop_task", {"task_id": task.id})
    assert first == second
    assert len(approvals.list_pending(store, "alice")) == 1


def test_approve_executes_exactly_once(store, monkeypatch):
    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    task = store.create_task("批准后放弃", user_id="alice")
    call_tool(store, "alice", "drop_task", {"task_id": task.id})
    pending = approvals.list_pending(store, "alice")[0]

    message = approvals.execute(store, approvals.take_pending(store, "alice", pending["id"]), "alice")
    assert "已放弃" in message, message
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.DROPPED
    assert approvals.list_pending(store, "alice") == []
    assert approvals.take_pending(store, "alice", pending["id"]) is None, "同一条不得被执行两次"


def test_reject_leaves_the_task_untouched(store, monkeypatch):
    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    task = store.create_task("取消后保留", user_id="alice")
    call_tool(store, "alice", "drop_task", {"task_id": task.id})
    pending = approvals.list_pending(store, "alice")[0]

    assert approvals.take_pending(store, "alice", pending["id"]) is not None
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.TODO
    assert approvals.list_pending(store, "alice") == []


def test_gate_can_be_disabled(store, monkeypatch):
    monkeypatch.setenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", "none")
    task = store.create_task("关闭门禁", user_id="alice")
    message = call_tool(store, "alice", "drop_task", {"task_id": task.id})
    assert "已放弃" in message, message
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.DROPPED
    assert approvals.list_pending(store, "alice") == []


def test_pending_operations_are_per_user(store, monkeypatch):
    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    task = store.create_task("alice 的任务", user_id="alice")
    call_tool(store, "alice", "drop_task", {"task_id": task.id})
    assert len(approvals.list_pending(store, "alice")) == 1
    assert approvals.list_pending(store, "bob") == []
    assert approvals.take_pending(store, "bob", approvals.list_pending(store, "alice")[0]["id"]) is None


def test_gated_tool_reports_missing_task_without_creating_pending(store, monkeypatch):
    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    message = call_tool(store, "alice", "drop_task", {"task_id": 999})
    assert "不存在" in message, message
    assert approvals.list_pending(store, "alice") == []


# ── HTTP ────────────────────────────────────────────────────────

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


@pytest.fixture
def http_env(tmp_path, monkeypatch):
    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    database_path = tmp_path / "approvals-http.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    store = SQLiteTaskStore(database_path)
    handler_type = type("ApprovalsHttpHandler", (MomentumHandler,), {"database_url": database_url})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        user_id = f"approvals-{uuid.uuid4().hex}"
        password = f"pw-{uuid.uuid4().hex}"
        status, _ = http_call(base_url, "POST", "/api/register",
                              payload={"user_id": user_id, "display_name": user_id, "password": password})
        assert status == HTTPStatus.OK
        status, payload = http_call(base_url, "POST", "/api/login",
                                    payload={"user_id": user_id, "password": password})
        assert status == HTTPStatus.OK, payload
        yield SimpleNamespace(base_url=base_url, token=payload["token"], store=store, user_id=user_id)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)


def test_http_list_and_approve(http_env):
    task = http_env.store.create_task("HTTP 审批", user_id=http_env.user_id)
    http_env.store.set_memory(
        approvals.PENDING_MEMORY_KEY,
        json.dumps([{"id": "abc123", "tool": "drop_task", "arguments": {"task_id": task.id},
                     "summary": f"放弃任务 #{task.id}", "created_at": "2026-10-08T00:00:00+00:00"}]),
        user_id=http_env.user_id,
    )

    status, payload = http_call(http_env.base_url, "GET", "/api/approvals", token=http_env.token)
    assert status == HTTPStatus.OK, payload
    assert [item["id"] for item in payload["approvals"]] == ["abc123"]
    assert "drop_task" in payload["gated_tools"]

    status, payload = http_call(http_env.base_url, "POST", "/api/approvals/approve",
                                token=http_env.token, payload={"id": "abc123"})
    assert status == HTTPStatus.OK, payload
    assert "已放弃" in payload["message"], payload
    assert http_env.store.get_task_for_user(task.id, http_env.user_id).status == TaskStatus.DROPPED

    status, payload = http_call(http_env.base_url, "POST", "/api/approvals/approve",
                                token=http_env.token, payload={"id": "abc123"})
    assert status == HTTPStatus.NOT_FOUND, payload


def test_http_reject_and_validation(http_env):
    task = http_env.store.create_task("HTTP 取消", user_id=http_env.user_id)
    http_env.store.set_memory(
        approvals.PENDING_MEMORY_KEY,
        json.dumps([{"id": "keep01", "tool": "drop_task", "arguments": {"task_id": task.id},
                     "summary": "放弃任务", "created_at": "2026-10-08T00:00:00+00:00"}]),
        user_id=http_env.user_id,
    )
    status, payload = http_call(http_env.base_url, "POST", "/api/approvals/reject",
                                token=http_env.token, payload={"id": "keep01"})
    assert status == HTTPStatus.OK, payload
    assert "已取消" in payload["message"]
    assert http_env.store.get_task_for_user(task.id, http_env.user_id).status == TaskStatus.TODO

    status, payload = http_call(http_env.base_url, "POST", "/api/approvals/approve",
                                token=http_env.token, payload={})
    assert status == HTTPStatus.BAD_REQUEST, payload

