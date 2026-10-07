"""Weather and location data powered by Open-Meteo (no API key required)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS = 8
WEATHER_CACHE_SECONDS = 10 * 60
GEOCODING_CACHE_SECONDS = 24 * 60 * 60


class WeatherServiceError(RuntimeError):
    """Open-Meteo could not provide a valid response."""


class CityNotFoundError(WeatherServiceError):
    """The requested city could not be found by the geocoding service."""


# Coordinates are retained for common cities so they work without an extra
# geocoding request. Unlisted city names are resolved through Open-Meteo.
CITIES = {
    "北京": (39.9042, 116.4074), "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644), "深圳": (22.5431, 114.0579),
    "成都": (30.5728, 104.0668), "杭州": (30.2741, 120.1551),
    "武汉": (30.5928, 114.3055), "西安": (34.3416, 108.9398),
    "南京": (32.0603, 118.7969), "重庆": (29.4316, 106.9123),
    "天津": (39.3434, 117.3616), "苏州": (31.2989, 120.5853),
    "长沙": (28.2282, 112.9388), "郑州": (34.7466, 113.6253),
    "青岛": (36.0671, 120.3826), "沈阳": (41.8057, 123.4328),
    "厦门": (24.4798, 118.0894), "哈尔滨": (45.8038, 126.5340),
    "昆明": (25.0406, 102.7129), "合肥": (31.8206, 117.2272),
    "济南": (36.6512, 116.6870), "福州": (26.0745, 119.2965),
    "南昌": (28.6829, 115.8579), "贵阳": (26.6470, 106.6302),
    "南宁": (22.8170, 108.3665), "长春": (43.8171, 125.3235),
    "兰州": (36.0611, 103.8343), "太原": (37.8706, 112.5489),
    "石家庄": (38.0428, 114.5149), "海口": (20.0444, 110.1999),
    "乌鲁木齐": (43.8256, 87.6168), "呼和浩特": (40.8424, 111.7490),
    "拉萨": (29.6500, 91.1000), "香港": (22.3193, 114.1694),
    "台北": (25.0330, 121.5654), "东京": (35.6762, 139.6503),
    "大阪": (34.6937, 135.5023), "纽约": (40.7128, -74.0060),
    "洛杉矶": (34.0522, -118.2437), "伦敦": (51.5074, -0.1278),
    "巴黎": (48.8566, 2.3522), "新加坡": (1.3521, 103.8198),
    "首尔": (37.5665, 126.9780), "悉尼": (-33.8688, 151.2093),
    "多伦多": (43.6532, -79.3832), "柏林": (52.5200, 13.4050),
    "罗马": (41.9028, 12.4964), "马德里": (40.4168, -3.7038),
    "曼谷": (13.7563, 100.5018), "迪拜": (25.2048, 55.2708),
    "孟买": (19.0760, 72.8777), "莫斯科": (55.7558, 37.6173),
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

# WMO weather interpretation codes returned by Open-Meteo.
WEATHER_CODES = {
    0: ("Clear", "晴朗", "☀️"),
    1: ("Clear", "大致晴朗", "🌤️"),
    2: ("Partly Cloudy", "局部多云", "⛅"),
    3: ("Cloudy", "阴天", "☁️"),
    45: ("Fog", "雾", "🌫️"), 48: ("Fog", "冻雾", "🌫️"),
    51: ("Drizzle", "毛毛雨", "🌦️"), 53: ("Drizzle", "毛毛雨", "🌦️"),
    55: ("Drizzle", "强毛毛雨", "🌧️"), 56: ("Freezing Rain", "冻毛毛雨", "🌧️"),
    57: ("Freezing Rain", "强冻毛毛雨", "🌧️"),
    61: ("Rain", "小雨", "🌦️"), 63: ("Rain", "中雨", "🌧️"),
    65: ("Rain", "大雨", "🌧️"), 66: ("Freezing Rain", "冻雨", "🌧️"),
    67: ("Freezing Rain", "强冻雨", "🌧️"),
    71: ("Snow", "小雪", "🌨️"), 73: ("Snow", "中雪", "🌨️"),
    75: ("Snow", "大雪", "❄️"), 77: ("Snow", "雪粒", "🌨️"),
    80: ("Rain", "阵雨", "🌦️"), 81: ("Rain", "强阵雨", "🌧️"),
    82: ("Rain", "暴雨", "🌧️"), 85: ("Snow", "阵雪", "🌨️"),
    86: ("Snow", "强阵雪", "❄️"), 95: ("Thunderstorm", "雷暴", "⛈️"),
    96: ("Thunderstorm", "雷暴伴冰雹", "⛈️"), 99: ("Thunderstorm", "强雷暴伴冰雹", "⛈️"),
}

_geocoding_cache: dict[str, tuple[float, dict]] = {}
_weather_cache: dict[str, tuple[float, dict]] = {}


def _fetch_json(url: str) -> dict:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Momentum/0.1 Open-Meteo client"},
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeatherServiceError(f"Open-Meteo 请求失败：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        reason = payload.get("reason", "返回了无效响应") if isinstance(payload, dict) else "返回了无效响应"
        raise WeatherServiceError(f"Open-Meteo 响应错误：{reason}")
    return payload


def _resolve(city: str) -> dict:
    query = (city or "").strip()
    if not query:
        raise CityNotFoundError("城市名称不能为空")

    canonical = ALIASES.get(query.casefold(), query)
    coords = CITIES.get(canonical)
    if coords:
        return {"city": canonical, "latitude": coords[0], "longitude": coords[1], "country": ""}

    cache_key = query.casefold()
    cached = _geocoding_cache.get(cache_key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return dict(cached[1])

    params = urlencode({"name": query, "count": 1, "language": "zh", "format": "json"})
    payload = _fetch_json(f"{GEOCODING_URL}?{params}")
    results = payload.get("results") or []
    if not results:
        raise CityNotFoundError(f"Open-Meteo 地理编码没有找到城市：{query}")
    result = results[0]
    try:
        location = {
            "city": str(result["name"]),
            "latitude": float(result["latitude"]),
            "longitude": float(result["longitude"]),
            "country": str(result.get("country", "")),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherServiceError("Open-Meteo 地理编码响应缺少有效坐标") from exc
    _geocoding_cache[cache_key] = (now + GEOCODING_CACHE_SECONDS, location)
    return dict(location)


def get_location(city: str) -> dict:
    location = _resolve(city)
    lat, lon = location["latitude"], location["longitude"]
    return {
        **location,
        "map_url": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=12/{lat}/{lon}",
    }


def get_weather(city: str) -> dict:
    location = _resolve(city)
    cache_key = f"{location['latitude']:.4f},{location['longitude']:.4f}"
    now = time.monotonic()
    cached = _weather_cache.get(cache_key)
    if cached and cached[0] > now:
        return dict(cached[1])

    params = urlencode({
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "current": "temperature_2m,relative_humidity_2m,weather_code,precipitation,wind_speed_10m",
        "timezone": "auto",
    })
    payload = _fetch_json(f"{FORECAST_URL}?{params}")
    current = payload.get("current") or {}
    try:
        temperature = round(float(current["temperature_2m"]), 1)
        humidity = int(current["relative_humidity_2m"])
        code = int(current["weather_code"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherServiceError("Open-Meteo 天气响应缺少当前温度、湿度或天气代码") from exc

    condition, condition_cn, emoji = WEATHER_CODES.get(code, ("Unknown", "天气状况未知", "🌡️"))
    tips = []
    if temperature < 10:
        tips.append("注意保暖")
    elif temperature > 30:
        tips.append("注意防暑")
    if condition in {"Drizzle", "Freezing Rain", "Rain", "Snow", "Thunderstorm"}:
        tips.append("注意降水，外出可准备雨具")
    if humidity > 80:
        tips.append("空气潮湿")
    elif humidity < 30:
        tips.append("注意保湿")

    weather = {
        **location,
        "temperature": temperature,
        "humidity": humidity,
        "condition": condition,
        "condition_cn": condition_cn,
        "emoji": emoji,
        "weather_code": code,
        "tips": tips,
        "updated_at": current.get("time") or datetime.now(timezone.utc).isoformat(),
        "source": "Open-Meteo",
    }
    _weather_cache[cache_key] = (now + WEATHER_CACHE_SECONDS, weather)
    return dict(weather)


def city_supported(city: str) -> bool:
    query = (city or "").strip()
    return query.casefold() in ALIASES or query in CITIES


def list_cities() -> list[str]:
    return list(CITIES.keys())


def search_cities(query: str, *, count: int = 8) -> list[dict]:
    """Search Open-Meteo geocoding and return safe, normalized city choices."""
    name = (query or "").strip()
    if len(name) < 2:
        return []
    if len(name) > 100:
        raise CityNotFoundError("城市搜索内容不能超过 100 个字符")
    params = urlencode({"name": name, "count": min(max(int(count), 1), 10), "language": "zh", "format": "json"})
    payload = _fetch_json(f"{GEOCODING_URL}?{params}")
    results = payload.get("results") or []
    cities = []
    for result in results:
        try:
            cities.append({
                "name": str(result["name"]),
                "country": str(result.get("country", "")),
                "admin1": str(result.get("admin1", "")),
                "latitude": float(result["latitude"]),
                "longitude": float(result["longitude"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return cities
