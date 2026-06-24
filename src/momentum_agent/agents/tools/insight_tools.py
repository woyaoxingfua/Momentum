"""洞察工具 — 让 Agent 能访问行为分析数据。"""
import json
from typing import TYPE_CHECKING
from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore


def _to_json(obj) -> str:
    """将对象转换为 JSON 字符串，确保工具输出为文本格式"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def create_insight_tools(store: 'TaskStore', user_id: str):
    """创建洞察相关的工具函数。"""

    @function_tool
    def get_behavioral_profile() -> str:
        """获取用户行为画像（完成率、预估准确率、高效时段、倦怠风险等）。"""
        from ...insights import InsightsEngine
        engine = InsightsEngine(store.db_path)
        profile = engine.build_profile(user_id)
        return _to_json(profile.to_dict())

    @function_tool
    def get_insights() -> str:
        """获取当前任务的行为洞察列表（风险、模式、建议、成就）。"""
        from ...insights import InsightsEngine
        engine = InsightsEngine(store.db_path)
        tasks = store.list_tasks(status=None, user_id=user_id)
        insights = engine.generate_insights(tasks, user_id)
        return _to_json([
            {
                "category": i.category,
                "icon": i.icon,
                "title": i.title,
                "detail": i.detail,
                "actionable": i.actionable,
                "priority": i.priority,
            }
            for i in insights
        ])

    @function_tool
    def get_strategic_summary() -> str:
        """获取行为分析的战略摘要（一段话总结用户模式和建议）。"""
        from ...insights import InsightsEngine
        engine = InsightsEngine(store.db_path)
        return engine.get_strategic_summary(user_id)

    @function_tool
    def estimate_task_smart(title: str, priority: str = "medium") -> str:
        """基于历史数据智能预估任务时间。

        根据用户过去的完成记录，给出更准确的时间预估。
        """
        from ...insights import InsightsEngine
        engine = InsightsEngine(store.db_path)
        profile = engine.build_profile(user_id)

        base_minutes = 30

        if profile.avg_completion_hours > 0:
            base_minutes = int(profile.avg_completion_hours * 60)

        if profile.underestimation_ratio > 1.2:
            base_minutes = int(base_minutes * min(profile.underestimation_ratio, 2.0))

        if profile.preferred_task_duration:
            base_minutes = profile.preferred_task_duration

        return _to_json({
            "title": title,
            "estimated_minutes": base_minutes,
            "based_on": f"基于 {profile.total_completed} 个已完成任务的模式",
            "confidence": "high" if profile.total_completed >= 10 else "medium" if profile.total_completed >= 5 else "low",
        })

    return [
        get_behavioral_profile,
        get_insights,
        get_strategic_summary,
        estimate_task_smart,
    ]
