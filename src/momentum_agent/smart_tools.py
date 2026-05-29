"""Smart tools for Momentum agent: weather, maps, location services."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .logger import get_logger
from .storage import TaskStore

log = get_logger("smart_tools")

# Weather emojis for different conditions
WEATHER_EMOJIS = {
    "sunny": "☀️",
    "partly_cloudy": "⛅",
    "cloudy": "☁️",
    "rainy": "🌧️",
    "snowy": "❄️",
    "thunderstorm": "⛈️",
    "foggy": "🌫️",
    "windy": "💨",
}

# Location data for common cities (latitude, longitude)
CITY_COORDINATES = {
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "杭州": (30.2741, 120.1551),
    "成都": (30.5728, 104.0668),
    "重庆": (29.4316, 106.9123),
    "武汉": (30.5928, 114.3055),
    "南京": (32.0603, 118.7969),
    "西安": (34.3416, 108.9398),
    "东京": (35.6762, 139.6503),
    "纽约": (40.7128, -74.0060),
    "伦敦": (51.5074, -0.1278),
    "巴黎": (48.8566, 2.3522),
    "悉尼": (-33.8688, 151.2093),
}

# Weather conditions and their descriptions
WEATHER_CONDITIONS = [
    ("sunny", "晴朗", "适合户外运动和散步"),
    ("partly_cloudy", "多云", "适合户外活动"),
    ("cloudy", "阴天", "适合室内活动"),
    ("rainy", "下雨", "记得带伞，适合室内活动"),
    ("snowy", "下雪", "注意保暖，出行小心"),
    ("thunderstorm", "雷暴", "避免外出，注意安全"),
    ("foggy", "有雾", "注意交通安全"),
    ("windy", "大风", "注意防风"),
]


def _get_weather_for_city(city: str) -> dict[str, Any]:
    """Get mock weather data for a city (since we don't have real API access)."""
    
    # Use city name as seed to make weather consistent for the same city
    seed = sum(ord(c) for c in city)
    random.seed(seed)
    
    # Get current hour to determine if it's day or night
    hour = datetime.now().hour
    is_night = hour < 6 or hour > 20
    
    # Random weather conditions based on seed
    condition_idx = seed % len(WEATHER_CONDITIONS)
    condition, condition_cn, advice = WEATHER_CONDITIONS[condition_idx]
    
    # Temperature based on city and season (simplified)
    temp_base = 15 + (seed % 20)
    if city in ["广州", "深圳", "上海", "杭州"]:
        temp_base += 8  # Warmer cities
    elif city in ["北京", "西安", "成都"]:
        temp_base -= 3  # Cooler cities
    
    # Humidity
    humidity = 40 + (seed % 50)
    
    # Wind speed
    wind_speed = 5 + (seed % 20)
    
    # Get coordinates
    coords = CITY_COORDINATES.get(city, (39.9042, 116.4074))  # Default to Beijing
    
    return {
        "city": city,
        "latitude": coords[0],
        "longitude": coords[1],
        "condition": condition,
        "condition_cn": condition_cn,
        "emoji": WEATHER_EMOJIS.get(condition, "🌤️"),
        "temperature": int(temp_base + random.randint(-3, 3)),
        "feels_like": int(temp_base + random.randint(-2, 4)),
        "humidity": humidity,
        "wind_speed": wind_speed,
        "advice": advice,
        "is_night": is_night,
        "updated_at": datetime.now().isoformat(),
    }


def get_weather(city: str) -> dict[str, Any]:
    """Get weather information for a city.
    
    Args:
        city: City name in Chinese or English
        
    Returns:
        Weather data including temperature, conditions, humidity, etc.
    """
    log.info("get_weather for city=%r", city)
    
    # Try to match city name (case insensitive, simplified)
    matched_city = None
    for city_name in CITY_COORDINATES.keys():
        if city in city_name or city_name in city:
            matched_city = city_name
            break
    
    if not matched_city:
        # If no match, default to Beijing
        matched_city = "北京"
        log.warning("City %r not found, defaulting to Beijing", city)
    
    return _get_weather_for_city(matched_city)


def get_weather_forecast(city: str, days: int = 3) -> list[dict[str, Any]]:
    """Get weather forecast for a city for N days.
    
    Args:
        city: City name
        days: Number of days to forecast (1-7)
        
    Returns:
        List of weather forecasts, one per day
    """
    log.info("get_weather_forecast city=%r days=%d", city, days)
    
    base_data = get_weather(city)
    forecasts = []
    
    for day_offset in range(days):
        date = datetime.now() + timedelta(days=day_offset)
        seed = sum(ord(c) for c in city) + day_offset
        random.seed(seed)
        
        condition_idx = seed % len(WEATHER_CONDITIONS)
        condition, condition_cn, _ = WEATHER_CONDITIONS[condition_idx]
        
        temp_variation = random.randint(-5, 5)
        
        forecast = {
            "date": date.strftime("%Y-%m-%d"),
            "day_of_week": date.strftime("%A"),
            "condition": condition,
            "condition_cn": condition_cn,
            "emoji": WEATHER_EMOJIS.get(condition, "🌤️"),
            "high_temp": base_data["temperature"] + temp_variation + 3,
            "low_temp": base_data["temperature"] + temp_variation - 5,
        }
        forecasts.append(forecast)
    
    return forecasts


def get_location_info(city: str) -> dict[str, Any]:
    """Get location information for a city.
    
    Args:
        city: City name
        
    Returns:
        Location data including coordinates, timezone info, etc.
    """
    log.info("get_location_info for city=%r", city)
    
    coords = CITY_COORDINATES.get(city)
    if not coords:
        # Try to find similar city
        for city_name in CITY_COORDINATES.keys():
            if city in city_name or city_name in city:
                coords = CITY_COORDINATES[city_name]
                city = city_name
                break
    
    if not coords:
        coords = (39.9042, 116.4074)  # Default to Beijing
        city = "北京"
    
    return {
        "city": city,
        "latitude": coords[0],
        "longitude": coords[1],
        "timezone": "Asia/Shanghai" if city in CITY_COORDINATES.keys() and CITY_COORDINATES.get(city) and coords[1] > 73 and coords[1] < 135 else "UTC",
        "country": "China" if coords[1] > 73 and coords[1] < 135 and coords[0] > 18 and coords[0] < 54 else "Unknown",
        "map_link": f"https://www.openstreetmap.org/?mlat={coords[0]}&mlon={coords[1]}#map=12/{coords[0]}/{coords[1]}",
    }


def suggest_task_based_on_weather(city: str) -> dict[str, Any]:
    """Suggest tasks based on current weather in a city.
    
    Args:
        city: City name
        
    Returns:
        Suggestions including task ideas, urgency, and reasoning
    """
    log.info("suggest_task_based_on_weather city=%r", city)
    
    weather = get_weather(city)
    condition = weather["condition"]
    
    suggestions = []
    urgency = "medium"
    
    if condition in ["rainy", "thunderstorm", "snowy"]:
        urgency = "high"
        suggestions = [
            "取消户外计划，改在室内进行",
            "如果必须出门，记得带伞",
            "可以安排室内学习或工作",
        ]
    elif condition == "sunny":
        suggestions = [
            "适合户外运动和散步",
            "可以安排野餐或户外活动",
            "适合晾晒衣物",
        ]
    elif condition in ["partly_cloudy", "cloudy"]:
        suggestions = [
            "适合轻量户外活动",
            "可以安排散步或慢跑",
            "注意可能有降温，带件外套",
        ]
    elif condition == "windy":
        suggestions = [
            "注意防风，减少户外活动",
            "关好门窗，固定好户外物品",
        ]
    elif condition == "foggy":
        urgency = "high"
        suggestions = [
            "注意交通安全，减速慢行",
            "如果能见度低，避免驾车",
        ]
    
    return {
        "weather": weather,
        "suggestions": suggestions,
        "urgency": urgency,
        "recommendation": suggestions[0] if suggestions else "保持日常安排",
    }


def format_weather_response(weather: dict) -> str:
    """Format weather data into a user-friendly string."""
    lines = [
        f"{weather['emoji']} **{weather['city']}天气**",
        f"状态：{weather['condition_cn']}",
        f"温度：{weather['temperature']}°C（体感 {weather['feels_like']}°C）",
        f"湿度：{weather['humidity']}%",
        f"风速：{weather['wind_speed']} km/h",
        f"建议：{weather['advice']}",
        f"更新时间：{weather['updated_at']}",
    ]
    return "\n".join(lines)


def format_forecast_response(forecasts: list[dict]) -> str:
    """Format forecast data into a user-friendly string."""
    lines = ["📅 **天气预报**"]
    for forecast in forecasts:
        lines.append(
            f"{forecast['emoji']} {forecast['date']} ({forecast['day_of_week']}): "
            f"{forecast['condition_cn']} {forecast['low_temp']}°C ~ {forecast['high_temp']}°C"
        )
    return "\n".join(lines)
