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
    # 学习/备考场景
    if any(marker in title for marker in ("学习", "备考", "复习", "考试", "课程", "读书", "阅读")):
        return [
            ("梳理核心知识点清单", 20),
            ("完成第一遍学习/阅读", 45),
            ("做笔记和重点标记", 25),
            ("练习关键题目或回顾", 30),
        ]
    
    # 会议/沟通场景
    if any(marker in title for marker in ("会议", "沟通", "讨论", "汇报", "交流")):
        return [
            ("准备会议/沟通大纲", 15),
            ("收集相关资料和数据", 20),
            ("参与会议/沟通并记录要点", 30),
            ("整理行动项和后续跟进", 15),
        ]
    
    # 面试/求职场景
    if any(marker in title for marker in ("面试", "应聘", "求职")):
        return [
            ("梳理岗位要求和个人匹配点", 25),
            ("准备 5 个高频问题回答", 35),
            ("做一次模拟复盘", 30),
        ]
    
    # 写作/文档场景
    if any(marker in title for marker in ("写", "方案", "文档", "材料", "报告")):
        return [
            ("列出大纲", 20),
            ("完成第一版草稿", 45),
            ("检查并压缩重点", 25),
        ]
    
    # 整理/清理场景
    if any(marker in title for marker in ("整理", "归档", "清理", "收拾")):
        return [
            ("收集需要处理的资料", 20),
            ("按重要性分类", 25),
            ("处理最紧急的一组", 30),
        ]
    
    # 开发/编程场景
    if any(marker in title for marker in ("开发", "编程", "代码", "功能", "需求", "bug", "修复")):
        return [
            ("分析需求/问题并确定方案", 25),
            ("编写核心代码/实现", 45),
            ("测试和验证功能", 20),
        ]
    
    # 运动/健身场景
    if any(marker in title for marker in ("运动", "健身", "跑步", "锻炼", "训练")):
        return [
            ("准备运动装备和场地", 10),
            ("完成主体运动/训练", 40),
            ("拉伸和放松恢复", 15),
        ]
    
    # 默认通用方案
    return [
        (f"明确「{title}」的具体目标", 15),
        (f"规划「{title}」的第一步行动", 20),
        (f"执行「{title}」的核心内容", 40),
        (f"检查「{title}」的完成情况", 15),
    ]


def child_priority(parent_priority: Priority) -> Priority:
    return Priority.HIGH if parent_priority == Priority.HIGH else Priority.MEDIUM
