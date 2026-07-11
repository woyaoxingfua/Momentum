"""MCP Server — 把 Momentum 的任务管理能力暴露为 MCP 工具，供外部 AI Agent 调用。

复用 agents/tools 下的全部 function_tool 定义，零重复代码。

两种传输方式：
  - stdio（默认）：本地进程，供 Claude Desktop / Cursor / 命令行 Agent 调用
  - sse：HTTP + SSE，供远程 / 网络 Agent 调用（可选 API Key 鉴权）

用法：
  momentum-agent mcp                          # stdio 模式
  momentum-agent mcp --transport sse         # SSE 模式，默认 127.0.0.1:8766
  momentum-agent mcp --transport sse --host 0.0.0.0 --port 9000
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from .config import DEFAULT_USER_ID
from .logger import get_logger
from .storage import create_task_store

log = get_logger("mcp")

# ═══════════════════════════════════════════════════════════════════
# 工具注册表 — 复用 agents/tools 下的全部 FunctionTool
# ═══════════════════════════════════════════════════════════════════


def build_all_tools(store, user_id: str) -> list:
    """构建全部工具列表（复用现有 function_tool 工厂）。"""
    from .agents.tools import (
        create_task_tools,
        create_subtask_tools,
        create_relation_tools,
        create_weather_tools,
        create_heartbeat_tools,
        create_insight_tools,
        create_focus_tools,
        create_extra_tools,
    )

    return (
        create_task_tools(store, user_id)
        + create_subtask_tools(store, user_id)
        + create_relation_tools(store, user_id)
        + create_heartbeat_tools(store, user_id)
        + create_insight_tools(store, user_id)
        + create_focus_tools(store, user_id)
        + create_weather_tools(store, user_id)
        + create_extra_tools(store, user_id)
    )


def _is_nullable(prop: dict) -> bool:
    """属性是否为 Optional（含 null 的 anyOf，或有 default）。"""
    if "default" in prop:
        return True
    if prop.get("type") == "null":
        return True
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        return any(isinstance(t, dict) and t.get("type") == "null" for t in any_of)
    return False


def _loosen_schema(schema: dict) -> dict:
    """把 agents SDK 的 strict schema 转成 MCP 友好的 schema。

    strict 模式把所有参数（含 Optional 和带默认值的）都标记为 required，
    并用 ``anyOf: [{type: X}, {type: null}]`` 表示可选类型。
    MCP 场景下可选参数不应出现在 required 中，否则 LLM 必须显式传 null。
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    properties = out.get("properties", {})
    required = out.get("required", [])
    if required and isinstance(properties, dict):
        loosened = [name for name in required if not _is_nullable(properties.get(name, {}))]
        if loosened:
            out["required"] = loosened
        else:
            out.pop("required", None)
    return out


def _to_mcp_tool(ft) -> "Any":
    """把 FunctionTool 转成 MCP Tool 定义。"""
    import mcp.types as types

    return types.Tool(
        name=ft.name,
        description=ft.description or ft.name,
        inputSchema=_loosen_schema(ft.params_json_schema or {"type": "object", "properties": {}}),
    )


async def _invoke_function_tool(ft, arguments: dict) -> str:
    """用最小上下文调用现有 FunctionTool，隔离 agents SDK 细节。

    如果 SDK 版本升级导致 ToolContext 构造方式变化，只需修改此函数。
    """
    from agents import RunContextWrapper
    from agents.tool import ToolContext, invoke_function_tool

    args_str = json.dumps(arguments, ensure_ascii=False) if arguments else "{}"
    wrapper = RunContextWrapper(context=None)
    ctx = ToolContext(
        context=wrapper,
        tool_name=ft.name,
        tool_call_id="mcp",
        tool_arguments=args_str,
    )
    result = await invoke_function_tool(function_tool=ft, context=ctx, arguments=args_str)
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════════════════
# MCP Server
# ═══════════════════════════════════════════════════════════════════

DEFAULT_INSTRUCTIONS = (
    "Momentum 任务管理 MCP 服务器。"
    "提供任务的创建、查询、编辑、状态流转、子任务、依赖关系、行为洞察等能力。"
    "所有工具均作用于当前配置的用户数据空间。"
)


def create_mcp_server(store, user_id: str, *, instructions: str | None = None):
    """创建配置好全部工具的 MCP Server 实例。"""
    import mcp.types as types
    from mcp.server import Server

    server = Server("momentum", instructions=instructions or DEFAULT_INSTRUCTIONS)
    function_tools = build_all_tools(store, user_id)
    tool_map: dict[str, Any] = {ft.name: ft for ft in function_tools}
    mcp_tools = [_to_mcp_tool(ft) for ft in function_tools]

    log.info("MCP server created: %d tools for user=%r", len(mcp_tools), user_id)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return mcp_tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None):
        ft = tool_map.get(name)
        if ft is None:
            return [types.TextContent(type="text", text=f"未知工具：{name}")]
        try:
            result = await _invoke_function_tool(ft, arguments or {})
        except Exception as exc:
            log.warning("tool %s failed: %s", name, exc)
            return [types.TextContent(type="text", text=f"工具 {name} 执行出错：{exc}")]

        # 如果结果是 JSON 字符串，保留原文返回（LLM 可直接解析）
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        return [types.TextContent(type="text", text=text)]

    return server


# ═══════════════════════════════════════════════════════════════════
# 传输层
# ═══════════════════════════════════════════════════════════════════


async def run_stdio(database_url: str, user_id: str = DEFAULT_USER_ID) -> None:
    """stdio 传输 — 本地 Agent（Claude Desktop / Cursor）标准接入方式。"""
    from mcp.server.stdio import stdio_server

    store = create_task_store(database_url)
    server = create_mcp_server(store, user_id)
    init_opts = server.create_initialization_options()
    log.info("starting MCP stdio server: user=%r db=%s", user_id, database_url)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_opts)


def _check_api_key(api_key: str | None) -> bool:
    """读取环境变量中的 API Key，与传入的对比。"""
    expected = os.environ.get("MOMENTUM_MCP_API_KEY")
    if not expected:
        return True  # 未配置则不鉴权
    return bool(api_key) and api_key == expected


async def run_sse(
    database_url: str,
    user_id: str = DEFAULT_USER_ID,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> None:
    """SSE 传输 — 远程 HTTP 接入，可选 API Key 鉴权（MOMENTUM_MCP_API_KEY）。"""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route
    import uvicorn

    store = create_task_store(database_url)
    server = create_mcp_server(store, user_id)
    init_opts = server.create_initialization_options()
    sse_transport = SseServerTransport("/messages/")

    require_auth = bool(os.environ.get("MOMENTUM_MCP_API_KEY"))

    async def handle_sse(request: Request) -> Response:
        if require_auth:
            token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not _check_api_key(token):
                return JSONResponse({"error": "无效或缺失的 API Key"}, status_code=401)
        async with sse_transport.connect_sse(request.scope, request.receive, request._send) as (read, write):
            await server.run(read, write, init_opts)
        return Response()

    app = Starlette(
        debug=False,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
    )

    log.info("starting MCP SSE server: http://%s:%s/sse user=%r auth=%s", host, port, user_id, require_auth)
    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    http_server = uvicorn.Server(config)
    await http_server.serve()


# ═══════════════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════════════


def run_mcp_server(
    database_url: str,
    *,
    transport: str = "stdio",
    user_id: str = DEFAULT_USER_ID,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> None:
    """启动 MCP 服务器。

    Args:
        database_url: 数据库 URL
        transport: 传输方式 "stdio" 或 "sse"
        user_id: 操作的目标用户
        host: SSE 模式监听地址
        port: SSE 模式监听端口
    """
    if transport == "stdio":
        asyncio.run(run_stdio(database_url, user_id))
    elif transport == "sse":
        asyncio.run(run_sse(database_url, user_id, host=host, port=port))
    else:
        raise ValueError(f"不支持的传输方式：{transport}（可选：stdio, sse）")


__all__ = [
    "build_all_tools",
    "create_mcp_server",
    "run_stdio",
    "run_sse",
    "run_mcp_server",
]
