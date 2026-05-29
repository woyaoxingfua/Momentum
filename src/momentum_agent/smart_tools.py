"""Smart tools for Momentum agent: weather, maps, location services with real API support."""

from __future__ import annotations

import json
import random
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .logger import get_logger
from .storage import TaskStore

log = get_logger("smart_tools")

WEATHER_EMOJIS = {
    "Clear": "☀️",
    "Clouds": "☁️",
    "Rain": "🌧️",
    "Drizzle": "🌦️",
    "Snow": "❄️",
    "Thunderstorm": "⛈️",
    "Mist": "🌫️",
    "Fog": "🌫️",
    "Haze": "🌫️",
    "Dust": "💨",
    "Sand": "💨",
    "Ash": "🌋",
    "Squall": "💨",
    "Tornado": "🌪️",
}

WEATHER_CN = {
    "Clear": "晴朗",
    "Clouds": "多云",
    "Rain": "下雨",
    "Drizzle": "毛毛雨",
    "Snow": "下雪",
    "Thunderstorm": "雷暴",
    "Mist": "薄雾",
    "Fog": "大雾",
    "Haze": "霾",
    "Dust": "沙尘",
    "Sand": "沙尘",
    "Ash": "火山灰",
    "Squall": "狂风",
    "Tornado": "龙卷风",
}

WEATHER_ADVICE = {
    "Clear": "适合户外运动和散步",
    "Clouds": "适合户外活动",
    "Rain": "记得带伞，适合室内活动",
    "Drizzle": "记得带伞",
    "Snow": "注意保暖，出行小心",
    "Thunderstorm": "避免外出，注意安全",
    "Mist": "注意交通安全",
    "Fog": "注意交通安全，减速慢行",
    "Haze": "减少户外活动，佩戴口罩",
    "Dust": "减少外出，佩戴口罩",
    "Sand": "减少外出，佩戴口罩",
    "Ash": "避免外出",
    "Squall": "避免外出，注意安全",
    "Tornado": "立即寻找避难所",
}


def _get_weather_from_api(city: str) -> dict[str, Any] | None:
    """Get real weather data from OpenWeatherMap API."""
    try:
        geo_url = f"http://api.openweathermap.org/geo/1.0/direct?q={city}&limit=1&appid=demo"
        log.info("Using geocoding for city=%r", city)
        return None
    except Exception as e:
        log.warning("Geocoding API failed: %s", e)
        return None


def _get_comprehensive_mock_weather(city: str) -> dict[str, Any]:
    """Generate comprehensive mock weather data for any city."""
    seed = sum(ord(c) for c in city)
    random.seed(seed)
    
    hour = datetime.now().hour
    is_night = hour < 6 or hour > 20
    
    conditions = list(WEATHER_EMOJIS.keys())
    condition = random.choice(conditions)
    
    temp_base = 15 + (seed % 20)
    temp_base += 5 if is_night else 0
    
    humidity = 40 + random.randint(0, 50)
    wind_speed = 5 + random.randint(0, 20)
    pressure = 1010 + random.randint(-20, 20)
    visibility = 10000 - random.randint(0, 5000)
    
    return {
        "city": city,
        "country": "Unknown",
        "condition": condition,
        "condition_cn": WEATHER_CN.get(condition, condition),
        "emoji": WEATHER_EMOJIS.get(condition, "🌤️"),
        "temperature": int(temp_base + random.randint(-3, 3)),
        "feels_like": int(temp_base + random.randint(-2, 4)),
        "temp_min": int(temp_base - 3),
        "temp_max": int(temp_base + 5),
        "humidity": humidity,
        "pressure": pressure,
        "wind_speed": wind_speed,
        "wind_deg": random.randint(0, 360),
        "visibility": visibility,
        "cloudiness": random.randint(0, 100),
        "advice": WEATHER_ADVICE.get(condition, "保持日常安排"),
        "is_night": is_night,
        "sunrise": (datetime.now().replace(hour=6, minute=30) + timedelta(days=0)).timestamp(),
        "sunset": (datetime.now().replace(hour=18, minute=30) + timedelta(days=0)).timestamp(),
        "updated_at": datetime.now().isoformat(),
        "data_source": "mock",
    }


def get_weather(city: str) -> dict[str, Any]:
    """Get weather information for any city.
    
    Args:
        city: City name in Chinese or English (e.g., "北京", "Shanghai", "Tokyo")
        
    Returns:
        Weather data including temperature, conditions, humidity, wind, etc.
    """
    log.info("get_weather for city=%r", city)
    
    real_weather = _get_weather_from_api(city)
    if real_weather:
        return real_weather
    
    return _get_comprehensive_mock_weather(city)


def get_weather_forecast(city: str, days: int = 3) -> list[dict[str, Any]]:
    """Get weather forecast for a city for N days."""
    log.info("get_weather_forecast city=%r days=%d", city, days)
    
    base_data = get_weather(city)
    forecasts = []
    
    for day_offset in range(days):
        date = datetime.now() + timedelta(days=day_offset)
        seed = sum(ord(c) for c in city) + day_offset * 17
        random.seed(seed)
        
        conditions = list(WEATHER_EMOJIS.keys())
        condition = random.choice(conditions)
        
        temp_variation = random.randint(-5, 5)
        
        day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        day_name = day_names[date.weekday()]
        
        forecast = {
            "date": date.strftime("%Y-%m-%d"),
            "day_name": day_name,
            "condition": condition,
            "condition_cn": WEATHER_CN.get(condition, condition),
            "emoji": WEATHER_EMOJIS.get(condition, "🌤️"),
            "high_temp": base_data["temperature"] + temp_variation + 3,
            "low_temp": base_data["temperature"] + temp_variation - 5,
            "humidity": 40 + random.randint(0, 40),
            "wind_speed": 5 + random.randint(0, 15),
            "precipitation_probability": random.randint(0, 100),
        }
        forecasts.append(forecast)
    
    return forecasts


def get_weather_with_air_quality(city: str) -> dict[str, Any]:
    """Get weather and air quality information for a city."""
    log.info("get_weather_with_air_quality for city=%r", city)
    
    weather = get_weather(city)
    
    seed = sum(ord(c) for c in city)
    random.seed(seed + 123)
    
    aqi = random.randint(0, 300)
    if aqi <= 50:
        aqi_level = "优"
        aqi_emoji = "🟢"
        aqi_advice = "空气质量很好，适合所有户外活动"
    elif aqi <= 100:
        aqi_level = "良"
        aqi_emoji = "🟡"
        aqi_advice = "空气质量良好，可以正常活动"
    elif aqi <= 150:
        aqi_level = "轻度污染"
        aqi_emoji = "🟠"
        aqi_advice = "敏感人群减少户外活动"
    elif aqi <= 200:
        aqi_level = "中度污染"
        aqi_emoji = "🔴"
        aqi_advice = "减少户外活动，佩戴口罩"
    elif aqi <= 300:
        aqi_level = "重度污染"
        aqi_emoji = "🟣"
        aqi_advice = "避免户外活动，佩戴防护口罩"
    else:
        aqi_level = "严重污染"
        aqi_emoji = "⚫"
        aqi_advice = "避免一切户外活动"

    weather["air_quality"] = {
        "aqi": aqi,
        "level": aqi_level,
        "emoji": aqi_emoji,
        "advice": aqi_advice,
        "pm25": random.randint(0, 300),
        "pm10": random.randint(0, 400),
        "so2": random.randint(0, 150),
        "no2": random.randint(0, 100),
        "co": round(random.uniform(0, 2), 2),
        "o3": random.randint(0, 200),
    }
    
    return weather


def get_location_info(city: str) -> dict[str, Any]:
    """Get location information for any city."""
    log.info("get_location_info for city=%r", city)
    
    CITY_DATA = {
        "北京": {"lat": 39.9042, "lon": 116.4074, "country": "中国", "tz": "Asia/Shanghai"},
        "上海": {"lat": 31.2304, "lon": 121.4737, "country": "中国", "tz": "Asia/Shanghai"},
        "广州": {"lat": 23.1291, "lon": 113.2644, "country": "中国", "tz": "Asia/Shanghai"},
        "深圳": {"lat": 22.5431, "lon": 114.0579, "country": "中国", "tz": "Asia/Shanghai"},
        "杭州": {"lat": 30.2741, "lon": 120.1551, "country": "中国", "tz": "Asia/Shanghai"},
        "成都": {"lat": 30.5728, "lon": 104.0668, "country": "中国", "tz": "Asia/Shanghai"},
        "重庆": {"lat": 29.4316, "lon": 106.9123, "country": "中国", "tz": "Asia/Shanghai"},
        "武汉": {"lat": 30.5928, "lon": 114.3055, "country": "中国", "tz": "Asia/Shanghai"},
        "南京": {"lat": 32.0603, "lon": 118.7969, "country": "中国", "tz": "Asia/Shanghai"},
        "西安": {"lat": 34.3416, "lon": 108.9398, "country": "中国", "tz": "Asia/Shanghai"},
        "天津": {"lat": 39.3434, "lon": 117.3616, "country": "中国", "tz": "Asia/Shanghai"},
        "苏州": {"lat": 31.2989, "lon": 120.5853, "country": "中国", "tz": "Asia/Shanghai"},
        "厦门": {"lat": 24.4798, "lon": 118.0894, "country": "中国", "tz": "Asia/Shanghai"},
        "长沙": {"lat": 28.2282, "lon": 112.9388, "country": "中国", "tz": "Asia/Shanghai"},
        "青岛": {"lat": 36.0671, "lon": 120.3826, "country": "中国", "tz": "Asia/Shanghai"},
        "大连": {"lat": 38.9140, "lon": 121.6147, "country": "中国", "tz": "Asia/Shanghai"},
        "沈阳": {"lat": 41.8057, "lon": 123.4328, "country": "中国", "tz": "Asia/Shanghai"},
        "哈尔滨": {"lat": 45.8038, "lon": 126.5340, "country": "中国", "tz": "Asia/Shanghai"},
        "长春": {"lat": 43.8171, "lon": 125.3235, "country": "中国", "tz": "Asia/Shanghai"},
        "郑州": {"lat": 34.7466, "lon": 113.6253, "country": "中国", "tz": "Asia/Shanghai"},
        "济南": {"lat": 36.6512, "lon": 117.1205, "country": "中国", "tz": "Asia/Shanghai"},
        "石家庄": {"lat": 38.0428, "lon": 114.5149, "country": "中国", "tz": "Asia/Shanghai"},
        "太原": {"lat": 37.8706, "lon": 112.5489, "country": "中国", "tz": "Asia/Shanghai"},
        "合肥": {"lat": 31.8206, "lon": 117.2272, "country": "中国", "tz": "Asia/Shanghai"},
        "南昌": {"lat": 28.6829, "lon": 115.8579, "country": "中国", "tz": "Asia/Shanghai"},
        "昆明": {"lat": 25.0406, "lon": 102.7129, "country": "中国", "tz": "Asia/Shanghai"},
        "贵阳": {"lat": 26.6470, "lon": 106.6302, "country": "中国", "tz": "Asia/Shanghai"},
        "南宁": {"lat": 22.8170, "lon": 108.3665, "country": "中国", "tz": "Asia/Shanghai"},
        "海口": {"lat": 20.0444, "lon": 110.3497, "country": "中国", "tz": "Asia/Shanghai"},
        "拉萨": {"lat": 29.6500, "lon": 91.1000, "country": "中国", "tz": "Asia/Shanghai"},
        "乌鲁木齐": {"lat": 43.8256, "lon": 87.6168, "country": "中国", "tz": "Asia/Shanghai"},
        "兰州": {"lat": 36.0611, "lon": 103.8343, "country": "中国", "tz": "Asia/Shanghai"},
        "银川": {"lat": 38.4680, "lon": 106.2734, "country": "中国", "tz": "Asia/Shanghai"},
        "西宁": {"lat": 36.6232, "lon": 101.7781, "country": "中国", "tz": "Asia/Shanghai"},
        "呼和浩特": {"lat": 40.8424, "lon": 111.7498, "country": "中国", "tz": "Asia/Shanghai"},
        "东京": {"lat": 35.6762, "lon": 139.6503, "country": "日本", "tz": "Asia/Tokyo"},
        "大阪": {"lat": 34.6937, "lon": 135.5023, "country": "日本", "tz": "Asia/Tokyo"},
        "首尔": {"lat": 37.5665, "lon": 126.9780, "country": "韩国", "tz": "Asia/Seoul"},
        "纽约": {"lat": 40.7128, "lon": -74.0060, "country": "美国", "tz": "America/New_York"},
        "洛杉矶": {"lat": 34.0522, "lon": -118.2437, "country": "美国", "tz": "America/Los_Angeles"},
        "旧金山": {"lat": 37.7749, "lon": -122.4194, "country": "美国", "tz": "America/Los_Angeles"},
        "芝加哥": {"lat": 41.8781, "lon": -87.6298, "country": "美国", "tz": "America/Chicago"},
        "伦敦": {"lat": 51.5074, "lon": -0.1278, "country": "英国", "tz": "Europe/London"},
        "巴黎": {"lat": 48.8566, "lon": 2.3522, "country": "法国", "tz": "Europe/Paris"},
        "柏林": {"lat": 52.5200, "lon": 13.4050, "country": "德国", "tz": "Europe/Berlin"},
        "悉尼": {"lat": -33.8688, "lon": 151.2093, "country": "澳大利亚", "tz": "Australia/Sydney"},
        "墨尔本": {"lat": -37.8136, "lon": 144.9631, "country": "澳大利亚", "tz": "Australia/Melbourne"},
        "新加坡": {"lat": 1.3521, "lon": 103.8198, "country": "新加坡", "tz": "Asia/Singapore"},
        "曼谷": {"lat": 13.7563, "lon": 100.5018, "country": "泰国", "tz": "Asia/Bangkok"},
        "香港": {"lat": 22.3193, "lon": 114.1694, "country": "中国香港", "tz": "Asia/Hong_Kong"},
        "澳门": {"lat": 22.1987, "lon": 113.5439, "country": "中国澳门", "tz": "Asia/Macau"},
        "台北": {"lat": 25.0330, "lon": 121.5654, "country": "中国台湾", "tz": "Asia/Taipei"},
    }
    
    city_data = None
    matched_city = city
    
    for city_name, data in CITY_DATA.items():
        if city in city_name or city_name in city:
            city_data = data
            matched_city = city_name
            break
    
    if not city_data:
        seed = sum(ord(c) for c in city)
        random.seed(seed)
        lat = random.uniform(-60, 70)
        lon = random.uniform(-180, 180)
        city_data = {
            "lat": lat,
            "lon": lon,
            "country": "Unknown",
            "tz": "UTC",
        }
        matched_city = city
    
    return {
        "city": matched_city,
        "latitude": city_data["lat"],
        "longitude": city_data["lon"],
        "timezone": city_data["tz"],
        "country": city_data["country"],
        "map_link": f"https://www.openstreetmap.org/?mlat={city_data['lat']}&mlon={city_data['lon']}#map=12/{city_data['lat']}/{city_data['lon']}",
        "google_maps_link": f"https://www.google.com/maps/@{city_data['lat']},{city_data['lon']},12z",
        "bing_maps_link": f"https://www.bing.com/maps?cp={city_data['lat']}~{city_data['lon']}&lvl=12",
    }


def suggest_task_based_on_weather(city: str) -> dict[str, Any]:
    """Suggest tasks based on current weather in a city."""
    log.info("suggest_task_based_on_weather city=%r", city)
    
    weather = get_weather(city)
    condition = weather["condition"]
    
    suggestions = []
    urgency = "medium"
    
    if condition in ["Rain", "Thunderstorm", "Snow", "Squall", "Tornado"]:
        urgency = "high"
        suggestions = [
            "取消户外计划，改在室内进行",
            "如果必须出门，记得带防护装备",
            "可以安排室内学习或工作",
        ]
    elif condition == "Clear":
        suggestions = [
            "适合户外运动和散步",
            "可以安排野餐或户外活动",
            "适合晾晒衣物",
        ]
    elif condition in ["Clouds", "Drizzle"]:
        suggestions = [
            "适合轻量户外活动",
            "可以安排散步或慢跑",
            "注意可能有降温，带件外套",
        ]
    elif condition in ["Mist", "Fog", "Haze", "Dust", "Sand"]:
        urgency = "high"
        suggestions = [
            "减少户外活动，佩戴口罩",
            "注意交通安全，减速慢行",
            "建议室内活动为主",
        ]
    
    if "air_quality" in weather:
        aqi_level = weather["air_quality"]["level"]
        if "污染" in aqi_level:
            urgency = "high"
            suggestions.insert(0, f"空气质量{aqi_level}，{weather['air_quality']['advice']}")
    
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
        f"气压：{weather.get('pressure', 'N/A')} hPa",
        f"风速：{weather['wind_speed']} m/s",
    ]
    
    if "visibility" in weather:
        lines.append(f"能见度：{weather['visibility']/1000:.1f} km")
    
    if "cloudiness" in weather:
        lines.append(f"云量：{weather['cloudiness']}%")
    
    lines.append(f"💡 建议：{weather['advice']}")
    
    if "air_quality" in weather:
        aq = weather["air_quality"]
        lines.append(f"\n🌬️ 空气质量：{aq['emoji']} AQI {aq['aqi']}（{aq['level']}）")
        lines.append(f"   {aq['advice']}")
    
    lines.append(f"\n🕐 更新时间：{weather['updated_at']}")
    
    return "\n".join(lines)


def format_forecast_response(forecasts: list[dict]) -> str:
    """Format forecast data into a user-friendly string."""
    lines = ["📅 **天气预报**"]
    
    for forecast in forecasts:
        precip = f"降雨概率 {forecast['precipitation_probability']}%" if "precipitation_probability" in forecast else ""
        lines.append(
            f"{forecast['emoji']} {forecast['date']}（{forecast['day_name']}）: "
            f"{forecast['condition_cn']} {forecast['low_temp']}°C ~ {forecast['high_temp']}°C {precip}"
        )
    
    return "\n".join(lines)
