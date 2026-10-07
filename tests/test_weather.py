from urllib.parse import parse_qs, urlparse

import pytest

from momentum_agent.services import weather


@pytest.fixture(autouse=True)
def clear_weather_caches():
    weather._weather_cache.clear()
    weather._geocoding_cache.clear()
    yield
    weather._weather_cache.clear()
    weather._geocoding_cache.clear()


def test_get_weather_uses_open_meteo_current_conditions(monkeypatch):
    requested = []

    def fake_fetch(url):
        requested.append(url)
        return {
            "current": {
                "time": "2026-10-03T14:00",
                "temperature_2m": 22.6,
                "relative_humidity_2m": 64,
                "weather_code": 61,
                "precipitation": 0.2,
                "wind_speed_10m": 12.0,
            }
        }

    monkeypatch.setattr(weather, "_fetch_json", fake_fetch)
    result = weather.get_weather("北京")

    assert result["city"] == "北京"
    assert result["temperature"] == 22.6
    assert result["humidity"] == 64
    assert result["condition"] == "Rain"
    assert result["condition_cn"] == "小雨"
    assert "注意降水，外出可准备雨具" in result["tips"]
    assert result["source"] == "Open-Meteo"
    assert result["updated_at"] == "2026-10-03T14:00"

    query = parse_qs(urlparse(requested[0]).query)
    assert urlparse(requested[0]).netloc == "api.open-meteo.com"
    assert query["current"] == [
        "temperature_2m,relative_humidity_2m,weather_code,precipitation,wind_speed_10m"
    ]
    assert query["timezone"] == ["auto"]


def test_weather_response_is_cached_for_same_coordinates(monkeypatch):
    calls = 0

    def fake_fetch(_url):
        nonlocal calls
        calls += 1
        return {"current": {
            "time": "2026-10-03T14:00", "temperature_2m": 20,
            "relative_humidity_2m": 50, "weather_code": 0,
        }}

    monkeypatch.setattr(weather, "_fetch_json", fake_fetch)
    first = weather.get_weather("北京")
    second = weather.get_weather("beijing")

    assert calls == 1
    assert first == second


def test_unlisted_city_uses_open_meteo_geocoding(monkeypatch):
    requested = []

    def fake_fetch(url):
        requested.append(url)
        if "geocoding-api.open-meteo.com" in url:
            return {"results": [{
                "name": "奥克兰", "latitude": -36.85, "longitude": 174.76,
                "country": "新西兰",
            }]}
        return {"current": {
            "time": "2026-10-03T14:00", "temperature_2m": 16.2,
            "relative_humidity_2m": 70, "weather_code": 2,
        }}

    monkeypatch.setattr(weather, "_fetch_json", fake_fetch)
    result = weather.get_weather("奥克兰")

    assert result["city"] == "奥克兰"
    assert result["country"] == "新西兰"
    assert result["latitude"] == -36.85
    assert len(requested) == 2
    geocoding_query = parse_qs(urlparse(requested[0]).query)
    assert geocoding_query["name"] == ["奥克兰"]
    assert geocoding_query["language"] == ["zh"]


def test_unknown_city_raises_instead_of_silently_using_beijing(monkeypatch):
    monkeypatch.setattr(weather, "_fetch_json", lambda _url: {"results": []})

    with pytest.raises(weather.CityNotFoundError, match="没有找到城市"):
        weather.get_location("不存在的城市")


def test_weather_network_failure_is_reported_not_fabricated(monkeypatch):
    def fail(_url):
        raise weather.WeatherServiceError("offline")

    monkeypatch.setattr(weather, "_fetch_json", fail)

    with pytest.raises(weather.WeatherServiceError, match="offline"):
        weather.get_weather("北京")


def test_search_cities_uses_open_meteo_geocoding_and_limits_result_count(monkeypatch):
    requested = []

    def fake_fetch(url):
        requested.append(url)
        return {"results": [{
            "name": "Paris", "country": "France", "admin1": "Île-de-France",
            "latitude": 48.86, "longitude": 2.35,
        }]}

    monkeypatch.setattr(weather, "_fetch_json", fake_fetch)
    results = weather.search_cities("paris", count=500)

    assert results == [{
        "name": "Paris", "country": "France", "admin1": "Île-de-France",
        "latitude": 48.86, "longitude": 2.35,
    }]
    assert urlparse(requested[0]).netloc == "geocoding-api.open-meteo.com"
    query = parse_qs(urlparse(requested[0]).query)
    assert query["name"] == ["paris"]
    assert query["count"] == ["10"]
    assert query["language"] == ["zh"]


def test_search_cities_rejects_overlong_queries():
    with pytest.raises(weather.CityNotFoundError, match="不能超过 100"):
        weather.search_cities("x" * 101)
