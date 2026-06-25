from typing import TYPE_CHECKING
from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore


def create_weather_tools(store: "TaskStore", user_id: str):
    from ...services import weather as w
    from ._common import _to_json

    @function_tool
    def set_user_location(city: str) -> str:
        """设置用户默认位置。"""
        store.set_memory("user_location", city, user_id=user_id)
        return f"已设置默认位置：{city}"

    @function_tool
    def get_user_location() -> str:
        """获取用户默认位置。"""
        city = store.get_memory("user_location", user_id=user_id)
        if city:
            return _to_json({"city": city, "is_default": False})
        return _to_json({"city": "北京", "is_default": True})

    @function_tool
    def get_current_weather(city: str | None = None) -> str:
        """获取当前天气。不提供城市则使用默认位置。"""
        if not city:
            city = store.get_memory("user_location", user_id=user_id) or "北京"
        return _to_json(w.get_weather(city))

    @function_tool
    def get_location_info(city: str | None = None) -> str:
        """获取位置信息（经纬度、地图链接）。"""
        if not city:
            city = store.get_memory("user_location", user_id=user_id) or "北京"
        return _to_json(w.get_location(city))

    @function_tool
    def plan_outdoor_activity(activity: str, city: str | None = None) -> str:
        """根据天气判断是否适合户外活动。"""
        if not city:
            city = store.get_memory("user_location", user_id=user_id) or "北京"

        data = w.get_weather(city)
        good = data["condition"] in ("Clear", "Partly Cloudy")

        if good:
            return (
                f"天气不错，「{activity}」可以安排\n"
                f"{data['city']} {data['emoji']} {data['condition_cn']}，{data['temperature']}°C\n"
                + ("；".join(data["tips"]) if data["tips"] else "")
            )
        else:
            return (
                f"天气一般，「{activity}」建议改期或改室内\n"
                f"{data['city']} {data['emoji']} {data['condition_cn']}，{data['temperature']}°C\n"
                + ("；".join(data["tips"]) if data["tips"] else "")
            )

    return [set_user_location, get_user_location, get_current_weather, get_location_info, plan_outdoor_activity]
