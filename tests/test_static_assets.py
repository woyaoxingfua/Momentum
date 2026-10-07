from pathlib import Path
from unittest.mock import Mock

from momentum_agent.web.server import MomentumHandler


def test_background_module_is_served_as_javascript(monkeypatch):
    from momentum_agent.web import handlers

    static_root = Path(__file__).parents[1] / "src" / "momentum_agent" / "static"
    assert (static_root / "js" / "background.mjs").is_file()

    captured = {}
    monkeypatch.setattr(
        handlers,
        "send_static",
        lambda _handler, filename, content_type: captured.update(
            filename=filename, content_type=content_type
        ),
    )

    class FakeRouteHandler:
        STATIC_MAP = MomentumHandler.STATIC_MAP

    assert MomentumHandler._send_static_or_none(FakeRouteHandler(), "/js/background.mjs")
    assert captured == {
        "filename": "js/background.mjs",
        "content_type": "text/javascript; charset=utf-8",
    }


def test_static_script_route_still_rejects_non_script_traversal():
    class FakeRouteHandler:
        STATIC_MAP = MomentumHandler.STATIC_MAP

    assert not MomentumHandler._send_static_or_none(FakeRouteHandler(), "/js/../secrets.txt")
    assert not MomentumHandler._send_static_or_none(FakeRouteHandler(), "/js/payload.json")
