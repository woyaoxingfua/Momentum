"""行为学习引擎 — 从 task_events 中挖掘用户行为模式，提供数据驱动的洞察。

核心理念：
  Notion 等工具是"被动记录"，Momentum 应该是"主动学习"。
  通过分析任务创建、完成、推迟、放弃的模式，提供真正的个性化建议。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .logger import get_logger
from .models import Task, TaskStatus

if TYPE_CHECKING:
    from .storage import MySQLTaskStore, SQLiteTaskStore

log = get_logger("insights")


@dataclass
class BehavioralProfile:
    """用户行为画像 — 从历史数据中提炼的模式。"""

    # 完成率
    completion_rate: float = 0.0
    total_created: int = 0
    total_completed: int = 0
    total_dropped: int = 0

    # 时间模式
    avg_completion_hours: float = 0.0  # 平均完成耗时（小时）
    estimation_accuracy: float = 0.0  # 预估准确率（0-1，1=完美）
    underestimation_ratio: float = 0.0  # 低估比例（>1 表示经常低估）

    # 偏好模式
    peak_completion_hour: int | None = None  # 最常完成任务的时段
    preferred_task_duration: int | None = None  # 用户实际偏好任务时长
    procrastination_types: list[str] = field(default_factory=list)  # 容易拖延的任务类型

    # 周期模式
    productive_days: list[str] = field(default_factory=list)  # 产出最高的日子
    avg_tasks_per_day: float = 0.0
    consistency_score: float = 0.0  # 一致性得分（0-1）

    # 风险信号
    overdue_trend: str = "stable"  # stable / increasing / decreasing
    burnout_risk: str = "low"  # low / medium / high

    def to_dict(self) -> dict:
        return {
            "completion_rate": self.completion_rate,
            "total_created": self.total_created,
            "total_completed": self.total_completed,
            "total_dropped": self.total_dropped,
            "avg_completion_hours": self.avg_completion_hours,
            "estimation_accuracy": self.estimation_accuracy,
            "underestimation_ratio": self.underestimation_ratio,
            "peak_completion_hour": self.peak_completion_hour,
            "preferred_task_duration": self.preferred_task_duration,
            "procrastination_types": self.procrastination_types,
            "productive_days": self.productive_days,
            "avg_tasks_per_day": self.avg_tasks_per_day,
            "consistency_score": self.consistency_score,
            "overdue_trend": self.overdue_trend,
            "burnout_risk": self.burnout_risk,
        }


@dataclass
class Insight:
    """一条洞察 — 可以直接呈现给用户的发现。"""

    category: str  # pattern / risk / suggestion / achievement
    icon: str
    title: str
    detail: str
    actionable: bool = True
    priority: int = 0  # 越高越重要


class InsightsEngine:
    """行为分析引擎 — 从任务存储后端中提取洞察。"""

    def __init__(self, store: "SQLiteTaskStore | MySQLTaskStore") -> None:
        self.store = store
        # 延迟导入以避免循环依赖
        from .storage import MySQLTaskStore, SQLiteTaskStore
        self._is_sqlite = isinstance(store, SQLiteTaskStore)
        self._is_mysql = isinstance(store, MySQLTaskStore)
        if not self._is_sqlite and not self._is_mysql:
            raise TypeError(f"Unsupported store type: {type(store)}")

    def _connect(self) -> Any:
        if self._is_sqlite:
            import sqlite3
            conn = sqlite3.connect(self.store.db_path)
            conn.row_factory = sqlite3.Row
            return conn
        import pymysql
        return pymysql.connect(
            host=self.store.host,
            port=self.store.port,
            user=self.store.user,
            password=self.store.password,
            database=self.store.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _date_expr(self, col: str) -> str:
        return f"DATE({col})" if self._is_sqlite else f"DATE({col})"

    def _dow_expr(self, col: str) -> str:
        # SQLite strftime('%w'): 0=周日...6=周六
        # MySQL DAYOFWEEK: 1=周日...7=周六，所以减 1 对齐
        return f"strftime('%w', {col})" if self._is_sqlite else f"DAYOFWEEK({col}) - 1"

    def _now_minus_days(self, days: int) -> str:
        return f"datetime('now', '-{days} days')" if self._is_sqlite else f"DATE_SUB(NOW(), INTERVAL {days} DAY)"

    def _ph(self) -> str:
        """返回当前数据库对应的参数占位符（SQLite 用 ?，MySQL 用 %s）。"""
        return "%s" if self._is_mysql else "?"

    def build_profile(self, user_id: str = "default") -> BehavioralProfile:
        """构建用户行为画像。"""
        profile = BehavioralProfile()
        now = datetime.now(timezone.utc)

        with self._connect() as conn:
            cur = conn.cursor()
            # ── 基础统计 ──────────────────────────────────────
            cur.execute(
                f"SELECT status, COUNT(*) as cnt FROM tasks WHERE user_id = {self._ph()} GROUP BY status",
                (user_id,),
            )
            rows = cur.fetchall()
            status_counts = {r["status"]: r["cnt"] for r in rows}
            profile.total_created = sum(status_counts.values())
            profile.total_completed = status_counts.get("done", 0)
            profile.total_dropped = status_counts.get("dropped", 0)

            if profile.total_created > 0:
                profile.completion_rate = profile.total_completed / profile.total_created

            # ── 完成时间分析 ──────────────────────────────────
            cur.execute(
                f"""
                SELECT t.id, t.estimated_minutes, t.created_at, t.updated_at
                FROM tasks t
                WHERE t.user_id = {self._ph()} AND t.status = 'done'
                ORDER BY t.updated_at DESC
                LIMIT 100
                """,
                (user_id,),
            )
            completed_events = cur.fetchall()

            if completed_events:
                completion_times = []
                estimation_errors = []
                for ev in completed_events:
                    try:
                        created = datetime.fromisoformat(ev["created_at"])
                        updated = datetime.fromisoformat(ev["updated_at"])
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        if updated.tzinfo is None:
                            updated = updated.replace(tzinfo=timezone.utc)
                        hours = (updated - created).total_seconds() / 3600
                        completion_times.append(hours)

                        if ev["estimated_minutes"]:
                            estimated_hours = ev["estimated_minutes"] / 60
                            if estimated_hours > 0:
                                error = hours / estimated_hours
                                estimation_errors.append(error)
                    except (ValueError, TypeError):
                        continue

                if completion_times:
                    profile.avg_completion_hours = sum(completion_times) / len(completion_times)

                if estimation_errors:
                    profile.estimation_accuracy = 1.0 - min(
                        abs(1.0 - sum(estimation_errors) / len(estimation_errors)), 1.0
                    )
                    profile.underestimation_ratio = sum(estimation_errors) / len(estimation_errors)

            # ── 完成时段分析 ──────────────────────────────────
            if completed_events:
                hour_counter = Counter()
                for ev in completed_events:
                    try:
                        updated = datetime.fromisoformat(ev["updated_at"])
                        hour_counter[updated.hour] += 1
                    except (ValueError, TypeError):
                        continue
                if hour_counter:
                    profile.peak_completion_hour = hour_counter.most_common(1)[0][0]

            # ── 偏好任务时长 ──────────────────────────────────
            if completed_events:
                durations = []
                for ev in completed_events:
                    if ev["estimated_minutes"]:
                        durations.append(ev["estimated_minutes"])
                if durations:
                    buckets = Counter()
                    for d in durations:
                        if d <= 15:
                            buckets["quick"] += 1
                        elif d <= 30:
                            buckets["short"] += 1
                        elif d <= 60:
                            buckets["medium"] += 1
                        else:
                            buckets["long"] += 1
                    most_common = buckets.most_common(1)[0][0]
                    profile.preferred_task_duration = {
                        "quick": 15, "short": 30, "medium": 60, "long": 90
                    }[most_common]

            # ── 拖延类型分析 ──────────────────────────────────
            cur.execute(
                f"""
                SELECT t.title, COUNT(*) as cnt
                FROM task_events e
                JOIN tasks t ON e.task_id = t.id
                WHERE e.event_type = 'status_changed'
                  AND e.payload = 'dropped'
                  AND t.user_id = {self._ph()}
                GROUP BY t.title
                ORDER BY cnt DESC
                LIMIT 5
                """,
                (user_id,),
            )
            postponed_tasks = cur.fetchall()
            profile.procrastination_types = [r["title"] for r in postponed_tasks]

            # ── 每日产出模式 ──────────────────────────────────
            date_col = self._date_expr("updated_at")
            cur.execute(
                f"""
                SELECT {date_col} as day, COUNT(*) as cnt
                FROM tasks
                WHERE user_id = {self._ph()} AND status = 'done'
                GROUP BY day
                ORDER BY cnt DESC
                LIMIT 7
                """,
                (user_id,),
            )
            daily_counts = cur.fetchall()
            if daily_counts:
                profile.avg_tasks_per_day = sum(r["cnt"] for r in daily_counts) / len(daily_counts)
                day_names = []
                for r in daily_counts:
                    try:
                        d = datetime.fromisoformat(r["day"])
                        day_names.append(d.strftime("%A"))
                    except (ValueError, TypeError):
                        continue
                profile.productive_days = list(dict.fromkeys(day_names))[:3]

            # ── 过期趋势 ──────────────────────────────────────
            cur.execute(
                f"""
                SELECT COUNT(*) as cnt FROM tasks
                WHERE user_id = {self._ph()} AND status IN ('todo', 'doing')
                  AND due_at < {self._ph()} AND due_at > {self._ph()}
                """,
                (user_id, now.isoformat(), (now - timedelta(days=7)).isoformat()),
            )
            overdue_week1 = cur.fetchone()["cnt"]

            cur.execute(
                f"""
                SELECT COUNT(*) as cnt FROM tasks
                WHERE user_id = {self._ph()} AND status IN ('todo', 'doing')
                  AND due_at < {self._ph()} AND due_at > {self._ph()}
                """,
                (user_id, (now - timedelta(days=7)).isoformat(), (now - timedelta(days=14)).isoformat()),
            )
            overdue_week2 = cur.fetchone()["cnt"]

            if overdue_week1 > overdue_week2 * 1.5:
                profile.overdue_trend = "increasing"
            elif overdue_week1 < overdue_week2 * 0.5:
                profile.overdue_trend = "decreasing"

            # ── 倦怠风险 ──────────────────────────────────────
            cur.execute(
                f"""
                SELECT COUNT(*) as cnt FROM tasks
                WHERE user_id = {self._ph()} AND status = 'done' AND updated_at > {self._ph()}
                """,
                (user_id, (now - timedelta(days=7)).isoformat()),
            )
            recent_done = cur.fetchone()["cnt"]

            cur.execute(
                f"""
                SELECT COUNT(*) as cnt FROM tasks
                WHERE user_id = {self._ph()} AND status = 'done' AND updated_at > {self._ph()} AND updated_at <= {self._ph()}
                """,
                (user_id, (now - timedelta(days=14)).isoformat(), (now - timedelta(days=7)).isoformat()),
            )
            prev_done = cur.fetchone()["cnt"]

            if prev_done > 0 and recent_done < prev_done * 0.3:
                profile.burnout_risk = "high"
            elif prev_done > 0 and recent_done < prev_done * 0.6:
                profile.burnout_risk = "medium"

        # ── 一致性得分 ──────────────────────────────────────
        profile.consistency_score = self._calc_consistency(conn, user_id)

        return profile

    def _calc_consistency(self, conn: Any, user_id: str) -> float:
        """计算每日完成任务的一致性。"""
        date_col = self._date_expr("updated_at")
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT {date_col} as day, COUNT(*) as cnt
            FROM tasks
            WHERE user_id = {self._ph()} AND status = 'done' AND updated_at > {self._now_minus_days(14)}
            GROUP BY day
            """,
            (user_id,),
        )
        rows = cur.fetchall()

        if len(rows) < 3:
            return 0.0

        counts = [r["cnt"] for r in rows]
        avg = sum(counts) / len(counts)
        if avg == 0:
            return 0.0

        variance = sum((x - avg) ** 2 for x in counts) / len(counts)
        std_dev = variance ** 0.5
        cv = std_dev / avg

        return round(max(0.0, min(1.0, 1.0 - cv)), 2)

    def generate_insights(
        self, tasks: list[Task], user_id: str = "default"
    ) -> list[Insight]:
        """基于行为画像和当前任务状态，生成洞察列表。"""
        profile = self.build_profile(user_id)
        insights: list[Insight] = []
        now = datetime.now().astimezone()

        # ── 完成率洞察 ──────────────────────────────────────
        if profile.total_created >= 5:
            if profile.completion_rate < 0.4:
                insights.append(Insight(
                    category="risk",
                    icon="📉",
                    title="完成率偏低",
                    detail=f"你的完成率是 {profile.completion_rate:.0%}（{profile.total_completed}/{profile.total_created}）。"
                           f"建议：减少同时进行的任务数量，或者更果断地放弃不再重要的任务。",
                    priority=3,
                ))
            elif profile.completion_rate > 0.8:
                insights.append(Insight(
                    category="achievement",
                    icon="🏆",
                    title="高完成率",
                    detail=f"你的完成率是 {profile.completion_rate:.0%}，非常棒！"
                           f"保持这个节奏，你正在建立良好的工作习惯。",
                    priority=1,
                ))

        # ── 预估准确率洞察 ──────────────────────────────────
        if profile.underestimation_ratio > 1.5:
            insights.append(Insight(
                category="pattern",
                icon="⏱️",
                title="时间预估偏低",
                detail=f"你实际完成时间平均是预估的 {profile.underestimation_ratio:.1f} 倍。"
                       f"建议：下次预估时乘以 {min(profile.underestimation_ratio, 2.0):.1f}，会更接近实际。",
                priority=2,
            ))
        elif profile.estimation_accuracy > 0.8 and profile.total_completed >= 5:
            insights.append(Insight(
                category="achievement",
                icon="🎯",
                title="预估很准",
                detail=f"你的时间预估准确率达到 {profile.estimation_accuracy:.0%}，说明你很了解自己的工作节奏！",
                priority=1,
            ))

        # ── 倦怠风险洞察 ──────────────────────────────────
        if profile.burnout_risk == "high":
            insights.append(Insight(
                category="risk",
                icon="🔥",
                title="产出下降明显",
                detail="你最近一周的完成量比前一周下降了很多。可能是倦怠的信号。"
                       "建议：今天只做一件最轻松的小任务，或者干脆休息一下。",
                priority=4,
            ))
        elif profile.burnout_risk == "medium":
            insights.append(Insight(
                category="risk",
                icon="⚡",
                title="产出有所下降",
                detail="你最近的完成速度有所放缓，这是正常的波动。"
                       "保持节奏，不要给自己太大压力。",
                priority=2,
            ))

        # ── 过期趋势洞察 ──────────────────────────────────
        if profile.overdue_trend == "increasing":
            insights.append(Insight(
                category="risk",
                icon="📊",
                title="过期任务在增加",
                detail="你的过期任务数量在增长。建议集中精力清理积压，"
                       "或者重新评估这些任务的优先级——也许有些可以放弃了。",
                priority=3,
            ))

        # ── 最佳时段洞察 ──────────────────────────────────
        if profile.peak_completion_hour is not None:
            insights.append(Insight(
                category="pattern",
                icon="⏰",
                title=f"你的高效时段是 {profile.peak_completion_hour}:00",
                detail=f"你最常在这个时段完成任务。"
                       f"建议把最重要的工作安排在这个时间段。",
                priority=1,
                actionable=False,
            ))

        # ── 拖延类型洞察 ──────────────────────────────────
        if profile.procrastination_types:
            insights.append(Insight(
                category="pattern",
                icon="🤔",
                title="你容易拖延这类任务",
                detail=f"以下任务被放弃过：{'、'.join(profile.procrastination_types[:3])}。"
                       f"也许它们对你来说不够重要？考虑直接删除或重新定义。",
                priority=2,
            ))

        # ── 当前任务风险洞察 ──────────────────────────────
        overdue = [t for t in tasks if t.due_at and t.due_at < now and t.status in (TaskStatus.TODO, TaskStatus.DOING)]
        if len(overdue) >= 3:
            insights.append(Insight(
                category="risk",
                icon="🚨",
                title=f"{len(overdue)} 个任务已过期",
                detail="积压过期任务会增加焦虑。建议：选择 1-2 个最重要的处理，其余的推迟或放弃。",
                priority=4,
            ))

        # ── 大任务风险洞察 ──────────────────────────────
        large_tasks = [t for t in tasks if (t.estimated_minutes or 0) >= 90 and t.status == TaskStatus.TODO]
        if large_tasks:
            insights.append(Insight(
                category="suggestion",
                icon="📐",
                title=f"{len(large_tasks)} 个大任务需要拆分",
                detail=f"「{large_tasks[0].title}」预计 {large_tasks[0].estimated_minutes} 分钟。"
                       f"大任务容易拖延，建议拆成 20-30 分钟的小步骤。",
                priority=2,
            ))

        # 按优先级排序
        insights.sort(key=lambda x: -x.priority)
        return insights

    def get_weekly_pattern(self, user_id: str = "default") -> dict:
        """分析每周产出模式。"""
        dow_col = self._dow_expr("updated_at")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT {dow_col} as weekday, COUNT(*) as cnt
                FROM tasks
                WHERE user_id = {self._ph()} AND status = 'done'
                GROUP BY weekday
                ORDER BY cnt DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()

        day_map = {"0": "周日", "1": "周一", "2": "周二", "3": "周三", "4": "周四", "5": "周五", "6": "周六"}
        pattern = {}
        for r in rows:
            weekday = str(int(r["weekday"]))
            pattern[day_map.get(weekday, weekday)] = r["cnt"]
        return pattern

    def get_task_type_analysis(self, user_id: str = "default") -> dict:
        """分析任务类型偏好（基于标签）。"""
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT tags FROM tasks
                WHERE user_id = {self._ph()} AND status = 'done' AND tags IS NOT NULL
                """,
                (user_id,),
            )
            done_tags = cur.fetchall()

            cur.execute(
                f"""
                SELECT tags FROM tasks
                WHERE user_id = {self._ph()} AND status = 'dropped' AND tags IS NOT NULL
                """,
                (user_id,),
            )
            dropped_tags = cur.fetchall()

        from collections import Counter
        done_counter = Counter()
        dropped_counter = Counter()

        for row in done_tags:
            if row["tags"]:
                for tag in row["tags"].split(","):
                    tag = tag.strip()
                    if tag:
                        done_counter[tag] += 1

        for row in dropped_tags:
            if row["tags"]:
                for tag in row["tags"].split(","):
                    tag = tag.strip()
                    if tag:
                        dropped_counter[tag] += 1

        return {
            "completed_tags": dict(done_counter.most_common(10)),
            "dropped_tags": dict(dropped_counter.most_common(10)),
        }

    def get_consistency_score(self, user_id: str = "default") -> float:
        """计算一致性得分（0-1，基于每日完成任务的稳定性）。"""
        date_col = self._date_expr("updated_at")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT {date_col} as day, COUNT(*) as cnt
                FROM tasks
                WHERE user_id = {self._ph()} AND status = 'done' AND updated_at > {self._now_minus_days(14)}
                GROUP BY day
                """,
                (user_id,),
            )
            rows = cur.fetchall()

        if len(rows) < 3:
            return 0.0

        counts = [r["cnt"] for r in rows]
        avg = sum(counts) / len(counts)
        if avg == 0:
            return 0.0

        variance = sum((x - avg) ** 2 for x in counts) / len(counts)
        std_dev = variance ** 0.5
        cv = std_dev / avg

        score = max(0.0, min(1.0, 1.0 - cv))
        return round(score, 2)

    def get_strategic_summary(self, user_id: str = "default") -> str:
        """生成战略摘要 — 一段话总结用户的行为模式和建议。"""
        profile = self.build_profile(user_id)
        consistency = self.get_consistency_score(user_id)
        weekly = self.get_weekly_pattern(user_id)

        parts = []

        # 完成率
        if profile.total_created >= 3:
            parts.append(
                f"你创建了 {profile.total_created} 个任务，完成了 {profile.total_completed} 个"
                f"（{profile.completion_rate:.0%}）。"
            )

        # 时间模式
        if profile.avg_completion_hours > 0:
            parts.append(f"平均完成一个任务需要 {profile.avg_completion_hours:.1f} 小时。")

        if profile.underestimation_ratio > 1.3:
            parts.append(
                f"你倾向于低估任务时间（实际是预估的 {profile.underestimation_ratio:.1f} 倍），"
                f"预估时多留些缓冲会更现实。"
            )

        # 高效时段
        if profile.peak_completion_hour is not None:
            parts.append(f"你在 {profile.peak_completion_hour}:00 左右最常完成任务。")

        # 一致性
        if consistency > 0:
            if consistency > 0.7:
                parts.append(f"你的工作节奏很稳定（一致性 {consistency:.0%}），这是好习惯。")
            elif consistency < 0.4:
                parts.append(f"你的产出波动较大（一致性 {consistency:.0%}），试着建立固定的工作节奏。")

        # 最佳工作日
        if weekly:
            best_day = max(weekly, key=weekly.get)
            parts.append(f"你通常在{best_day}产出最多。")

        # 风险
        if profile.burnout_risk == "high":
            parts.append("⚠️ 最近产出明显下降，注意休息。")
        elif profile.overdue_trend == "increasing":
            parts.append("⚠️ 过期任务在增加，建议集中清理。")

        if not parts:
            return "继续使用 Momentum，我会逐渐了解你的工作模式并提供更精准的建议。"

        return "".join(parts)
