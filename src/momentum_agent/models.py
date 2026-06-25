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
    DEPENDS_ON = "depends_on"
    BLOCKS = "blocks"
    RELATES_TO = "relates_to"
    PARENT_OF = "parent_of"
    FOLLOWS = "follows"


@dataclass(frozen=True)
class TaskRelation:
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
    title: str = Field(description="Clean task title without date/time/priority keywords")
    due_at: str | None = Field(default=None, description="ISO 8601 datetime string, or null when no deadline mentioned")
    priority: str = Field(default="medium", description="low, medium, or high")
    estimated_minutes: int | None = Field(default=None, description="Estimated minutes to complete, or null")
    notes: str | None = Field(default=None, description="Extra notes or context from the user's message")


class SubtaskItem(BaseModel):
    title: str = Field(description="Specific, actionable subtask title in Chinese")
    estimated_minutes: int = Field(description="Estimated minutes, between 10 and 60")


class PlanOutput(BaseModel):
    title: str = Field(description="Clean parent task title")
    subtasks: list[SubtaskItem] = Field(description="3-5 specific, actionable subtasks")
