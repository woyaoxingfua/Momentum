"""
位置服务模块 - Location Service Module
提供位置查询和地图链接功能
"""
from typing import Optional


class LocationService:
    """位置查询服务类"""
    
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
            "tokyo": {"lat": 35.6762, "lon": 139.6503, "country": "日本"},
            "osaka": {"lat": 34.6937, "lon": 135.5023, "country": "日本"},
            "new york": {"lat": 40.7128, "lon": -74.0060, "country": "美国"},
            "los angeles": {"lat": 34.0522, "lon": -118.2437, "country": "美国"},
            "london": {"lat": 51.5074, "lon": -0.1278, "country": "英国"},
            "paris": {"lat": 48.8566, "lon": 2.3522, "country": "法国"},
            "singapore": {"lat": 1.3521, "lon": 103.8198, "country": "新加坡"},
            "seoul": {"lat": 37.5665, "lon": 126.9780, "country": "韩国"},
            "sydney": {"lat": -33.8688, "lon": 151.2093, "country": "澳大利亚"},
            "toronto": {"lat": 43.6532, "lon": -79.3832, "country": "加拿大"},
            "berlin": {"lat": 52.5200, "lon": 13.4050, "country": "德国"},
            "rome": {"lat": 41.9028, "lon": 12.4964, "country": "意大利"},
            "madrid": {"lat": 40.4168, "lon": -3.7038, "country": "西班牙"},
            "bangkok": {"lat": 13.7563, "lon": 100.5018, "country": "泰国"},
            "dubai": {"lat": 25.2048, "lon": 55.2708, "country": "阿联酋"},
            "mumbai": {"lat": 19.0760, "lon": 72.8777, "country": "印度"},
        }
    
    def get_location_info(self, city: str) -> dict:
        """获取位置信息
        
        Args:
            city: 城市名称
            
        Returns:
            位置信息字典
        """
        city_lower = city.lower()
        info = self._city_data.get(city_lower, self._city_data.get("北京"))
        
        return {
            "city": city,
            "country": info["country"],
            "latitude": info["lat"],
            "longitude": info["lon"],
            "openstreetmap_url": self._generate_openstreetmap_url(info["lat"], info["lon"]),
            "google_maps_url": self._generate_google_maps_url(info["lat"], info["lon"]),
            "bing_maps_url": self._generate_bing_maps_url(info["lat"], info["lon"]),
            "coordinates": f"{info['lat']}, {info['lon']}"
        }
    
    def _generate_openstreetmap_url(self, lat: float, lon: float) -> str:
        """生成 OpenStreetMap URL"""
        return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=12/{lat}/{lon}"
    
    def _generate_google_maps_url(self, lat: float, lon: float) -> str:
        """生成 Google Maps URL"""
        return f"https://www.google.com/maps?q={lat},{lon}"
    
    def _generate_bing_maps_url(self, lat: float, lon: float) -> str:
        """生成 Bing Maps URL"""
        return f"https://www.bing.com/maps?cp={lat}~{lon}&lvl=12"
    
    def calculate_distance(self, city1: str, city2: str) -> Optional[float]:
        """计算两个城市之间的距离（公里）
        
        使用简化的球面距离公式
        
        Args:
            city1: 第一个城市
            city2: 第二个城市
            
        Returns:
            距离（公里），如果城市不支持则返回 None
        """
        import math
        
        info1 = self._city_data.get(city1.lower())
        info2 = self._city_data.get(city2.lower())
        
        if not info1 or not info2:
            return None
        
        lat1, lon1 = math.radians(info1["lat"]), math.radians(info1["lon"])
        lat2, lon2 = math.radians(info2["lat"]), math.radians(info2["lon"])
        
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        c = 2 * math.asin(math.sqrt(a))
        
        # 地球半径（公里）
        r = 6371
        
        return round(c * r, 2)
    
    def get_supported_cities(self) -> list[str]:
        """获取支持的城市列表"""
        return list(self._city_data.keys())
