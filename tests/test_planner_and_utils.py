"""planner.py 与 web/utils.py 里此前没有测试引用的函数。"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from momentum_agent.models import Priority
from momentum_agent.planner import child_priority, suggest_subtasks
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.utils import extract_task_id, extract_task_id_from_path, task_to_json


@pytest.fixture
def store():
    return SQLiteTaskStore(pathlib.Path(tempfile.mkdtemp()) / "planner.sqlite3")


def test_suggest_subtasks_is_deterministic_and_well_formed():
    first = suggest_subtasks("准备产品经理面试")
    second = suggest_subtasks("准备产品经理面试")
    assert first == second, "同样的输入必须给出同样的拆分"
    assert first, "至少要给出一条子任务"
    for title, minutes in first:
        assert isinstance(title, str) and title.strip()
        assert isinstance(minutes, int) and minutes > 0, (title, minutes)


def test_suggest_subtasks_handles_empty_title():
    assert isinstance(suggest_subtasks(""), list)


def test_child_priority_downgrades_non_high_parents():
    assert child_priority(Priority.HIGH) == Priority.HIGH
    assert child_priority(Priority.MEDIUM) == Priority.MEDIUM
    assert child_priority(Priority.LOW) == Priority.MEDIUM


def test_task_to_json_exposes_the_fields_the_frontend_needs(store):
    task = store.create_task("序列化任务", priority=Priority.HIGH, tags=["工作"], notes="备注")
    payload = task_to_json(task)
    assert payload["id"] == task.id
    assert payload["title"] == "序列化任务"
    assert payload["status"] == "todo"
    assert payload["priority"] == "high"
    assert payload["tags"] == ["工作"]
    assert payload["notes"] == "备注"
    assert payload["due_at"] is None, "没有截止日时必须是 None 而不是空字符串"
    assert isinstance(payload["created_at"], str) and payload["created_at"]


def test_task_to_json_serializes_due_at_when_present(store):
    from datetime import datetime, timezone

    due = datetime(2026, 6, 7, 9, 15, tzinfo=timezone.utc)
    task = store.create_task("有截止日", due_at=due)
    payload = task_to_json(task)
    assert payload["due_at"] == due.isoformat()


def test_extract_task_id_from_path():
    assert extract_task_id_from_path("/api/tasks/42") == 42
    assert extract_task_id_from_path("/api/tasks/42/done") == 42
    assert extract_task_id_from_path("/api/tasks/not-a-number") == -1
    assert extract_task_id_from_path("/api/tasks") == -1
    assert extract_task_id_from_path("/api/other/7") == -1


class FakeHandler:
    def __init__(self):
        self.status = None
        self.payload = None

    def send_json(self, payload, status=None):
        self.status = status
        self.payload = payload


def test_extract_task_id_reads_the_id_before_the_suffix():
    handler = FakeHandler()
    assert extract_task_id(handler, "/api/tasks/7/done", "done") == 7
    assert handler.payload is None, "成功时不应回错误"


def test_extract_task_id_rejects_a_malformed_path():
    handler = FakeHandler()
    assert extract_task_id(handler, "/api/tasks/abc/done", "done") is None
    assert handler.payload == {"error": "任务 ID 无效。"}
    assert handler.status is not None and int(handler.status) == 400

