"""
天气服务模块 - Weather Service Module
提供天气查询和位置相关的功能
"""
from .weather import WeatherService
from .location import LocationService

__all__ = ['WeatherService', 'LocationService']
