"""MCP 真实传输集成测试：Streamable HTTP 的鉴权门禁与 JSON-RPC 握手。

之前只有对 create_mcp_server / 鉴权函数的单元测试（全 mock），没有验证过
「真的用 CLI 起一个 HTTP 端点，然后按 MCP 协议握手」这条路径。
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="MCP 依赖未安装（pip install -e .[mcp]）")
pytest.importorskip("uvicorn", reason="MCP HTTP 传输需要 uvicorn")

ROOT = Path(__file__).resolve().parents[1]
API_KEY = "mcp-http-test-" + secrets.token_hex(8)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_env(key: str | None) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("MOMENTUM_DATABASE_URL", None)
    env["MOMENTUM_API_KEY"] = ""
    env["OPENAI_API_KEY"] = ""
    if key is None:
        env.pop("MOMENTUM_MCP_API_KEY", None)
    else:
        env["MOMENTUM_MCP_API_KEY"] = key
    return env


def start_mcp(db: Path, port: int, *, host: str = "127.0.0.1", key: str | None = None, transport: str = "streamable-http"):
    command = [sys.executable, "-m", "momentum_agent", "--db", f"sqlite:///{db}",
               "mcp", "--transport", transport, "--host", host, "--port", str(port)]
    return subprocess.Popen(command, cwd=ROOT, env=server_env(key),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def wait_for_port(port: int, process: subprocess.Popen, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("mcp server exited early: " + process.stdout.read().decode("utf-8", "replace")[-500:])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("mcp server did not start listening")


def parse_payload(content_type: str, raw: str):
    """Streamable HTTP 可能回 JSON，也可能回 SSE；两种都解出 JSON-RPC 结果。"""
    if "text/event-stream" in content_type:
        for line in raw.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def rpc(port: int, method: str, params: dict | None = None, *, key: str | None = None, session: str | None = None, request_id: int = 1):
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    if session:
        headers["mcp-session-id"] = session
    request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, dict(response.headers), parse_payload(response.headers.get("Content-Type", ""), raw)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        return error.code, dict(error.headers), parse_payload(error.headers.get("Content-Type", ""), raw)


@pytest.fixture
def secured(tmp_path):
    db = tmp_path / "mcp-http.sqlite3"
    port = free_port()
    process = start_mcp(db, port, key=API_KEY)
    try:
        wait_for_port(port, process)
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_missing_bearer_is_rejected(secured):
    status, _headers, payload = rpc(secured, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}})
    assert status == 401
    assert payload and "API Key" in json.dumps(payload, ensure_ascii=False)


def test_wrong_bearer_is_rejected(secured):
    status, _headers, payload = rpc(secured, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}, key="definitely-wrong")
    assert status == 401
    assert payload


def test_initialize_and_tools_list_with_valid_key(secured):
    status, headers, payload = rpc(secured, "initialize",
                                   {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "pytest", "version": "1"}},
                                   key=API_KEY)
    assert status == 200, payload
    assert payload and payload.get("result", {}).get("serverInfo"), payload
    session = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")

    status, _headers, listed = rpc(secured, "tools/list", {}, key=API_KEY, session=session, request_id=2)
    assert status == 200, listed
    tools = [tool["name"] for tool in (listed or {}).get("result", {}).get("tools", [])]
    assert tools, listed
    assert "create_task" in tools
    assert len(tools) >= 20, f"expected the full tool registry, got {len(tools)}"


def test_tools_call_creates_a_task(tmp_path):
    db = tmp_path / "mcp-call.sqlite3"
    port = free_port()
    process = start_mcp(db, port, key=API_KEY)
    try:
        wait_for_port(port, process)
        status, headers, payload = rpc(port, "initialize",
                                       {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "pytest", "version": "1"}}, key=API_KEY)
        assert status == 200 and payload
        session = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        status, _headers, called = rpc(port, "tools/call",
                                       {"name": "create_task", "arguments": {"title": "MCP 建立的任务"}},
                                       key=API_KEY, session=session, request_id=2)
        assert status == 200, called
        text = json.dumps(called, ensure_ascii=False)
        assert "MCP 建立的任务" in text, text[:400]

        import sqlite3

        with sqlite3.connect(db) as connection:
            titles = [row[0] for row in connection.execute("SELECT title FROM tasks")]
        assert "MCP 建立的任务" in titles, titles
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_loopback_without_key_needs_no_bearer(tmp_path):
    db = tmp_path / "mcp-open.sqlite3"
    port = free_port()
    process = start_mcp(db, port, key=None)
    try:
        wait_for_port(port, process)
        status, _headers, payload = rpc(port, "initialize",
                                        {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "pytest", "version": "1"}})
        assert status == 200, payload
        assert payload and payload.get("result"), payload
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_non_loopback_without_key_refuses_to_start(tmp_path):
    db = tmp_path / "mcp-refuse.sqlite3"
    port = free_port()
    process = start_mcp(db, port, host="0.0.0.0", key=None)
    try:
        output = process.communicate(timeout=25)[0].decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        process.kill()
        pytest.fail("MCP 在没有 API Key 时仍然接受了非 loopback 监听")
    assert process.returncode != 0, output
    assert "API Key" in output or "拒绝启动" in output, output[-400:]

