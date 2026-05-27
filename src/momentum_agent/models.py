from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    TODO = "todo"
    DOING = "doing"
    DONE = "done"
    DROPPED = "dropped"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


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
