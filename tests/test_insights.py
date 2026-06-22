"""Tests for the insights engine."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from momentum_agent.insights import InsightsEngine, BehavioralProfile, Insight
from momentum_agent.models import Priority, TaskStatus
from momentum_agent.storage import TaskStore


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "test.db")


@pytest.fixture
def engine(store):
    return InsightsEngine(store.db_path)


class TestBehavioralProfile:
    def test_empty_profile(self, engine):
        profile = engine.build_profile()
        assert profile.total_created == 0
        assert profile.completion_rate == 0.0
        assert profile.burnout_risk == "low"

    def test_profile_with_tasks(self, store, engine):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        store.create_task("任务1", priority=Priority.HIGH, due_at=now - timedelta(days=1))
        store.create_task("任务2", priority=Priority.MEDIUM)
        store.create_task("任务3", priority=Priority.LOW)
        store.update_status(1, TaskStatus.DONE)

        profile = engine.build_profile()
        assert profile.total_created == 3
        assert profile.total_completed == 1
        assert profile.completion_rate == pytest.approx(1 / 3, rel=0.1)

    def test_profile_to_dict(self, engine):
        profile = engine.build_profile()
        d = profile.to_dict()
        assert "completion_rate" in d
        assert "burnout_risk" in d
        assert "peak_completion_hour" in d
        assert "consistency_score" in d


class TestInsightsGeneration:
    def test_no_tasks_no_insights(self, engine):
        insights = engine.generate_insights([])
        risk_insights = [i for i in insights if i.category == "risk"]
        assert len(risk_insights) == 0

    def test_overdue_tasks_generate_risk(self, store, engine):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        for i in range(4):
            store.create_task(f"过期任务{i}", due_at=now - timedelta(days=1))

        tasks = store.list_tasks(status=None)
        insights = engine.generate_insights(tasks)

        risk_insights = [i for i in insights if i.category == "risk"]
        assert len(risk_insights) > 0

    def test_large_tasks_generate_suggestion(self, store, engine):
        store.create_task("大任务", estimated_minutes=120)
        tasks = store.list_tasks(status=None)
        insights = engine.generate_insights(tasks)

        suggestions = [i for i in insights if i.category == "suggestion"]
        assert len(suggestions) > 0
        assert "拆分" in suggestions[0].detail or "120" in suggestions[0].detail


class TestStrategicSummary:
    def test_empty_summary(self, engine):
        summary = engine.get_strategic_summary()
        assert "继续使用" in summary

    def test_summary_with_data(self, store, engine):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        store.create_task("任务1", due_at=now - timedelta(days=1))
        store.create_task("任务2")
        store.update_status(1, TaskStatus.DONE)

        summary = engine.get_strategic_summary()
        assert isinstance(summary, str)
        assert len(summary) > 0


class TestWeeklyPattern:
    def test_empty_pattern(self, engine):
        pattern = engine.get_weekly_pattern()
        assert isinstance(pattern, dict)

    def test_pattern_with_data(self, store, engine):
        store.create_task("任务1")
        store.update_status(1, TaskStatus.DONE)
        pattern = engine.get_weekly_pattern()
        assert isinstance(pattern, dict)


class TestTaskTypeAnalysis:
    def test_empty_analysis(self, engine):
        analysis = engine.get_task_type_analysis()
        assert "completed_tags" in analysis
        assert "dropped_tags" in analysis

    def test_analysis_with_tags(self, store, engine):
        store.create_task("任务1", tags=["work", "urgent"])
        store.create_task("任务2", tags=["personal"])
        store.update_status(1, TaskStatus.DONE)
        store.update_status(2, TaskStatus.DROPPED)

        analysis = engine.get_task_type_analysis()
        assert "work" in analysis["completed_tags"]
        assert "personal" in analysis["dropped_tags"]


class TestConsistencyScore:
    def test_empty_consistency(self, engine):
        score = engine.get_consistency_score()
        assert score == 0.0

    def test_consistency_with_data(self, store, engine):
        for i in range(5):
            store.create_task(f"任务{i}")
            store.update_status(i + 1, TaskStatus.DONE)

        score = engine.get_consistency_score()
        assert 0.0 <= score <= 1.0


class TestInsightDataclass:
    def test_insight_fields(self):
        insight = Insight(
            category="risk",
            icon="📉",
            title="测试洞察",
            detail="详细信息",
            priority=3,
        )
        assert insight.category == "risk"
        assert insight.actionable is True
