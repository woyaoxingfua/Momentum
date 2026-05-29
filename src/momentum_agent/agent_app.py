"""
Momentum Task Agent - 简化的入口文件
使用模块化的 Agent 架构
"""
from __future__ import annotations
from pathlib import Path
from typing import AsyncIterator
import asyncio

from .storage import TaskStore
from .config import load_provider_config, ProviderConfig
from .logger import get_logger

log = get_logger("agent")

DEFAULT_USER_ID = "default"


def run_agent_message(db_path: Path, message: str, *, image_base64: str | None = None, user_id: str = DEFAULT_USER_ID) -> str:
    """运行 Agent 消息（非流式）"""
    return asyncio.run(_run_agent_message_async(db_path, message, image_base64=image_base64, user_id=user_id))


def run_agent_message_stream(db_path: Path, message: str, *, image_base64: str | None = None, user_id: str = DEFAULT_USER_ID) -> AsyncIterator[str]:
    """运行 Agent 消息（流式）"""
    return _run_agent_message_stream_async(db_path, message, image_base64=image_base64, user_id=user_id)


async def _run_agent_message_async(db_path: Path, message: str, *, image_base64: str | None = None, user_id: str = DEFAULT_USER_ID) -> str:
    """异步执行 Agent 消息"""
    from .agents.agent import build_agent
    from agents import Runner, RunConfig
    from agents.lifecycle import RunHooksBase
    
    log.info("agent_message user=%r msg=%r has_image=%s", user_id, message[:80], bool(image_base64))
    provider = load_provider_config()
    
    if not provider.is_configured:
        from .parser import parse_task_text
        from .planner import create_plan_from_text
        
        store = TaskStore(db_path)
        if should_review(message):
            from .context import local_review
            return local_review(store, user_id=user_id)
        if should_plan(message):
            return create_plan_from_text(store, message, user_id=user_id)
        return create_task_from_text(store, message, user_id=user_id)
    
    from .config import create_openai_client
    
    store = TaskStore(db_path)
    client = create_openai_client(provider)
    agent = build_agent(store, provider, client, user_id=user_id)
    
    class _H(RunHooksBase):
        async def on_tool_start(self, context, agent, tool):
            log.debug("[agent] tool ⚡ %s", tool.name)
        async def on_tool_end(self, context, agent, tool, result):
            log.debug("[agent] tool ✓ %s", tool.name)
    
    session = _get_session(db_path, user_id)
    
    if image_base64:
        agent_input = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": message if message else "请分析这张图片，提取其中的任务信息并创建相应的待办事项。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                    }
                ]
            }
        ]
        result = await Runner.run(
            agent, agent_input,
            max_turns=30,
            session=session,
            hooks=_H(),
        )
    else:
        result = await Runner.run(
            agent, message,
            max_turns=30,
            session=session,
            hooks=_H(),
        )
    
    reply = result.final_output
    log.info("agent done: user=%r len=%d", user_id, len(reply))
    return reply


async def _run_agent_message_stream_async(db_path: Path, message: str, *, image_base64: str | None = None, user_id: str = DEFAULT_USER_ID) -> AsyncIterator[str]:
    """异步执行 Agent 消息（流式）"""
    from .agents.agent import build_agent
    from agents import Runner, RunConfig
    from agents.lifecycle import RunHooksBase
    
    log.info("agent_stream user=%r msg=%r has_image=%s", user_id, message[:80], bool(image_base64))
    provider = load_provider_config()
    
    if not provider.is_configured:
        from .parser import parse_task_text
        from .planner import create_plan_from_text
        
        store = TaskStore(db_path)
        if image_base64:
            yield "图片识别功能需要配置 AI 模型。请在 .env 中设置 MOMENTUM_API_KEY。"
            return
        if should_review(message):
            yield local_review(store, user_id=user_id)
        elif should_plan(message):
            yield create_plan_from_text(store, message, user_id=user_id)
        else:
            yield create_task_from_text(store, message, user_id=user_id)
        return
    
    from .config import create_openai_client
    
    store = TaskStore(db_path)
    client = create_openai_client(provider)
    agent = build_agent(store, provider, client, user_id=user_id)
    
    class _H(RunHooksBase):
        async def on_tool_start(self, context, agent, tool):
            log.debug("[agent] tool ⚡ %s", tool.name)
        async def on_tool_end(self, context, agent, tool, result):
            log.debug("[agent] tool ✓ %s", tool.name)
    
    session = _get_session(db_path, user_id)
    
    if image_base64:
        agent_input = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": message if message else "请分析这张图片，提取其中的任务信息并创建相应的待办事项。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                    }
                ]
            }
        ]
        result = await Runner.run(
            agent, agent_input,
            max_turns=30,
            session=session,
            hooks=_H(),
        )
    else:
        result = await Runner.run(
            agent, message,
            max_turns=30,
            session=session,
            hooks=_H(),
        )
    
    reply = result.final_output
    log.info("agent done: user=%r len=%d", user_id, len(reply))
    
    for line in reply.split("\n"):
        if line.strip():
            yield line


def create_task_from_text(store: TaskStore, text: str, *, user_id: str = DEFAULT_USER_ID) -> str:
    """从文本创建任务"""
    from .parser import parse_task_text
    
    parsed = parse_task_text(text)
    task = store.create_task(
        parsed.title,
        due_at=parsed.due_at,
        priority=parsed.priority,
        estimated_minutes=parsed.estimated_minutes,
        notes=parsed.notes,
        user_id=user_id,
    )
    due_info = f"，截止 {task.due_at.strftime('%Y-%m-%d')}" if task.due_at else ""
    return f"已创建任务 #{task.id}：{task.title}{due_info}"


def should_review(text: str) -> bool:
    """检查是否应该进行回顾"""
    review_keywords = ["回顾", "review", "总结", "看看我", "怎么样", "进度", "情况"]
    return any(kw in text.lower() for kw in review_keywords)


def should_plan(text: str) -> bool:
    """检查是否应该创建计划"""
    plan_keywords = ["计划", "plan", "拆解", "分解", "步骤"]
    return any(kw in text.lower() for kw in plan_keywords)


_sessions = {}


def _get_session(db_path: Path, user_id: str):
    """获取或创建会话"""
    from agents import SQLiteSession, SessionSettings
    
    key = str(db_path.resolve())
    if key not in _sessions:
        _sessions[key] = {}
    
    sessions_for_db = _sessions[key]
    if user_id not in sessions_for_db:
        session_path = db_path.parent / f".momentum_sessions_{user_id}.db"
        sessions_for_db[user_id] = SQLiteSession(
            session_id=user_id,
            db_path=str(session_path),
            session_settings=SessionSettings(limit=30),
        )
    
    return sessions_for_db[user_id]
