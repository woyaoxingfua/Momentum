import sys


def test_cli_accepts_streamable_http_transport(monkeypatch):
    from momentum_agent import cli, mcp_server, wizard_config

    calls = []
    monkeypatch.setattr(sys, "argv", ["momentum-agent", "mcp", "--transport", "streamable-http"])
    monkeypatch.setattr(cli, "create_task_store", lambda _url: object())
    monkeypatch.setattr(cli, "get_current_user", lambda: "default")
    monkeypatch.setattr(mcp_server, "run_mcp_server", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(wizard_config, "get_mcp_host", lambda host: host or "127.0.0.1")
    monkeypatch.setattr(wizard_config, "get_mcp_port", lambda port: port or 8766)

    cli.main()

    assert calls
    assert calls[0][1]["transport"] == "streamable-http"
