import asyncio
import importlib
import sys
from contextlib import asynccontextmanager

import pytest

from momentum_agent import mcp_server


async def _noop_runner(*_args, **_kwargs):
    return None


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.24", "100.117.187.9", "mcp.example.net"])
def test_remote_http_without_key_is_rejected(monkeypatch, transport, host):
    monkeypatch.delenv("MOMENTUM_MCP_API_KEY", raising=False)
    with pytest.raises(ValueError, match="MOMENTUM_MCP_API_KEY"):
        mcp_server.validate_mcp_transport_security(transport, host)


@pytest.mark.parametrize("transport,runner_name", [
    ("streamable-http", "run_streamable_http"),
    ("sse", "run_sse"),
])
def test_remote_startup_rejects_before_transport_or_database(monkeypatch, transport, runner_name):
    monkeypatch.delenv("MOMENTUM_MCP_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(mcp_server, runner_name, _noop_runner)
    monkeypatch.setattr(mcp_server, "create_task_store", lambda *_args, **_kwargs: calls.append("store"))

    with pytest.raises(ValueError, match="MOMENTUM_MCP_API_KEY"):
        mcp_server.run_mcp_server("sqlite:///must-not-be-opened.db", transport=transport, host="0.0.0.0")
    assert calls == []


@pytest.mark.parametrize("transport,runner_name", [
    ("streamable-http", "run_streamable_http"),
    ("sse", "run_sse"),
])
@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST."])
def test_loopback_hosts_allow_http_without_key(monkeypatch, transport, runner_name, host):
    monkeypatch.delenv("MOMENTUM_MCP_API_KEY", raising=False)
    monkeypatch.setattr(mcp_server, runner_name, _noop_runner)

    mcp_server.run_mcp_server("sqlite:///not-opened.db", transport=transport, host=host)


@pytest.mark.parametrize("transport,runner_name", [
    ("streamable-http", "run_streamable_http"),
    ("sse", "run_sse"),
])
def test_remote_http_with_key_passes_startup_gate(monkeypatch, transport, runner_name):
    monkeypatch.setenv("MOMENTUM_MCP_API_KEY", "test-key")
    monkeypatch.setattr(mcp_server, runner_name, _noop_runner)

    mcp_server.run_mcp_server("sqlite:///not-opened.db", transport=transport, host="0.0.0.0")


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_api_key_does_not_authorize_remote_host(monkeypatch, value):
    monkeypatch.setenv("MOMENTUM_MCP_API_KEY", value)
    with pytest.raises(ValueError, match="MOMENTUM_MCP_API_KEY"):
        mcp_server.validate_mcp_transport_security("sse", "::")


def test_stdio_remains_available_without_key(monkeypatch):
    monkeypatch.delenv("MOMENTUM_MCP_API_KEY", raising=False)
    monkeypatch.setattr(mcp_server, "run_stdio", _noop_runner)
    mcp_server.run_mcp_server("sqlite:///not-opened.db", transport="stdio", host="0.0.0.0")


@pytest.mark.parametrize("module_name", ["momentum_agent.cli", "momentum_agent.__main__"])
def test_cli_refuses_remote_before_opening_database(monkeypatch, capsys, module_name):
    module = importlib.import_module(module_name)
    monkeypatch.setattr(sys, "argv", ["momentum-agent", "mcp", "--transport", "streamable-http", "--host", "0.0.0.0"])
    monkeypatch.setattr(module, "init_from_env", lambda: None)
    monkeypatch.delenv("MOMENTUM_MCP_API_KEY", raising=False)
    store_calls = []
    monkeypatch.setattr(module, "create_task_store", lambda *_args, **_kwargs: store_calls.append("opened"))

    with pytest.raises(SystemExit) as error:
        module.main()

    assert error.value.code == 2
    assert "MOMENTUM_MCP_API_KEY" in capsys.readouterr().err
    assert store_calls == []


def test_bearer_comparison_uses_hmac_compare_digest(monkeypatch):
    original = mcp_server.hmac.compare_digest
    calls = []

    def compare(left, right):
        calls.append(True)
        return original(left, right)

    monkeypatch.setattr(mcp_server.hmac, "compare_digest", compare)
    assert mcp_server._check_api_key("test-key", expected="test-key")
    assert not mcp_server._check_api_key("wrong-key", expected="test-key")
    assert not mcp_server._check_api_key("无效密钥", expected="test-key")
    assert len(calls) == 3


def test_sse_requires_same_bearer_key_for_connection_and_messages(monkeypatch):
    import mcp.server.sse as sse_module
    from starlette.responses import Response
    from starlette.testclient import TestClient

    events = []

    class FakeSseTransport:
        def __init__(self, endpoint):
            assert endpoint == "/messages/"

        @asynccontextmanager
        async def connect_sse(self, *_args, **_kwargs):
            events.append("connect")
            yield object(), object()

        async def handle_post_message(self, scope, receive, send):
            events.append("message")
            await Response(status_code=202)(scope, receive, send)

    class FakeServer:
        def create_initialization_options(self):
            return object()

        async def run(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(sse_module, "SseServerTransport", FakeSseTransport)
    app = mcp_server.create_sse_app(FakeServer(), api_key="test-key")
    with TestClient(app) as client:
        denied_connect = client.get("/sse")
        denied_message = client.post("/messages/", json={"jsonrpc": "2.0"})
        assert denied_connect.status_code == 401
        assert denied_message.status_code == 401
        invalid_headers = {"Authorization": "Bearer wrong-key"}
        assert client.get("/sse", headers=invalid_headers).status_code == 401
        assert client.post("/messages/", headers=invalid_headers, json={"jsonrpc": "2.0"}).status_code == 401
        assert events == []

        headers = {"Authorization": "Bearer test-key"}
        connected = client.get("/sse", headers=headers)
        posted = client.post("/messages/", headers=headers, json={"jsonrpc": "2.0"})
        assert connected.status_code == 200
        assert posted.status_code == 202
        assert events == ["connect", "message"]


def test_streamable_direct_runner_rejects_before_opening_database(monkeypatch):
    monkeypatch.delenv("MOMENTUM_MCP_API_KEY", raising=False)
    store_calls = []
    monkeypatch.setattr(mcp_server, "create_task_store", lambda *_args, **_kwargs: store_calls.append("opened"))

    with pytest.raises(ValueError, match="MOMENTUM_MCP_API_KEY"):
        asyncio.run(mcp_server.run_streamable_http("sqlite:///must-not-be-opened.db", host="::"))
    assert store_calls == []


def test_sse_direct_runner_rejects_before_opening_database(monkeypatch):
    monkeypatch.delenv("MOMENTUM_MCP_API_KEY", raising=False)
    store_calls = []
    monkeypatch.setattr(mcp_server, "create_task_store", lambda *_args, **_kwargs: store_calls.append("opened"))

    with pytest.raises(ValueError, match="MOMENTUM_MCP_API_KEY"):
        asyncio.run(mcp_server.run_sse("sqlite:///must-not-be-opened.db", host="0.0.0.0"))
    assert store_calls == []
