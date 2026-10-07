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
    return InsightsEngine(store)


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


class TestEvidenceBasedTimeInsights:
    def test_uses_done_event_and_actual_focus_not_later_task_update(self, store, engine):
        task = store.create_task("有计时记录的任务", estimated_minutes=20)
        store.update_status(task.id, TaskStatus.DONE)

        now = datetime.now(timezone.utc)
        created_at = now - timedelta(hours=7)
        completed_at = now - timedelta(hours=2)
        later_update_at = now - timedelta(hours=1)
        with store._connect() as conn:
            conn.execute(
                "UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?",
                (created_at.isoformat(), later_update_at.isoformat(), task.id),
            )
            conn.execute(
                "UPDATE task_events SET created_at = ? WHERE task_id = ? AND event_type = 'status_changed' AND payload = 'done'",
                (completed_at.isoformat(), task.id),
            )

        store.record_focus_session(
            task.id,
            30,
            actual_seconds=30 * 60,
            planned_minutes=30,
            started_at=completed_at - timedelta(minutes=50),
            ended_at=completed_at,
            outcome="completed",
            session_id="d" * 32,
        )

        profile = engine.build_profile()
        assert profile.avg_completion_hours == pytest.approx(5.0)
        assert profile.peak_completion_hour == completed_at.hour
        assert profile.avg_actual_focus_minutes == pytest.approx(30.0)
        assert profile.focus_tracked_tasks == 1
        assert profile.estimated_focus_tasks == 1
        assert profile.underestimation_ratio == pytest.approx(1.5)
        assert profile.estimation_accuracy == pytest.approx(0.5)
        assert engine.get_completion_events()[0]["completed_at"] == completed_at

    def test_legacy_planned_minutes_do_not_claim_actual_time_or_accuracy(self, store, engine):
        task = store.create_task("旧专注记录", estimated_minutes=25)
        store.update_status(task.id, TaskStatus.DONE)
        store.record_focus_session(task.id, 25)

        profile = engine.build_profile()
        session = store.get_focus_sessions()[0]

        assert session["duration_minutes"] == 25
        assert session["actual_seconds"] is None
        assert profile.avg_actual_focus_minutes == 0
        assert profile.focus_tracked_tasks == 0
        assert profile.estimated_focus_tasks == 0
        assert profile.estimation_accuracy == 0
        assert profile.underestimation_ratio == 0

    def test_focus_window_uses_event_created_at_and_only_latest_100_done_tasks(
        self, store, engine
    ):
        tasks = []
        for index in range(101):
            estimate = 0 if index == 3 else 2
            task = store.create_task(f"完成任务{index}", estimated_minutes=estimate)
            store.update_status(task.id, TaskStatus.DONE)
            tasks.append(task)

        now = datetime.now(timezone.utc)
        with store._connect() as conn:
            for index, task in enumerate(tasks):
                completed_at = now - timedelta(seconds=101 - index)
                conn.execute(
                    "UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?",
                    (
                        (completed_at - timedelta(hours=1)).isoformat(),
                        completed_at.isoformat(),
                        task.id,
                    ),
                )
                conn.execute(
                    "UPDATE task_events SET created_at = ? "
                    "WHERE task_id = ? AND event_type = 'status_changed' AND payload = 'done'",
                    (completed_at.isoformat(), task.id),
                )

        for index, task in enumerate(tasks[:-1]):
            started_at = now - timedelta(days=40 if index == 1 else 1)
            store.record_focus_session(
                task.id,
                1,
                actual_seconds=60,
                planned_minutes=1,
                started_at=started_at,
                ended_at=started_at + timedelta(minutes=1),
                outcome="completed",
                session_id=f"{index + 1:032x}",
            )

        with store._connect() as conn:
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND event_type = 'focus_session'",
                ((now - timedelta(days=1)).isoformat(), tasks[1].id),
            )
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND event_type = 'focus_session'",
                ((now - timedelta(days=31)).isoformat(), tasks[2].id),
            )

        profile = engine.build_profile()

        # The latest 100 excludes task 0 (even though it has focus); task 100
        # is included but intentionally has no focus event.
        # Task 1's event is in-window despite old session timestamps; task 2's
        # event is out-of-window despite in-window session timestamps.
        assert profile.focus_tracked_tasks == 98
        assert profile.avg_actual_focus_minutes == pytest.approx(1.0)
        assert profile.estimated_focus_tasks == 97
        assert profile.underestimation_ratio == pytest.approx(0.5)
        assert profile.estimation_accuracy == pytest.approx(0.5)

    def test_accuracy_recalculates_from_current_estimate_after_done_task_update(
        self, store, engine
    ):
        task = store.create_task("事后修改预估的已完成任务", estimated_minutes=20)
        store.update_status(task.id, TaskStatus.DONE)

        now = datetime.now(timezone.utc)
        store.record_focus_session(
            task.id,
            30,
            actual_seconds=30 * 60,
            planned_minutes=30,
            started_at=now - timedelta(minutes=50),
            ended_at=now,
            outcome="completed",
            session_id="e" * 32,
        )

        before = engine.build_profile()
        assert before.avg_actual_focus_minutes == pytest.approx(30.0)
        assert before.focus_tracked_tasks == 1
        assert before.estimated_focus_tasks == 1
        assert before.underestimation_ratio == pytest.approx(1.5)
        assert before.estimation_accuracy == pytest.approx(0.5)

        store.update_task(task.id, estimated_minutes=30)

        after = engine.build_profile()
        assert after.avg_actual_focus_minutes == pytest.approx(30.0)
        assert after.focus_tracked_tasks == 1
        assert after.estimated_focus_tasks == 1
        assert after.underestimation_ratio == pytest.approx(1.0)
        assert after.estimation_accuracy == pytest.approx(1.0)
