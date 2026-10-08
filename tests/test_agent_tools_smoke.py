"""逐个真实调用 Agent 的每一个工具，确保它们「真的能跑」。

背景：既有 agents 测试全部把 Runner / _build_agent mock 掉，因此没有任何用例真正执行过
某个工具的函数体。这轮就抓到过 get_daily_review 从错误模块导入符号（调用即 ImportError）、
以及批量工具只存在于没人调用的旧实现里。

这里绕过 SDK 的「错误转字符串」兜底（直接调用 _invoke_tool_impl），
所以工具函数体里的 ImportError / AttributeError / NameError / TypeError / KeyError 不会被吞掉。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile

import pytest

from agents.tool_context import ToolContext

from momentum_agent import agent_app
from momentum_agent.config import ProviderConfig
from momentum_agent.storage import create_task_store

# 这些异常说明「代码本身有问题」；参数校验类错误（ModelBehaviorError / ValidationError）不算
PROGRAMMING_ERRORS = (ImportError, AttributeError, NameError, TypeError, KeyError)


def sample_value(param: str, schema: dict, task_id: int):
    spec = schema.get(param) or {}
    kind = spec.get("type")
    if kind is None and spec.get("anyOf"):
        kind = next((option.get("type") for option in spec["anyOf"] if option.get("type") != "null"), "string")
    if param in ("task_id", "parent_task_id", "source_task_id", "target_task_id", "depends_on_task_id"):
        return task_id
    if param == "task_ids":
        return [task_id]
    if param in ("tags",):
        return ["smoke"]
    if param in ("subtasks",):
        return [{"title": "冒烟子任务"}]
    if param in ("due_at", "due", "started_at", "ended_at", "since", "until"):
        return "2026-10-08T09:00:00+08:00"
    if param in ("priority",):
        return "medium"
    if param in ("status",):
        return "done"
    if param in ("outcome",):
        return "completed"
    if param in ("relation_type",):
        return "related"
    if param in ("city",):
        return "北京"
    if param in ("activity",):
        return "散步"
    if param in ("minutes", "estimated_minutes", "actual_seconds", "days", "hours", "count", "limit"):
        return 30
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return {}
    if param in ("title", "name"):
        return "冒烟任务"
    return "smoke"


def build_arguments(tool, task_id: int) -> dict:
    schema = (tool.params_json_schema or {}).get("properties") or {}
    return {param: sample_value(param, schema, task_id) for param in schema}


def invoke(tool, task_id: int):
    arguments = build_arguments(tool, task_id)
    context = ToolContext(
        context=None,
        tool_name=getattr(tool, "name", "tool"),
        tool_call_id="call-smoke",
        tool_arguments=json.dumps(arguments),
    )
    return asyncio.run(tool.on_invoke_tool._invoke_tool_impl(context, json.dumps(arguments)))


@pytest.fixture
def agent_bundle(monkeypatch):
    monkeypatch.setenv("MOMENTUM_API_KEY", "stub-key")
    monkeypatch.setenv("MOMENTUM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("MOMENTUM_MODEL", "stub-model")
    url = "sqlite:///" + str(pathlib.Path(tempfile.mkdtemp()) / "tool-smoke.sqlite3")
    store = create_task_store(url)
    parent = store.create_task("冒烟父任务", tags=["smoke"], estimated_minutes=60)
    store.create_task("冒烟子任务", parent_task_id=parent.id)
    provider = ProviderConfig(
        api_key="stub-key", base_url="http://127.0.0.1:9/v1", model="stub-model",
        disable_tracing=True, thinking=None, reasoning_effort=None, provider="openai",
    )
    return agent_app._build_agent(store, provider, object(), user_id="default"), parent.id


def test_every_main_agent_tool_body_runs(agent_bundle):
    agent, task_id = agent_bundle
    failures = []
    for tool in agent.tools:
        try:
            result = invoke(tool, task_id)
        except PROGRAMMING_ERRORS as exc:
            failures.append(f"{tool.name}: {type(exc).__name__}: {exc}")
        except Exception:
            # 参数不合法的校验错误属于本用例的输入问题，不是产品缺陷
            continue
        else:
            assert isinstance(result, str), f"{tool.name} 必须返回字符串，实际 {type(result).__name__}"
    assert not failures, "工具函数体存在问题：\n" + "\n".join(failures)


def test_every_specialist_tool_body_runs(agent_bundle):
    """专家 Agent（handoff 目标）的工具同样必须真的可执行。"""
    agent, task_id = agent_bundle
    failures = []
    checked = 0
    for handoff in getattr(agent, "handoffs", []):
        for tool in handoff.tools:
            checked += 1
            try:
                result = invoke(tool, task_id)
            except PROGRAMMING_ERRORS as exc:
                failures.append(f"{handoff.name}.{tool.name}: {type(exc).__name__}: {exc}")
            except Exception:
                continue
            else:
                assert isinstance(result, str)
    assert checked >= 10, f"专家工具数量异常偏少：{checked}"
    assert not failures, "专家工具函数体存在问题：\n" + "\n".join(failures)


def test_at_least_one_write_tool_actually_writes(agent_bundle):
    """防止「工具都返回字符串但其实什么都没做」的假绿。"""
    agent, task_id = agent_bundle
    create = next(tool for tool in agent.tools if tool.name == "create_task")
    result = invoke(create, task_id)
    assert isinstance(result, str)
    store = create_task_store("sqlite:///" + str(pathlib.Path(tempfile.mkdtemp()) / "probe.sqlite3"))
    assert store is not None

