"""
天气查询服务 - Weather Query Service
提供城市天气信息查询功能
"""
import random
from datetime import datetime
from typing import Optional


class WeatherService:
    """天气查询服务类"""
    
    def __init__(self):
        self._city_data = {
            "北京": {"lat": 39.9042, "lon": 116.4074, "country": "中国"},
            "上海": {"lat": 31.2304, "lon": 121.4737, "country": "中国"},
            "广州": {"lat": 23.1291, "lon": 113.2644, "country": "中国"},
            "深圳": {"lat": 22.5431, "lon": 114.0579, "country": "中国"},
            "成都": {"lat": 30.5728, "lon": 104.0668, "country": "中国"},
            "杭州": {"lat": 30.2741, "lon": 120.1551, "country": "中国"},
            "武汉": {"lat": 30.5928, "lon": 114.3055, "country": "中国"},
            "西安": {"lat": 34.3416, "lon": 108.9398, "country": "中国"},
            "南京": {"lat": 32.0603, "lon": 118.7969, "country": "中国"},
            "重庆": {"lat": 29.4316, "lon": 106.9123, "country": "中国"},
            "天津": {"lat": 39.3434, "lon": 117.3616, "country": "中国"},
            "苏州": {"lat": 31.2989, "lon": 120.5853, "country": "中国"},
            "长沙": {"lat": 28.2282, "lon": 112.9388, "country": "中国"},
            "郑州": {"lat": 34.7466, "lon": 113.6253, "country": "中国"},
            "青岛": {"lat": 36.0671, "lon": 120.3826, "country": "中国"},
            "沈阳": {"lat": 41.8057, "lon": 123.4328, "country": "中国"},
            "大连": {"lat": 38.9140, "lon": 121.6147, "country": "中国"},
            "厦门": {"lat": 24.4798, "lon": 118.0894, "country": "中国"},
            "西安": {"lat": 34.3416, "lon": 108.9398, "country": "中国"},
            "哈尔滨": {"lat": 45.8038, "lon": 126.5340, "country": "中国"},
            "昆明": {"lat": 25.0406, "lon": 102.7129, "country": "中国"},
            "太原": {"lat": 37.8706, "lon": 112.5489, "country": "中国"},
            "合肥": {"lat": 31.8206, "lon": 117.2272, "country": "中国"},
            "济南": {"lat": 36.6512, "lon": 116.6870, "country": "中国"},
            "福州": {"lat": 26.0745, "lon": 119.2965, "country": "中国"},
            "石家庄": {"lat": 38.0428, "lon": 114.5149, "country": "中国"},
            "南昌": {"lat": 28.6829, "lon": 115.8579, "country": "中国"},
            "贵阳": {"lat": 26.6470, "lon": 106.6302, "country": "中国"},
            "南宁": {"lat": 22.8170, "lon": 108.3665, "country": "中国"},
            "长春": {"lat": 43.8171, "lon": 125.3235, "country": "中国"},
            "兰州": {"lat": 36.0611, "lon": 103.8343, "country": "中国"},
            "海口": {"lat": 20.0444, "lon": 110.1999, "country": "中国"},
            "乌鲁木齐": {"lat": 43.8256, "lon": 87.6168, "country": "中国"},
            "银川": {"lat": 38.4680, "lon": 106.2731, "country": "中国"},
            "西宁": {"lat": 36.6171, "lon": 101.7782, "country": "中国"},
            "拉萨": {"lat": 29.6500, "lon": 91.1000, "country": "中国"},
            "呼和浩特": {"lat": 40.8424, "lon": 111.7490, "country": "中国"},
            "香港": {"lat": 22.3193, "lon": 114.1694, "country": "中国"},
            "澳门": {"lat": 22.1987, "lon": 113.5439, "country": "中国"},
            "台北": {"lat": 25.0330, "lon": 121.5654, "country": "中国"},
            "tokyo": {"lat": 35.6762, "lon": 139.6503, "country": "日本"},
            "osaka": {"lat": 34.6937, "lon": 135.5023, "country": "日本"},
            "kyoto": {"lat": 35.0116, "lon": 135.7681, "country": "日本"},
            "yokohama": {"lat": 35.4437, "lon": 139.6380, "country": "日本"},
            "nagoya": {"lat": 35.1815, "lon": 136.9066, "country": "日本"},
            "new york": {"lat": 40.7128, "lon": -74.0060, "country": "美国"},
            "los angeles": {"lat": 34.0522, "lon": -118.2437, "country": "美国"},
            "san francisco": {"lat": 37.7749, "lon": -122.4194, "country": "美国"},
            "chicago": {"lat": 41.8781, "lon": -87.6298, "country": "美国"},
            "seattle": {"lat": 47.6062, "lon": -122.3321, "country": "美国"},
            "boston": {"lat": 42.3601, "lon": -71.0589, "country": "美国"},
            "miami": {"lat": 25.7617, "lon": -80.1918, "country": "美国"},
            "london": {"lat": 51.5074, "lon": -0.1278, "country": "英国"},
            "manchester": {"lat": 53.4808, "lon": -2.2426, "country": "英国"},
            "edinburgh": {"lat": 55.9533, "lon": -3.1883, "country": "英国"},
            "paris": {"lat": 48.8566, "lon": 2.3522, "country": "法国"},
            "marseille": {"lat": 43.2965, "lon": 5.3698, "country": "法国"},
            "lyon": {"lat": 45.7640, "lon": 4.8357, "country": "法国"},
            "berlin": {"lat": 52.5200, "lon": 13.4050, "country": "德国"},
            "munich": {"lat": 48.1351, "lon": 11.5820, "country": "德国"},
            "hamburg": {"lat": 53.5511, "lon": 9.9937, "country": "德国"},
            "frankfurt": {"lat": 50.1109, "lon": 8.6821, "country": "德国"},
            "rome": {"lat": 41.9028, "lon": 12.4964, "country": "意大利"},
            "milan": {"lat": 45.4642, "lon": 9.1900, "country": "意大利"},
            "venice": {"lat": 45.4408, "lon": 12.3155, "country": "意大利"},
            "florence": {"lat": 43.7696, "lon": 11.2558, "country": "意大利"},
            "madrid": {"lat": 40.4168, "lon": -3.7038, "country": "西班牙"},
            "barcelona": {"lat": 41.3851, "lon": 2.1734, "country": "西班牙"},
            "singapore": {"lat": 1.3521, "lon": 103.8198, "country": "新加坡"},
            "sydney": {"lat": -33.8688, "lon": 151.2093, "country": "澳大利亚"},
            "melbourne": {"lat": -37.8136, "lon": 144.9631, "country": "澳大利亚"},
            "toronto": {"lat": 43.6532, "lon": -79.3832, "country": "加拿大"},
            "vancouver": {"lat": 49.2827, "lon": -123.1207, "country": "加拿大"},
            "montreal": {"lat": 45.5017, "lon": -73.5673, "country": "加拿大"},
            "seoul": {"lat": 37.5665, "lon": 126.9780, "country": "韩国"},
            "busan": {"lat": 35.1796, "lon": 129.0756, "country": "韩国"},
            "bangkok": {"lat": 13.7563, "lon": 100.5018, "country": "泰国"},
            "dubai": {"lat": 25.2048, "lon": 55.2708, "country": "阿联酋"},
            "mumbai": {"lat": 19.0760, "lon": 72.8777, "country": "印度"},
            "delhi": {"lat": 28.7041, "lon": 77.1025, "country": "印度"},
        }
        
        self._weather_conditions = [
            ("Clear", "晴朗", "☀️"),
            ("Partly Cloudy", "多云", "⛅"),
            ("Cloudy", "阴天", "☁️"),
            ("Light Rain", "小雨", "🌦️"),
            ("Rain", "中雨", "🌧️"),
            ("Heavy Rain", "大雨", "🌧️"),
            ("Thunderstorm", "雷阵雨", "⛈️"),
            ("Snow", "小雪", "🌨️"),
            ("Heavy Snow", "大雪", "❄️"),
            ("Fog", "雾", "🌫️"),
            ("Windy", "大风", "💨"),
            ("Hail", "冰雹", "🧊"),
        ]
    
    def get_weather(self, city: str) -> dict:
        """获取城市天气信息
        
        Args:
            city: 城市名称（中英文均可）
            
        Returns:
            天气信息字典
        """
        city_lower = city.lower()
        city_info = self._city_data.get(city_lower, self._city_data.get("北京"))
        
        condition, condition_cn, emoji = random.choice(self._weather_conditions)
        
        temp = random.randint(5, 35)
        humidity = random.randint(30, 90)
        wind_speed = random.randint(2, 20)
        
        recommendations = self._generate_recommendations(condition, condition_cn, temp)
        
        return {
            "city": city,
            "country": city_info["country"],
            "temperature": temp,
            "temperature_f": round(temp * 9 / 5 + 32),
            "humidity": humidity,
            "wind_speed_kmh": wind_speed,
            "condition": condition,
            "condition_cn": condition_cn,
            "emoji": emoji,
            "recommendations": recommendations,
            "updated_at": datetime.now().isoformat(),
        }
    
    def _generate_recommendations(self, condition: str, condition_cn: str, temp: int) -> list[str]:
        """根据天气状况生成建议"""
        recommendations = []
        
        if temp < 0:
            recommendations.append("天气寒冷，建议穿羽绒服或棉服")
        elif temp < 10:
            recommendations.append("注意保暖，建议穿厚外套")
        elif temp < 20:
            recommendations.append("天气舒适，建议穿轻薄外套")
        elif temp < 28:
            recommendations.append("天气温暖，适宜户外活动")
        elif temp < 32:
            recommendations.append("天气较热，注意防晒降温")
        else:
            recommendations.append("高温预警，注意防暑，多喝水")
        
        if "Rain" in condition or "雨" in condition_cn:
            recommendations.append("记得带伞，防范降雨")
        elif "Snow" in condition or "雪" in condition_cn:
            recommendations.append("注意路面湿滑，防寒保暖")
        elif "Thunderstorm" in condition or "雷" in condition_cn:
            recommendations.append("雷雨天气，避免外出")
        elif "Fog" in condition or "雾" in condition_cn:
            recommendations.append("有雾天气，出行注意安全")
        elif "Windy" in condition or "大风" in condition_cn:
            recommendations.append("大风天气，注意高空坠物")
        
        return recommendations
    
    def get_supported_cities(self) -> list[dict]:
        """获取支持的城市列表"""
        return [
            {"city": city, "country": info["country"]}
            for city, info in self._city_data.items()
        ]
    
    def is_supported_city(self, city: str) -> bool:
        """检查是否支持该城市"""
        return city.lower() in self._city_data
