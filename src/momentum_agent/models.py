from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

__all__ = [
    "TaskStatus",
    "Priority",
    "TaskRelationType",
    "TaskRelation",
    "Task",
    "UserContext",
    "ParsedTaskOutput",
    "SubtaskItem",
    "PlanOutput",
    "TaskRelationInput",
    "BulkSubtaskInput",
    "TaskWithRelationsOutput",
]


class TaskStatus(StrEnum):
    TODO = "todo"
    DOING = "doing"
    DONE = "done"
    DROPPED = "dropped"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskRelationType(StrEnum):
    """任务关联关系类型"""
    DEPENDS_ON = "depends_on"   # 任务A 依赖 任务B 完成
    BLOCKS = "blocks"           # 任务A 阻塞 任务B
    RELATES_TO = "relates_to"  # 任务A 和 任务B 相关
    PARENT_OF = "parent_of"    # 任务A 是 任务B 的父任务（子任务关系）
    FOLLOWS = "follows"        # 任务A 在 任务B 之后执行


@dataclass(frozen=True)
class TaskRelation:
    """任务之间的关联关系"""
    id: int
    source_task_id: int
    target_task_id: int
    relation_type: TaskRelationType
    created_at: datetime


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    status: TaskStatus
    priority: Priority
    due_at: datetime | None
    estimated_minutes: int | None
    notes: str | None
    parent_task_id: int | None
    recurrence: str | None
    user_id: str | None
    created_at: datetime
    updated_at: datetime
    # v1 新增：标签和动态关联字段（带默认值，向后兼容）
    tags: list[str] | None = None
    subtasks: list[Task] | None = None
    relations: list[TaskRelation] | None = None


@dataclass(frozen=True)
class UserContext:
    now: datetime
    energy: str
    available_minutes_today: int
    recent_pattern: str


class ParsedTaskOutput(BaseModel):
    """Structured output for AI-based natural-language task parsing."""

    title: str = Field(description="Clean task title without date/time/priority keywords")
    due_at: str | None = Field(default=None, description="ISO 8601 datetime string, or null when no deadline mentioned")
    priority: str = Field(default="medium", description="low, medium, or high")
    estimated_minutes: int | None = Field(default=None, description="Estimated minutes to complete, or null")
    notes: str | None = Field(default=None, description="Extra notes or context from the user's message")


class SubtaskItem(BaseModel):
    """A single subtask in an AI-generated plan."""

    title: str = Field(description="Specific, actionable subtask title in Chinese")
    estimated_minutes: int = Field(description="Estimated minutes, between 10 and 60")


class PlanOutput(BaseModel):
    """Structured output for AI-based task planning."""

    title: str = Field(description="Clean parent task title")
    subtasks: list[SubtaskItem] = Field(description="3-5 specific, actionable subtasks")


class TaskRelationInput(BaseModel):
    """用于创建任务关联的输入模型"""
    target_task_id: int = Field(description="目标任务 ID")
    relation_type: str = Field(description="关系类型: depends_on, blocks, relates_to, parent_of, follows")


class BulkSubtaskInput(BaseModel):
    """批量创建子任务的输入模型"""
    subtasks: list[SubtaskItem] = Field(description="子任务列表")


class TaskWithRelationsOutput(BaseModel):
    """任务详情输出，包含子任务和关联关系"""
    task: dict
    subtasks: list[dict]
    dependencies: list[dict]
    dependents: list[dict]
    related: list[dict]
