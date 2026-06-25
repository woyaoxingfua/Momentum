"""天气 & 位置 - 假数据，给 agent 当玩具用。"""
from __future__ import annotations

import random
from datetime import datetime


CITIES = {
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "成都": (30.5728, 104.0668),
    "杭州": (30.2741, 120.1551),
    "武汉": (30.5928, 114.3055),
    "西安": (34.3416, 108.9398),
    "南京": (32.0603, 118.7969),
    "重庆": (29.4316, 106.9123),
    "天津": (39.3434, 117.3616),
    "苏州": (31.2989, 120.5853),
    "长沙": (28.2282, 112.9388),
    "郑州": (34.7466, 113.6253),
    "青岛": (36.0671, 120.3826),
    "沈阳": (41.8057, 123.4328),
    "厦门": (24.4798, 118.0894),
    "哈尔滨": (45.8038, 126.5340),
    "昆明": (25.0406, 102.7129),
    "合肥": (31.8206, 117.2272),
    "济南": (36.6512, 116.6870),
    "福州": (26.0745, 119.2965),
    "南昌": (28.6829, 115.8579),
    "贵阳": (26.6470, 106.6302),
    "南宁": (22.8170, 108.3665),
    "长春": (43.8171, 125.3235),
    "兰州": (36.0611, 103.8343),
    "太原": (37.8706, 112.5489),
    "石家庄": (38.0428, 114.5149),
    "海口": (20.0444, 110.1999),
    "乌鲁木齐": (43.8256, 87.6168),
    "呼和浩特": (40.8424, 111.7490),
    "拉萨": (29.6500, 91.1000),
    "香港": (22.3193, 114.1694),
    "台北": (25.0330, 121.5654),
    "东京": (35.6762, 139.6503),
    "大阪": (34.6937, 135.5023),
    "纽约": (40.7128, -74.0060),
    "洛杉矶": (34.0522, -118.2437),
    "伦敦": (51.5074, -0.1278),
    "巴黎": (48.8566, 2.3522),
    "新加坡": (1.3521, 103.8198),
    "首尔": (37.5665, 126.9780),
    "悉尼": (-33.8688, 151.2093),
    "多伦多": (43.6532, -79.3832),
    "柏林": (52.5200, 13.4050),
    "罗马": (41.9028, 12.4964),
    "马德里": (40.4168, -3.7038),
    "曼谷": (13.7563, 100.5018),
    "迪拜": (25.2048, 55.2708),
    "孟买": (19.0760, 72.8777),
    "莫斯科": (55.7558, 37.6173),
}

ALIASES = {
    "beijing": "北京", "shanghai": "上海", "guangzhou": "广州", "shenzhen": "深圳",
    "chengdu": "成都", "hangzhou": "杭州", "wuhan": "武汉", "xian": "西安",
    "nanjing": "南京", "chongqing": "重庆", "tokyo": "东京", "osaka": "大阪",
    "new york": "纽约", "los angeles": "洛杉矶", "london": "伦敦", "paris": "巴黎",
    "singapore": "新加坡", "seoul": "首尔", "sydney": "悉尼", "toronto": "多伦多",
    "berlin": "柏林", "rome": "罗马", "madrid": "马德里", "bangkok": "曼谷",
    "dubai": "迪拜", "mumbai": "孟买", "moscow": "莫斯科",
}

CONDITIONS = [
    ("Clear", "晴朗", "☀️"),
    ("Partly Cloudy", "多云", "⛅"),
    ("Cloudy", "阴天", "☁️"),
    ("Light Rain", "小雨", "🌦️"),
    ("Rain", "中雨", "🌧️"),
    ("Thunderstorm", "雷阵雨", "⛈️"),
    ("Snow", "小雪", "🌨️"),
    ("Fog", "雾", "🌫️"),
]


def _resolve(city: str) -> tuple[str, tuple[float, float]]:
    name = ALIASES.get(city.lower(), city)
    lat, lon = CITIES.get(name, CITIES["北京"])
    return name, (lat, lon)


def get_location(city: str) -> dict:
    name, (lat, lon) = _resolve(city)
    return {
        "city": name,
        "latitude": lat,
        "longitude": lon,
        "map_url": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=12/{lat}/{lon}",
    }


def get_weather(city: str) -> dict:
    name, (lat, lon) = _resolve(city)
    condition, condition_cn, emoji = random.choice(CONDITIONS)
    base_temp = 15 - (abs(lat) * 0.3)
    temp = round(base_temp + random.uniform(-5, 10), 1)
    humidity = random.randint(30, 90)

    tips = []
    if temp < 10:
        tips.append("注意保暖")
    elif temp > 30:
        tips.append("注意防暑")
    if "Rain" in condition:
        tips.append("记得带伞")
    if humidity > 80:
        tips.append("空气潮湿")
    elif humidity < 30:
        tips.append("注意保湿")

    return {
        "city": name,
        "temperature": temp,
        "humidity": humidity,
        "condition": condition,
        "condition_cn": condition_cn,
        "emoji": emoji,
        "tips": tips,
        "updated_at": datetime.now().isoformat(),
    }


def city_supported(city: str) -> bool:
    return city.lower() in ALIASES or city in CITIES


def list_cities() -> list[str]:
    return list(CITIES.keys())
