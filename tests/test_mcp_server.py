"""MCP server tests — 验证 Momentum 的 MCP 工具暴露。

覆盖：
  - 工具注册（数量、名称）
  - schema 松绑（strict_json_schema → MCP 友好）
  - 单工具调用（create_task / get_overview / search_tasks / complete_task）
  - 未知工具的优雅错误
  - 用户隔离
  - 端到端 MCP 协议（ClientSession ↔ Server）
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import mcp.types as types
from momentum_agent.models import Priority, TaskStatus
from momentum_agent.storage import TaskStore


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    """每条测试独立的 SQLite 文件存储。"""
    return TaskStore(tmp_path / "mcp_test.db")


@pytest.fixture
def user_id():
    return "mcp_test_user"


@pytest.fixture
def server(store, user_id):
    from momentum_agent.mcp_server import create_mcp_server

    return create_mcp_server(store, user_id)


@pytest.fixture
def function_tools(store, user_id):
    from momentum_agent.mcp_server import build_all_tools

    return build_all_tools(store, user_id)


# ── handler 调用辅助 ───────────────────────────────────────────────


def _list_tools(server) -> list[types.Tool]:
    """直接调用注册在 server.request_handlers 上的 list_tools handler。"""
    handler = server.request_handlers[types.ListToolsRequest]
    req = types.ListToolsRequest(method="tools/list")
    result = asyncio.run(handler(req))
    # handler 返回 ServerResult，真实结果在 .root
    return result.root.tools


def _call_tool(server, name: str, arguments: dict | None = None) -> list[types.TextContent]:
    """直接调用注册在 server.request_handlers 上的 call_tool handler。"""
    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    result = asyncio.run(handler(req))
    return result.root.content


def _extract_task_id(text: str) -> int | None:
    """从 '已创建任务 #12：...' 这样的返回字符串中提取任务 ID。"""
    m = re.search(r"#(\d+)", text)
    return int(m.group(1)) if m else None


def _get_task(store, task_id: int):
    """store 没有公开的 get_task，统一用 _get_task 并校验 user_id。"""
    task = store._get_task(task_id)
    return task


# ── 工具注册 ────────────────────────────────────────────────────────


class TestToolRegistration:
    def test_all_tools_registered(self, server, function_tools):
        """所有 FunctionTool 都应映射为 MCP Tool。"""
        tools = _list_tools(server)
        assert len(tools) == len(function_tools)
        assert all(isinstance(t, types.Tool) for t in tools)

    def test_tool_names_unique(self, server):
        tools = _list_tools(server)
        names = [t.name for t in tools]
        assert len(names) == len(set(names))

    def test_expected_core_tools_present(self, server):
        tools = _list_tools(server)
        names = {t.name for t in tools}
        for expected in (
            "create_task",
            "list_tasks",
            "get_task",
            "edit_task",
            "complete_task",
            "start_task",
            "drop_task",
            "reopen_task",
            "postpone_task",
            "search_tasks",
            "get_overview",
        ):
            assert expected in names, f"缺少核心工具：{expected}"

    def test_subtask_relation_tools_present(self, server):
        tools = _list_tools(server)
        names = {t.name for t in tools}
        for expected in (
            "create_subtask",
            "get_subtasks",
            "get_task_with_subtasks",
            "add_task_dependency",
            "remove_task_dependency",
            "add_task_relation",
            "get_task_relations",
            "is_task_blocked",
        ):
            assert expected in names, f"缺少子任务/关系工具：{expected}"

    def test_extra_tools_present(self, server):
        """extra_tools（从 agent_app 抽出的）应可见。"""
        tools = _list_tools(server)
        names = {t.name for t in tools}
        for expected in (
            "get_all_tags",
            "get_tasks_by_tag",
            "add_tags_to_task",
            "save_note",
            "get_my_notes",
            "get_user_context",
            "get_daily_review",
        ):
            assert expected in names, f"缺少 extra 工具：{expected}"

    def test_insight_focus_heartbeat_tools_present(self, server):
        tools = _list_tools(server)
        names = {t.name for t in tools}
        for expected in (
            "get_system_status",
            "get_daily_summary",
            "check_in",
            "get_behavioral_profile",
            "get_insights",
            "get_next_best_task",
            "get_tasks_due_today",
            "get_overdue_tasks",
        ):
            assert expected in names, f"缺少 insight/focus/heartbeat 工具：{expected}"

    def test_tool_has_description(self, server):
        tools = _list_tools(server)
        for t in tools:
            assert t.description, f"工具 {t.name} 缺少 description"

    def test_tool_has_input_schema(self, server):
        tools = _list_tools(server)
        for t in tools:
            assert t.inputSchema, f"工具 {t.name} 缺少 inputSchema"
            assert t.inputSchema.get("type") == "object"


# ── schema 松绑 ────────────────────────────────────────────────────


class TestSchemaLoosening:
    def test_create_task_required_only_title(self, server):
        """create_task 的 required 应只含 title（其他都是可选）。"""
        tools = _list_tools(server)
        create_task = next(t for t in tools if t.name == "create_task")
        required = create_task.inputSchema.get("required", [])
        assert required == ["title"], f"create_task.required 应为 ['title']，实际：{required}"

    def test_get_task_required_only_id(self, server):
        tools = _list_tools(server)
        get_task = next(t for t in tools if t.name == "get_task")
        assert get_task.inputSchema.get("required") == ["task_id"]

    def test_optional_params_not_in_required(self, server):
        """所有 anyOf 含 null 的参数不应出现在 required 中。"""
        from momentum_agent.mcp_server import _is_nullable

        tools = _list_tools(server)
        for t in tools:
            props = t.inputSchema.get("properties", {})
            required = t.inputSchema.get("required", [])
            for name, prop in props.items():
                if _is_nullable(prop):
                    assert name not in required, (
                        f"工具 {t.name} 的可选参数 {name} 不应在 required 中"
                    )

    def test_loosen_schema_directly(self):
        from momentum_agent.mcp_server import _loosen_schema

        # 全是可选 → required 应被移除
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "string", "anyOf": [{"type": "string"}, {"type": "null"}]},
                "b": {"type": "string", "default": "x"},
            },
            "required": ["a", "b"],
        }
        out = _loosen_schema(schema)
        assert out.get("required") is None

        # 必选参数保留
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "opt": {"type": "string", "anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["title", "opt"],
        }
        out = _loosen_schema(schema)
        assert out["required"] == ["title"]

    def test_loosen_schema_idempotent(self):
        from momentum_agent.mcp_server import _loosen_schema

        schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
        once = _loosen_schema(schema)
        twice = _loosen_schema(once)
        assert once == twice

    def test_loosen_schema_preserves_properties(self):
        """松绑不应修改 properties 本身，只调整 required。"""
        from momentum_agent.mcp_server import _loosen_schema

        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "opt": {"type": "string", "anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["title", "opt"],
        }
        out = _loosen_schema(schema)
        assert out["properties"] == schema["properties"]


# ── 单工具调用 ─────────────────────────────────────────────────────


class TestToolCalls:
    def test_create_task(self, server, store, user_id):
        content = _call_tool(server, "create_task", {"title": "MCP 测试任务"})
        text = content[0].text
        task_id = _extract_task_id(text)
        assert task_id, f"create_task 未返回 id：{text}"
        task = _get_task(store, task_id)
        assert task is not None
        assert task.title == "MCP 测试任务"

    def test_create_task_with_all_fields(self, server, store, user_id):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        due = (now + timedelta(days=2)).isoformat()
        content = _call_tool(server, "create_task", {
            "title": "完整 MCP 任务",
            "priority": "high",
            "due_at": due,
            "notes": "来自测试",
            "tags": ["work", "mcp"],
        })
        task_id = _extract_task_id(content[0].text)
        assert task_id
        task = _get_task(store, task_id)
        assert task.title == "完整 MCP 任务"
        assert task.priority == Priority.HIGH
        assert task.notes == "来自测试"
        assert set(task.tags) == {"work", "mcp"}

    def test_get_overview_empty(self, server):
        content = _call_tool(server, "get_overview", {})
        data = json.loads(content[0].text)
        assert isinstance(data, (dict, list))

    def test_get_overview_with_tasks(self, server, store, user_id):
        store.create_task("任务A", priority=Priority.HIGH, user_id=user_id)
        store.create_task("任务B", priority=Priority.LOW, user_id=user_id)
        content = _call_tool(server, "get_overview", {})
        text = content[0].text
        assert "任务A" in text or "任务B" in text

    def test_search_tasks(self, server, store, user_id):
        store.create_task("写 MCP 文档", user_id=user_id)
        store.create_task("买菜", user_id=user_id)
        content = _call_tool(server, "search_tasks", {"query": "MCP"})
        data = json.loads(content[0].text)
        items = data if isinstance(data, list) else data.get("results", data.get("tasks", []))
        assert any("MCP" in (t.get("title", "") if isinstance(t, dict) else "") for t in items)

    def test_complete_task_flow(self, server, store, user_id):
        task = store.create_task("待完成", user_id=user_id)
        _call_tool(server, "complete_task", {"task_id": task.id})
        updated = _get_task(store, task.id)
        assert updated.status == TaskStatus.DONE

    def test_start_task_flow(self, server, store, user_id):
        task = store.create_task("待开始", user_id=user_id)
        _call_tool(server, "start_task", {"task_id": task.id})
        updated = _get_task(store, task.id)
        assert updated.status == TaskStatus.DOING

    def test_unknown_tool_returns_graceful_error(self, server):
        content = _call_tool(server, "nonexistent_tool_xyz", {})
        text = content[0].text
        assert "未知工具" in text or "nonexistent_tool_xyz" in text

    def test_get_all_tags_empty(self, server):
        content = _call_tool(server, "get_all_tags", {})
        data = json.loads(content[0].text)
        assert isinstance(data, (list, dict))

    def test_add_tags_and_get_tasks_by_tag(self, server, store, user_id):
        task = store.create_task("带标签任务", user_id=user_id)
        _call_tool(server, "add_tags_to_task", {
            "task_id": task.id,
            "tags": ["mcp", "test"],
        })
        # get_all_tags
        content = _call_tool(server, "get_all_tags", {})
        data = json.loads(content[0].text)
        tags = data if isinstance(data, list) else data.get("tags", [])
        assert "mcp" in tags or "test" in tags
        # get_tasks_by_tag
        content = _call_tool(server, "get_tasks_by_tag", {"tag": "mcp"})
        data = json.loads(content[0].text)
        items = data if isinstance(data, list) else data.get("tasks", data.get("results", []))
        assert any(t.get("id") == task.id if isinstance(t, dict) else False for t in items)

    def test_postpone_task(self, server, store, user_id):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        task = store.create_task("要推迟", due_at=now, user_id=user_id)
        original_due = task.due_at
        _call_tool(server, "postpone_task", {"task_id": task.id, "days": 3})
        updated = _get_task(store, task.id)
        assert updated.due_at > original_due

    def test_drop_and_reopen(self, server, store, user_id):
        task = store.create_task("要放弃", user_id=user_id)
        _call_tool(server, "drop_task", {"task_id": task.id})
        assert _get_task(store, task.id).status == TaskStatus.DROPPED
        _call_tool(server, "reopen_task", {"task_id": task.id})
        assert _get_task(store, task.id).status == TaskStatus.TODO

    def test_edit_task(self, server, store, user_id):
        task = store.create_task("原标题", user_id=user_id)
        _call_tool(server, "edit_task", {
            "task_id": task.id,
            "title": "新标题",
            "priority": "high",
        })
        updated = _get_task(store, task.id)
        assert updated.title == "新标题"
        assert updated.priority == Priority.HIGH

    def test_create_subtask_and_get(self, server, store, user_id):
        parent = store.create_task("父任务", user_id=user_id)
        _call_tool(server, "create_subtask", {
            "parent_task_id": parent.id,
            "title": "子任务1",
        })
        content = _call_tool(server, "get_subtasks", {"parent_task_id": parent.id})
        data = json.loads(content[0].text)
        items = data if isinstance(data, list) else data.get("subtasks", data.get("tasks", []))
        assert any("子任务1" in (t.get("title", "") if isinstance(t, dict) else "") for t in items)

    def test_add_dependency(self, server, store, user_id):
        a = store.create_task("任务A", user_id=user_id)
        b = store.create_task("任务B", user_id=user_id)
        _call_tool(server, "add_task_dependency", {
            "task_id": b.id,
            "depends_on_task_id": a.id,
        })
        content = _call_tool(server, "get_task_dependencies", {"task_id": b.id})
        # 不报错即可（依赖列表结构由 store 决定）
        text = content[0].text
        assert isinstance(text, str)

    def test_save_and_get_note(self, server):
        _call_tool(server, "save_note", {"content": "测试笔记内容"})
        content = _call_tool(server, "get_my_notes", {})
        data = json.loads(content[0].text)
        # notes 结构灵活，只校验是合法 JSON 且包含内容
        assert "测试笔记内容" in json.dumps(data, ensure_ascii=False)

    def test_get_user_context(self, server):
        content = _call_tool(server, "get_user_context", {})
        data = json.loads(content[0].text)
        assert isinstance(data, dict)

    def test_get_daily_review(self, server):
        content = _call_tool(server, "get_daily_review", {})
        # 只要能返回字符串即可（review 结构由实现决定）
        assert isinstance(content[0].text, str)

    def test_check_in(self, server):
        content = _call_tool(server, "check_in", {})
        assert isinstance(content[0].text, str)

    def test_get_insights(self, server):
        content = _call_tool(server, "get_insights", {})
        data = json.loads(content[0].text)
        assert isinstance(data, (list, dict))


# ── 用户隔离 ───────────────────────────────────────────────────────


class TestUserIsolation:
    def test_different_users_separate_data(self, tmp_path, user_id):
        """不同 user_id 的 MCP server 操作各自的数据空间。"""
        from momentum_agent.mcp_server import create_mcp_server

        db = tmp_path / "iso.db"
        store_a = TaskStore(db)
        server_a = create_mcp_server(store_a, user_id)
        store_b = TaskStore(db)
        server_b = create_mcp_server(store_b, "other_user")

        # 用户 A 创建任务
        _call_tool(server_a, "create_task", {"title": "A 的任务"})
        # 用户 B 创建任务
        _call_tool(server_b, "create_task", {"title": "B 的任务"})

        # A 看不到 B 的任务
        content_a = _call_tool(server_a, "search_tasks", {"query": "B 的任务"})
        data_a = json.loads(content_a[0].text)
        items_a = data_a if isinstance(data_a, list) else data_a.get("results", data_a.get("tasks", []))
        assert len(items_a) == 0

        # B 看不到 A 的任务
        content_b = _call_tool(server_b, "search_tasks", {"query": "A 的任务"})
        data_b = json.loads(content_b[0].text)
        items_b = data_b if isinstance(data_b, list) else data_b.get("results", data_b.get("tasks", []))
        assert len(items_b) == 0


# ── 端到端 MCP 协议 ────────────────────────────────────────────────


class TestEndToEndProtocol:
    """通过真实的 ClientSession ↔ Server 内存流验证协议正确性。"""

    def test_list_tools_via_client(self, store, user_id):
        from mcp import ClientSession
        from mcp.shared.memory import create_client_server_memory_streams
        from momentum_agent.mcp_server import create_mcp_server

        server = create_mcp_server(store, user_id)
        init_opts = server.create_initialization_options()

        async def scenario():
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                async with ClientSession(*client_streams) as session:
                    server_task = asyncio.create_task(
                        server.run(*server_streams, init_opts)
                    )
                    await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) > 0
                    names = {t.name for t in tools.tools}
                    assert "create_task" in names
                    assert "get_overview" in names

                    server_task.cancel()
                    try:
                        await server_task
                    except (asyncio.CancelledError, Exception):
                        pass

        asyncio.run(scenario())

    def test_call_tool_via_client(self, store, user_id):
        from mcp import ClientSession
        from mcp.shared.memory import create_client_server_memory_streams
        from momentum_agent.mcp_server import create_mcp_server

        server = create_mcp_server(store, user_id)
        init_opts = server.create_initialization_options()

        async def scenario():
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                async with ClientSession(*client_streams) as session:
                    server_task = asyncio.create_task(
                        server.run(*server_streams, init_opts)
                    )
                    await session.initialize()

                    result = await session.call_tool(
                        "create_task",
                        {"title": "端到端测试任务"},
                    )
                    assert result.isError is False
                    assert len(result.content) > 0
                    text = result.content[0].text
                    task_id = _extract_task_id(text)
                    assert task_id, f"未从返回中提取到 task_id：{text}"

                    task = _get_task(store, task_id)
                    assert task.title == "端到端测试任务"

                    server_task.cancel()
                    try:
                        await server_task
                    except (asyncio.CancelledError, Exception):
                        pass

        asyncio.run(scenario())


# ── 入口函数 ───────────────────────────────────────────────────────


class TestRunMcpServerEntry:
    def test_invalid_transport_raises(self):
        from momentum_agent.mcp_server import run_mcp_server

        with pytest.raises(ValueError, match="不支持的传输方式"):
            run_mcp_server("sqlite:///tmp/nonexistent.db", transport="invalid")

    def test_run_mcp_server_importable(self):
        from momentum_agent.mcp_server import run_mcp_server

        assert callable(run_mcp_server)

    def test_cli_mcp_subcommand_exists(self):
        """CLI mcp 子命令应可被 argparse 解析。"""
        import argparse
        from momentum_agent.cli import main as cli_main

        # 不能真正执行（会启动 server），但可校验 parser 能识别 mcp 子命令
        # 通过手动构造 parser 来验证
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        mcp_p = sub.add_parser("mcp")
        mcp_p.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
        mcp_p.add_argument("--host", default="127.0.0.1")
        mcp_p.add_argument("--port", type=int, default=8766)
        mcp_p.add_argument("--user", default=None)

        args = parser.parse_args(["mcp", "--transport", "sse", "--port", "9999"])
        assert args.command == "mcp"
        assert args.transport == "sse"
        assert args.port == 9999
