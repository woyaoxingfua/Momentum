"""
Weather Service - 独立的天气查询服务

支持 50+ 个国内外城市，提供天气查询和位置信息功能

使用方式：
    from momentum_agent.services.weather import WeatherService
    service = WeatherService()
    data = service.get_weather("上海")
    print(f"{data['city']}: {data['temperature']}°C, {data['condition_cn']}")
"""
import random
from datetime import datetime
from typing import Optional


class WeatherService:
    """天气查询服务 - 支持全球50+城市"""
    
    def __init__(self):
        # 中国城市
        self._city_data = {
            # 一线城市
            "北京": {"lat": 39.9042, "lon": 116.4074, "country": "中国", "region": "华北"},
            "上海": {"lat": 31.2304, "lon": 121.4737, "country": "中国", "region": "华东"},
            "广州": {"lat": 23.1291, "lon": 113.2644, "country": "中国", "region": "华南"},
            "深圳": {"lat": 22.5431, "lon": 114.0579, "country": "中国", "region": "华南"},
            
            # 新一线城市
            "成都": {"lat": 30.5728, "lon": 104.0668, "country": "中国", "region": "西南"},
            "杭州": {"lat": 30.2741, "lon": 120.1551, "country": "中国", "region": "华东"},
            "武汉": {"lat": 30.5928, "lon": 114.3055, "country": "中国", "region": "华中"},
            "西安": {"lat": 34.3416, "lon": 108.9398, "country": "中国", "region": "西北"},
            "南京": {"lat": 32.0603, "lon": 118.7969, "country": "中国", "region": "华东"},
            "重庆": {"lat": 29.4316, "lon": 106.9123, "country": "中国", "region": "西南"},
            "天津": {"lat": 39.3434, "lon": 117.3616, "country": "中国", "region": "华北"},
            "苏州": {"lat": 31.2989, "lon": 120.5853, "country": "中国", "region": "华东"},
            "长沙": {"lat": 28.2282, "lon": 112.9388, "country": "中国", "region": "华中"},
            "郑州": {"lat": 34.7466, "lon": 113.6253, "country": "中国", "region": "华中"},
            "青岛": {"lat": 36.0671, "lon": 120.3826, "country": "中氮", "region": "华东"},
            "沈阳": {"lat": 41.8057, "lon": 123.4328, "country": "中国", "region": "东北"},
            "大连": {"lat": 38.9140, "lon": 121.6147, "country": "中国", "region": "东北"},
            "厦门": {"lat": 24.4798, "lon": 118.0894, "country": "中国", "region": "华南"},
            "哈尔滨": {"lat": 45.8038, "lon": 126.5340, "country": "中国", "region": "东北"},
            "昆明": {"lat": 25.0406, "lon": 102.7129, "country": "中国", "region": "西南"},
            
            # 二线城市
            "太原": {"lat": 37.8706, "lon": 112.5489, "country": "中国", "region": "华北"},
            "合肥": {"lat": 31.8206, "lon": 117.2272, "country": "中国", "region": "华东"},
            "济南": {"lat": 36.6512, "lon": 116.6870, "country": "中国", "region": "华东"},
            "福州": {"lat": 26.0745, "lon": 119.2965, "country": "中国", "region": "华南"},
            "石家庄": {"lat": 38.0428, "lon": 114.5149, "country": "中国", "region": "华北"},
            "南昌": {"lat": 28.6829, "lon": 115.8579, "country": "中国", "region": "华东"},
            "贵阳": {"lat": 26.6470, "lon": 106.6302, "country": "中国", "region": "西南"},
            "南宁": {"lat": 22.8170, "lon": 108.3665, "country": "中国", "region": "华南"},
            "长春": {"lat": 43.8171, "lon": 125.3235, "country": "中国", "region": "东北"},
            "兰州": {"lat": 36.0611, "lon": 103.8343, "country": "中国", "region": "西北"},
            "海口": {"lat": 20.0444, "lon": 110.1999, "country": "中国", "region": "华南"},
            "乌鲁木齐": {"lat": 43.8256, "lon": 87.6168, "country": "中国", "region": "西北"},
            "银川": {"lat": 38.4680, "lon": 106.2731, "country": "中国", "region": "西北"},
            "西宁": {"lat": 36.6171, "lon": 101.7782, "country": "中国", "region": "西北"},
            "拉萨": {"lat": 29.6500, "lon": 91.1000, "country": "中国", "region": "西南"},
            "呼和浩特": {"lat": 40.8424, "lon": 111.7490, "country": "中国", "region": "华北"},
            "香港": {"lat": 22.3193, "lon": 114.1694, "country": "中国", "region": "华南"},
            "澳门": {"lat": 22.1987, "lon": 113.5439, "country": "中国", "region": "华南"},
            "台北": {"lat": 25.0330, "lon": 121.5654, "country": "中国", "region": "华东"},
            
            # 日本
            "东京": {"lat": 35.6762, "lon": 139.6503, "country": "日本", "region": "关东"},
            "大阪": {"lat": 34.6937, "lon": 135.5023, "country": "日本", "region": "关西"},
            "京都": {"lat": 35.0116, "lon": 135.7681, "country": "日本", "region": "关西"},
            "横滨": {"lat": 35.4437, "lon": 139.6380, "country": "日本", "region": "关东"},
            "名古屋": {"lat": 35.1815, "lon": 136.9066, "country": "日本", "region": "中部"},
            
            # 美国
            "纽约": {"lat": 40.7128, "lon": -74.0060, "country": "美国", "region": "东北"},
            "洛杉矶": {"lat": 34.0522, "lon": -118.2437, "country": "美国", "region": "西南"},
            "旧金山": {"lat": 37.7749, "lon": -122.4194, "country": "美国", "region": "西南"},
            "芝加哥": {"lat": 41.8781, "lon": -87.6298, "country": "美国", "region": "中西部"},
            "西雅图": {"lat": 47.6062, "lon": -122.3321, "country": "美国", "region": "西北"},
            "波士顿": {"lat": 42.3601, "lon": -71.0589, "country": "美国", "region": "东北"},
            "迈阿密": {"lat": 25.7617, "lon": -80.1918, "country": "美国", "region": "东南"},
            "华盛顿": {"lat": 38.9072, "lon": -77.0369, "country": "美国", "region": "东北"},
            
            # 英国
            "伦敦": {"lat": 51.5074, "lon": -0.1278, "country": "英国", "region": "英格兰"},
            "曼彻斯特": {"lat": 53.4808, "lon": -2.2426, "country": "英国", "region": "英格兰"},
            "爱丁堡": {"lat": 55.9533, "lon": -3.1883, "country": "英国", "region": "苏格兰"},
            
            # 法国
            "巴黎": {"lat": 48.8566, "lon": 2.3522, "country": "法国", "region": "法兰西岛"},
            "马赛": {"lat": 43.2965, "lon": 5.3698, "country": "法国", "region": "普罗旺斯"},
            "里昂": {"lat": 45.7640, "lon": 4.8357, "country": "法国", "region": "奥弗涅"},
            
            # 德国
            "柏林": {"lat": 52.5200, "lon": 13.4050, "country": "德国", "region": "柏林州"},
            "慕尼黑": {"lat": 48.1351, "lon": 11.5820, "country": "德国", "region": "拜仁"},
            "汉堡": {"lat": 53.5511, "lon": 9.9937, "country": "德国", "region": "汉堡州"},
            "法兰克福": {"lat": 50.1109, "lon": 8.6821, "country": "德国", "region": "黑森"},
            
            # 意大利
            "罗马": {"lat": 41.9028, "lon": 12.4964, "country": "意大利", "region": "拉齐奥"},
            "米兰": {"lat": 45.4642, "lon": 9.1900, "country": "意大利", "region": "伦巴第"},
            "威尼斯": {"lat": 45.4408, "lon": 12.3155, "country": "意大利", "region": "威尼托"},
            "佛罗伦萨": {"lat": 43.7696, "lon": 11.2558, "country": "意大利", "region": "托斯卡纳"},
            
            # 西班牙
            "马德里": {"lat": 40.4168, "lon": -3.7038, "country": "西班牙", "region": "马德里"},
            "巴塞罗那": {"lat": 41.3851, "lon": 2.1734, "country": "西班牙", "region": "加泰罗尼亚"},
            "巴伦西亚": {"lat": 39.4699, "lon": -0.3763, "country": "西班牙", "region": "巴伦西亚"},
            
            # 新加坡
            "新加坡": {"lat": 1.3521, "lon": 103.8198, "country": "新加坡", "region": "新加坡"},
            
            # 澳大利亚
            "悉尼": {"lat": -33.8688, "lon": 151.2093, "country": "澳大利亚", "region": "新南威尔士"},
            "墨尔本": {"lat": -37.8136, "lon": 144.9631, "country": "澳大利亚", "region": "维多利亚"},
            "布里斯班": {"lat": -27.4698, "lon": 153.0251, "country": "澳大利亚", "region": "昆士兰"},
            
            # 加拿大
            "多伦多": {"lat": 43.6532, "lon": -79.3832, "country": "加拿大", "region": "安大略"},
            "温哥华": {"lat": 49.2827, "lon": -123.1207, "country": "加拿大", "region": "不列颠哥伦比亚"},
            "蒙特利尔": {"lat": 45.5017, "lon": -73.5673, "country": "加拿大", "region": "魁北克"},
            
            # 韩国
            "首尔": {"lat": 37.5665, "lon": 126.9780, "country": "韩国", "region": "首尔"},
            "釜山": {"lat": 35.1796, "lon": 129.0756, "country": "韩国", "region": "庆尚南道"},
            
            # 泰国
            "曼谷": {"lat": 13.7563, "lon": 100.5018, "country": "泰国", "region": "中部"},
            "清迈": {"lat": 18.7883, "lon": 98.9853, "country": "泰国", "region": "北部"},
            
            # 阿联酋
            "迪拜": {"lat": 25.2048, "lon": 55.2708, "country": "阿联酋", "region": "迪拜"},
            "阿布扎比": {"lat": 24.4539, "lon": 54.3773, "country": "阿联酋", "region": "阿布扎比"},
            
            # 印度
            "孟买": {"lat": 19.0760, "lon": 72.8777, "country": "印度", "region": "马哈拉施特拉"},
            "新德里": {"lat": 28.6139, "lon": 77.2090, "country": "印度", "region": "德里"},
            "班加罗尔": {"lat": 12.9716, "lon": 77.5946, "country": "印度", "region": "卡纳塔克"},
            
            # 俄罗斯
            "莫斯科": {"lat": 55.7558, "lon": 37.6173, "country": "俄罗斯", "region": "中央联邦区"},
            "圣彼得堡": {"lat": 59.9311, "lon": 30.3609, "country": "俄罗斯", "region": "西北联邦区"},
        }
        
        self._conditions = [
            ("Clear", "晴朗", "☀️", "适合户外活动"),
            ("Partly Cloudy", "多云", "⛅", "适合外出"),
            ("Cloudy", "阴天", "☁️", "注意保暖"),
            ("Light Rain", "小雨", "🌦️", "记得带伞"),
            ("Rain", "中雨", "🌧️", "注意防滑"),
            ("Heavy Rain", "大雨", "⛈️", "建议室内活动"),
            ("Thunderstorm", "雷阵雨", "⛈️", "避免外出"),
            ("Snow", "小雪", "🌨️", "注意保暖"),
            ("Heavy Snow", "大雪", "❄️", "出行小心"),
            ("Fog", "雾", "🌫️", "注意安全"),
            ("Windy", "大风", "💨", "注意防风"),
            ("Hot", "高温", "🔥", "注意防暑"),
            ("Cold", "低温", "🥶", "注意保暖"),
        ]
        
        # 别名映射
        self._aliases = {
            "beijing": "北京",
            "shanghai": "上海",
            "guangzhou": "广州",
            "shenzhen": "深圳",
            "chengdu": "成都",
            "hangzhou": "杭州",
            "wuhan": "武汉",
            "xian": "西安",
            "nanjing": "南京",
            "chongqing": "重庆",
            "tokyo": "东京",
            "osaka": "大阪",
            "new york": "纽约",
            "london": "伦敦",
            "paris": "巴黎",
            "singapore": "新加坡",
        }
    
    def get_weather(self, city: str) -> dict:
        """获取城市天气信息
        
        Args:
            city: 城市名称（中英文均可，支持别名）
            
        Returns:
            天气信息字典
        """
        city_lower = city.lower()
        
        # 检查别名
        if city_lower in self._aliases:
            city = self._aliases[city_lower]
            city_lower = city.lower()
        
        # 获取城市信息
        info = self._city_data.get(city_lower, self._city_data["北京"])
        
        # 随机天气
        condition, condition_cn, emoji, suggestion = random.choice(self._conditions)
        
        # 根据城市纬度调整温度
        base_temp = 15 - (info["lat"] * 0.3)
        temp = base_temp + random.randint(-5, 15)
        
        humidity = random.randint(30, 90)
        wind_speed = random.randint(0, 25)
        
        # 生成建议
        recommendations = self._generate_recommendations(condition, temp, humidity)
        
        return {
            "city": city,
            "country": info["country"],
            "region": info["region"],
            "temperature": temp,
            "temperature_f": round(temp * 9 / 5 + 32),
            "humidity": humidity,
            "wind_speed_kmh": wind_speed,
            "condition": condition,
            "condition_cn": condition_cn,
            "emoji": emoji,
            "recommendations": recommendations,
            "map_url": self._generate_map_url(info["lat"], info["lon"]),
            "updated_at": datetime.now().isoformat(),
        }
    
    def _generate_recommendations(self, condition: str, temp: int, humidity: int) -> list[str]:
        """根据天气状况生成建议"""
        recommendations = []
        
        # 温度建议
        if temp < 0:
            recommendations.append("天气寒冷，建议穿羽绒服或棉服")
        elif temp < 10:
            recommendations.append("注意保暖，建议穿厚外套")
        elif temp < 18:
            recommendations.append("天气较凉，建议穿外套")
        elif temp < 25:
            recommendations.append("天气舒适，适宜户外活动")
        elif temp < 30:
            recommendations.append("天气温暖，注意防晒")
        else:
            recommendations.append("高温预警，注意防暑降温，多喝水")
        
        # 湿度建议
        if humidity > 80:
            recommendations.append("空气潮湿，注意防潮")
        elif humidity < 30:
            recommendations.append("空气干燥，注意保湿")
        
        # 天气状况建议
        if "Rain" in condition or "雨" in condition:
            recommendations.append("记得带伞，防范降雨")
        elif "Snow" in condition or "雪" in condition:
            recommendations.append("注意路面湿滑，防寒保暖")
        elif "Thunderstorm" in condition or "雷" in condition:
            recommendations.append("雷雨天气，避免外出")
        elif "Fog" in condition or "雾" in condition:
            recommendations.append("有雾天气，出行注意安全")
        elif "Windy" in condition or "风" in condition:
            recommendations.append("大风天气，注意高空坠物")
        
        return recommendations[:3]  # 最多返回3条建议
    
    def _generate_map_url(self, lat: float, lon: float) -> dict:
        """生成地图链接"""
        return {
            "openstreetmap": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=12/{lat}/{lon}",
            "google": f"https://www.google.com/maps?q={lat},{lon}",
            "baidu": f"https://api.map.baidu.com/geocoder?location={lat},{lon}&coord_type=gcj02ll",
        }
    
    def is_supported(self, city: str) -> bool:
        """检查是否支持该城市"""
        city_lower = city.lower()
        if city_lower in self._aliases:
            return True
        return city_lower in self._city_data
    
    def get_supported_cities(self, country: str = None) -> list[dict]:
        """获取支持的城市列表
        
        Args:
            country: 可选，按国家筛选
            
        Returns:
            城市列表
        """
        cities = []
        for city_name, info in self._city_data.items():
            if country is None or info["country"] == country:
                cities.append({
                    "city": city_name,
                    "country": info["country"],
                    "region": info["region"],
                })
        return sorted(cities, key=lambda x: (x["country"], x["city"]))
    
    def get_countries(self) -> list[str]:
        """获取支持的国家列表"""
        countries = set(info["country"] for info in self._city_data.values())
        return sorted(list(countries))


# 便捷函数
def get_weather(city: str = "北京") -> dict:
    """便捷函数：获取天气"""
    return WeatherService().get_weather(city)


def get_location(city: str = "北京") -> dict:
    """便捷函数：获取位置信息"""
    service = WeatherService()
    info = service._city_data.get(city.lower(), service._city_data["北京"])
    return {
        "city": city,
        "country": info["country"],
        "region": info["region"],
        "latitude": info["lat"],
        "longitude": info["lon"],
        "maps": service._generate_map_url(info["lat"], info["lon"]),
    }


def is_supported_city(city: str) -> bool:
    """便捷函数：检查是否支持该城市"""
    return WeatherService().is_supported(city)


def get_supported_cities(country: str = None) -> list[dict]:
    """便捷函数：获取支持的城市列表"""
    return WeatherService().get_supported_cities(country)
