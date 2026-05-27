from __future__ import annotations

from .config import DEFAULT_USER_ID
from .logger import get_logger
from .models import Priority, Task
from .parser import parse_task_text
from .storage import TaskStore

log = get_logger("planner")


def create_task_plan(
    store: TaskStore, text: str, *, user_id: str = DEFAULT_USER_ID
) -> tuple[Task, list[Task]]:
    parsed = parse_task_text(text)
    parent = store.create_task(
        parsed.title,
        due_at=parsed.due_at,
        priority=parsed.priority,
        estimated_minutes=parsed.estimated_minutes or 90,
        notes=parsed.notes,
        user_id=user_id,
    )

    subtasks = suggest_subtasks(parsed.title)
    children = [
        store.create_task(
            title,
            due_at=parsed.due_at,
            priority=child_priority(parsed.priority),
            estimated_minutes=minutes,
            parent_task_id=parent.id,
            user_id=user_id,
        )
        for title, minutes in subtasks
    ]
    log.info("template plan: parent=#%d children=%d", parent.id, len(children))
    return parent, children


def suggest_subtasks(title: str) -> list[tuple[str, int]]:
    if any(marker in title for marker in ("面试", "应聘", "求职")):
        return [
            ("梳理岗位要求和个人匹配点", 25),
            ("准备 5 个高频问题回答", 35),
            ("做一次模拟复盘", 30),
        ]

    if any(marker in title for marker in ("写", "方案", "文档", "材料")):
        return [
            ("列出大纲", 20),
            ("完成第一版草稿", 45),
            ("检查并压缩重点", 25),
        ]

    if any(marker in title for marker in ("整理", "归档", "清理")):
        return [
            ("收集需要处理的资料", 20),
            ("按重要性分类", 25),
            ("处理最紧急的一组", 30),
        ]

    return [
        (f"明确「{title}」完成标准", 15),
        (f"推进「{title}」的最小下一步", 25),
        (f"复查「{title}」并决定后续动作", 20),
    ]


def child_priority(parent_priority: Priority) -> Priority:
    return Priority.HIGH if parent_priority == Priority.HIGH else Priority.MEDIUM
