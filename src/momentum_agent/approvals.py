"""破坏性操作的审批门禁（human-in-the-loop 的应用层实现）。

归档 review/Momentum_REVIEW_archive_20261007.md 的「未贸然接入的能力 / tool approval」写道：
「当前任务创建/编辑/完成/放弃等工具会直接执行。若产品要求高风险操作确认，应设计显式审批流
与可恢复状态」。这里落地最小可用版本：被保护的工具不再直接执行，而是写一条待确认记录并
告诉模型「等待用户确认」；用户通过 API / CLI / 界面批准后，才真正执行。

为什么做在应用层而不是用 SDK 的 needs_approval：SDK 的中断恢复需要贯穿流式协议的
RunState 管理与前端状态机，改动面大；应用层门禁可以立刻交付同样的安全价值，
且待确认记录本身是可恢复状态（写在 user_memory 里，重启不丢）。

配置：MOMENTUM_APPROVAL_REQUIRED_TOOLS 逗号分隔的工具名，默认 "drop_task"（最不可逆的操作）；
设为 "none" 关闭全部门禁。
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .storage import TaskStore

PENDING_MEMORY_KEY = "pending_approvals"
DEFAULT_GATED_TOOLS: tuple[str, ...] = ("drop_task",)


def gated_tools() -> tuple[str, ...]:
    """当前需要审批的工具名。默认只保护最不可逆的 drop_task。"""
    raw = os.environ.get("MOMENTUM_APPROVAL_REQUIRED_TOOLS")
    if raw is None:
        return DEFAULT_GATED_TOOLS
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    if items == ("none",):
        return ()
    return items


def is_gated(tool: str) -> bool:
    return tool in gated_tools()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(store: "TaskStore", user_id: str) -> list[dict]:
    raw = store.get_memory(PENDING_MEMORY_KEY, user_id=user_id)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("id")]


def _save(store: "TaskStore", user_id: str, items: list[dict]) -> None:
    store.set_memory(PENDING_MEMORY_KEY, json.dumps(items, ensure_ascii=False), user_id=user_id)


def list_pending(store: "TaskStore", user_id: str = "default") -> list[dict]:
    return _load(store, user_id)


def create_pending(
    store: "TaskStore",
    *,
    tool: str,
    arguments: dict[str, Any],
    user_id: str = "default",
    summary: str,
) -> dict:
    """登记一条待确认操作并返回它。同工具同参数若已存在待确认项则复用（避免刷屏）。"""
    items = _load(store, user_id)
    for item in items:
        if item.get("tool") == tool and item.get("arguments") == arguments:
            return item
    pending = {
        "id": secrets.token_hex(6),
        "tool": tool,
        "arguments": arguments,
        "summary": summary,
        "created_at": _now(),
    }
    items.append(pending)
    _save(store, user_id, items)
    return pending


def take_pending(store: "TaskStore", user_id: str, approval_id: str) -> dict | None:
    """取出并移除一条待确认操作（批准/取消都算消费掉，避免重复执行）。"""
    items = _load(store, user_id)
    found = None
    remaining = []
    for item in items:
        if found is None and str(item.get("id")) == str(approval_id):
            found = item
            continue
        remaining.append(item)
    if found is None:
        return None
    _save(store, user_id, remaining)
    return found


def _execute_drop_task(store: "TaskStore", arguments: dict, user_id: str) -> str:
    task_id = int(arguments.get("task_id"))
    task = store.drop_task(task_id, user_id=user_id)
    if not task:
        return f"任务 #{task_id} 不存在或不属于你"
    return f"已放弃 #{task.id}：{task.title}"


EXECUTORS: dict[str, Callable[["TaskStore", dict, str], str]] = {
    "drop_task": _execute_drop_task,
}


def execute(store: "TaskStore", approval: dict, user_id: str = "default") -> str:
    """执行一条已批准的待确认操作。"""
    tool = str(approval.get("tool") or "")
    executor = EXECUTORS.get(tool)
    if executor is None:
        return f"不支持的操作：{tool}"
    arguments = approval.get("arguments") or {}
    if not isinstance(arguments, dict):
        return f"操作参数损坏，未执行：{tool}"
    return executor(store, arguments, user_id)

