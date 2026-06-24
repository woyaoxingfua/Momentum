"""
天气和位置工具 - Weather and Location Tools
提供天气查询和位置信息工具函数
"""
import json
from typing import TYPE_CHECKING
from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore
    from ...services.weather import WeatherService
    from ...services.location import LocationService


def _to_json(obj) -> str:
    """将对象转换为 JSON 字符串，确保工具输出为文本格式"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def create_weather_tools(store: 'TaskStore', user_id: str):
    """创建天气和位置相关的工具函数"""
    from ...services.weather import WeatherService
    from ...services.location import LocationService
    
    weather_service = WeatherService()
    location_service = LocationService()
    
    @function_tool
    def set_user_location(city: str) -> str:
        """设置用户默认位置
        
        Args:
            city: 城市名称
        """
        store.set_memory("user_location", city, user_id=user_id)
        return f"✅ 已设置默认位置为：{city}"
    
    @function_tool
    def get_user_location() -> str:
        """获取用户默认位置"""
        city = store.get_memory("user_location", user_id=user_id)
        if not city:
            return _to_json({
                "city": "北京",
                "is_default": True,
                "message": "还未设置默认位置，当前使用默认位置：北京"
            })
        return _to_json({
            "city": city,
            "is_default": False,
            "message": f"当前保存的位置是：{city}"
        })
    
    @function_tool
    def get_current_weather(city: str | None = None) -> str:
        """获取当前天气
        
        Args:
            city: 城市名称（不提供则使用默认位置）
        """
        if not city:
            saved_city = store.get_memory("user_location", user_id=user_id)
            if saved_city:
                city = saved_city
            else:
                city = "北京"
        
        return _to_json(weather_service.get_weather(city))
    
    @function_tool
    def get_location_info(city: str | None = None) -> str:
        """获取位置信息
        
        Args:
            city: 城市名称（不提供则使用默认位置）
        """
        if not city:
            saved_city = store.get_memory("user_location", user_id=user_id)
            if saved_city:
                city = saved_city
            else:
                city = "北京"
        
        return _to_json(location_service.get_location_info(city))
    
    @function_tool
    def plan_outdoor_activity(activity: str, city: str | None = None, target_date: str | None = None) -> str:
        """规划户外活动
        
        Args:
            activity: 活动描述
            city: 城市（不提供则使用默认位置）
            target_date: 目标日期
        """
        if not city:
            saved_city = store.get_memory("user_location", user_id=user_id)
            if saved_city:
                city = saved_city
            else:
                city = "北京"
        
        weather = weather_service.get_weather(city)
        good_conditions = ["Clear", "Partly Cloudy"]
        is_good_weather = weather["condition"] in good_conditions
        
        if is_good_weather:
            rec = f"✅ 天气不错！「{activity}」适合进行\n"
            rec += f"📍 {city} | {weather['emoji']} {weather['condition_cn']}，{weather['temperature']}°C\n"
            if weather['recommendations']:
                rec += "💡 " + "；".join(weather['recommendations'])
        else:
            rec = f"⚠️ 建议重新考虑「{activity}」\n"
            rec += f"📍 {city} | {weather['emoji']} {weather['condition_cn']}，{weather['temperature']}°C\n"
            if weather['recommendations']:
                rec += "💡 " + "；".join(weather['recommendations'])
            rec += "\n建议改为室内活动或改期"
        
        return rec
    
    return [
        set_user_location,
        get_user_location,
        get_current_weather,
        get_location_info,
        plan_outdoor_activity,
    ]
