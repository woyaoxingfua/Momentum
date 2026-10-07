import json
from http import HTTPStatus

import pytest

from momentum_agent.services import weather
from momentum_agent.web import handlers


class MemoryStore:
    def __init__(self):
        self.values = {}

    def set_memory(self, key, value, user_id="default"):
        self.values[(user_id, key)] = value

    def get_memory(self, key, user_id="default"):
        return self.values.get((user_id, key))

    def get_all_memory(self, user_id="default"):
        return {key: value for (owner, key), value in self.values.items() if owner == user_id}


class Handler:
    def __init__(self, payload=None):
        self.store = MemoryStore()
        self.payload = payload or {}
        self.status = HTTPStatus.OK
        self.body = None

    def read_json(self):
        return self.payload

    def send_json(self, payload, status=HTTPStatus.OK):
        self.body = payload
        self.status = status


def test_remote_background_and_builtin_theme_are_saved_as_user_memory_only():
    handler = Handler({
        "theme": "sage",
        "background": {"source": "remote_url", "url": "https://cdn.example.org/paper.jpg", "opacity": "35"},
    })

    handlers.handle_set_preferences(handler, "alice")

    assert handler.status == HTTPStatus.OK
    assert handler.store.get_memory("ui_theme", user_id="alice") == "sage"
    assert handler.store.get_memory("background_source", user_id="alice") == "remote_url"
    assert handler.store.get_memory("background_url", user_id="alice") == "https://cdn.example.org/paper.jpg"
    assert handler.store.get_memory("background_opacity", user_id="alice") == "35"
    assert handler.store.get_memory("background_url", user_id="bob") is None


def test_local_background_persists_only_source_and_empty_url():
    handler = Handler({"background": {"source": "local", "url": "", "opacity": "22"}})

    handlers.handle_set_preferences(handler, "alice")

    assert handler.status == HTTPStatus.OK
    assert handler.store.get_memory("background_source", user_id="alice") == "local"
    assert handler.store.get_memory("background_url", user_id="alice") == ""
    assert handler.store.get_memory("background_opacity", user_id="alice") == "22"


def test_local_image_payload_is_rejected_and_never_stored():
    handler = Handler({"background": {
        "source": "local", "url": "data:image/png;base64,not-an-image", "opacity": "15",
    }})

    handlers.handle_set_preferences(handler, "alice")

    assert handler.status == HTTPStatus.BAD_REQUEST
    assert not handler.store.values


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "data:image/png;base64,abc",
    "https://user:password@cdn.example.org/photo.jpg",
    "http://127.0.0.1/photo.jpg",
    "http://192.168.1.10/photo.jpg",
])
def test_synced_background_rejects_non_public_or_executable_urls(url):
    handler = Handler({"background": {"source": "remote_url", "url": url, "opacity": "15"}})

    handlers.handle_set_preferences(handler, "alice")

    assert handler.status == HTTPStatus.BAD_REQUEST
    assert not handler.store.values


def test_invalid_preferences_do_not_partially_write_theme():
    handler = Handler({
        "theme": "midnight",
        "background": {"source": "remote_url", "url": "file:///tmp/image.png", "opacity": "15"},
    })

    handlers.handle_set_preferences(handler, "alice")

    assert handler.status == HTTPStatus.BAD_REQUEST
    assert not handler.store.values


def test_get_preferences_reports_local_source_without_image_content():
    handler = Handler()
    handler.store.set_memory("background_source", "local", user_id="alice")
    handler.store.set_memory("background_url", "", user_id="alice")
    handler.store.set_memory("background_opacity", "40", user_id="alice")

    handlers.handle_get_preferences(handler, "alice")

    assert handler.body["background"] == {
        "configured": True, "source": "local", "url": "", "opacity": "40",
    }


def test_user_city_and_country_are_persisted_with_coordinates():
    handler = Handler({"city": "Paris", "country": "France", "latitude": 48.86, "longitude": 2.35})

    handlers.handle_set_user_location(handler, "alice")

    assert handler.status == HTTPStatus.OK
    assert handler.store.get_memory("user_location", user_id="alice") == "Paris"
    assert handler.store.get_memory("user_location_country", user_id="alice") == "France"
    assert handler.store.get_memory("user_location_latitude", user_id="alice") == "48.86"
    assert handler.store.get_memory("user_location_longitude", user_id="alice") == "2.35"


def test_invalid_city_coordinate_does_not_partially_save():
    handler = Handler({"city": "Paris", "country": "France", "latitude": 91, "longitude": 2.35})

    handlers.handle_set_user_location(handler, "alice")

    assert handler.status == HTTPStatus.BAD_REQUEST
    assert not handler.store.values


def test_city_search_uses_geocoding_results(monkeypatch):
    handler = Handler()
    monkeypatch.setattr(weather, "search_cities", lambda q: [{
        "name": "Paris", "country": "France", "admin1": "Île-de-France", "latitude": 48.86, "longitude": 2.35,
    }] if q == "paris" else [])

    handlers.handle_search_cities(handler, type("Parsed", (), {"query": "q=paris"})())

    assert handler.status == HTTPStatus.OK
    assert handler.body["cities"][0]["country"] == "France"


def test_existing_user_memory_table_needs_no_destructive_schema_migration(tmp_path):
    from momentum_agent.storage.sqlite import SQLiteTaskStore

    db = tmp_path / "existing.db"
    store = SQLiteTaskStore(db)
    store.set_memory("background_source", "local", user_id="alice")
    store.set_memory("user_location", "Paris", user_id="alice")
    reopened = SQLiteTaskStore(db)

    assert reopened.get_memory("background_source", user_id="alice") == "local"
    assert reopened.get_memory("user_location", user_id="alice") == "Paris"
    assert reopened.get_memory("background_source", user_id="bob") is None
