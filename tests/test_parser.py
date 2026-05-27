from datetime import datetime
from zoneinfo import ZoneInfo

from momentum_agent.models import Priority
from momentum_agent.parser import parse_task_text


def test_parse_tomorrow_task() -> None:
    now = datetime(2026, 5, 25, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    parsed = parse_task_text("明天整理产品经理面试材料", now=now)

    assert parsed.title == "整理产品经理面试材料"
    assert parsed.due_at is not None
    assert parsed.due_at.day == 26
    assert parsed.due_at.hour == 18
    assert parsed.estimated_minutes == 45


def test_parse_priority() -> None:
    parsed = parse_task_text("尽快交水费")

    assert parsed.priority == Priority.HIGH


def test_parse_iso_date() -> None:
    now = datetime(2026, 5, 25, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    parsed = parse_task_text("2026-06-01 准备周会材料", now=now)

    assert parsed.due_at is not None
    assert parsed.due_at.month == 6
    assert parsed.due_at.day == 1


def test_cleanup_conversational_prefix() -> None:
    now = datetime(2026, 5, 25, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    parsed = parse_task_text("帮我安排下周准备产品经理面试", now=now)

    assert parsed.title == "准备产品经理面试"


def test_parse_afternoon_time() -> None:
    now = datetime(2026, 5, 25, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    parsed = parse_task_text("明天下午3点交水费", now=now)

    assert parsed.due_at is not None
    assert parsed.due_at.day == 26
    assert parsed.due_at.hour == 15
    assert parsed.title == "交水费"
