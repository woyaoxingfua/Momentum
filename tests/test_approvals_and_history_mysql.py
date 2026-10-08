"""新功能在真实 MySQL 上的行为：审批门禁与对话历史。

线上部署用的是 MySQL，这两项新功能都依赖 user_memory（键值读写）与 store 的事务语义，
因此除了 SQLite 用例之外，再在真实 MySQL 上验一遍；未配置 MOMENTUM_TEST_MYSQL_URL 时整体跳过。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from agents.tool_context import ToolContext

from backend_fixtures import fresh_mysql_store
from momentum_agent import agent_app, approvals
from momentum_agent.agents.tools import create_task_tools
from momentum_agent.models import TaskStatus


@pytest.fixture
def store():
    return fresh_mysql_store()


@pytest.fixture(autouse=True)
def clean_memory_cache():
    agent_app._conversation_history.clear()
    yield
    agent_app._conversation_history.clear()


def call_tool(store, user_id, name, arguments):
    tools = create_task_tools(store, user_id)
    tool = next(item for item in tools if getattr(item, "name", "") == name)
    context = ToolContext(
        context=None, tool_name=name, tool_call_id="call-1",
        tool_arguments=json.dumps(arguments),
    )
    return asyncio.run(tool.on_invoke_tool._invoke_tool_impl(context, json.dumps(arguments)))


def test_gate_records_pending_without_dropping_on_mysql(store, monkeypatch):
    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    task = store.create_task("MySQL 待审批", user_id="alice")

    message = call_tool(store, "alice", "drop_task", {"task_id": task.id})
    assert "需要你确认" in message, message
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.TODO
    pending = approvals.list_pending(store, "alice")
    assert len(pending) == 1 and pending[0]["arguments"] == {"task_id": task.id}


def test_approve_executes_exactly_once_on_mysql(store, monkeypatch):
    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    task = store.create_task("MySQL 批准", user_id="alice")
    call_tool(store, "alice", "drop_task", {"task_id": task.id})
    pending = approvals.list_pending(store, "alice")[0]

    message = approvals.execute(store, approvals.take_pending(store, "alice", pending["id"]), "alice")
    assert "已放弃" in message, message
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.DROPPED
    assert approvals.list_pending(store, "alice") == []
    assert approvals.take_pending(store, "alice", pending["id"]) is None


def test_reject_keeps_the_task_on_mysql(store, monkeypatch):
    monkeypatch.delenv("MOMENTUM_APPROVAL_REQUIRED_TOOLS", raising=False)
    task = store.create_task("MySQL 取消", user_id="alice")
    call_tool(store, "alice", "drop_task", {"task_id": task.id})
    pending = approvals.list_pending(store, "alice")[0]
    assert approvals.take_pending(store, "alice", pending["id"]) is not None
    assert store.get_task_for_user(task.id, "alice").status == TaskStatus.TODO


def test_chat_history_survives_a_restart_on_mysql(store):
    history = [{"role": "user", "content": "MySQL 提问"}, {"role": "assistant", "content": "MySQL 回答"}]
    agent_app._save_history("alice", history, store)
    agent_app._conversation_history.clear()

    assert agent_app._get_history("alice", store) == history
    assert agent_app.plain_chat_history("alice", store=store) == history


def test_chat_history_is_per_user_and_clearable_on_mysql(store):
    agent_app._save_history("alice", [{"role": "user", "content": "alice"}], store)
    agent_app._save_history("bob", [{"role": "user", "content": "bob"}], store)
    agent_app._conversation_history.clear()
    assert agent_app._get_history("alice", store)[0]["content"] == "alice"
    assert agent_app._get_history("bob", store)[0]["content"] == "bob"

    agent_app.clear_conversation_history("alice", store=store)
    agent_app._conversation_history.clear()
    assert agent_app._get_history("alice", store) == []
    assert agent_app._get_history("bob", store)[0]["content"] == "bob"

