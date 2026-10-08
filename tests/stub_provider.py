"""本地假 provider（OpenAI Chat Completions 线格式），供多个测试复用。

离线、无凭据、可进 CI。任何测试只要依赖 stub_provider，就不会打到真实 API。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _StubHandler(BaseHTTPRequestHandler):
    """实现 POST /v1/chat/completions：JSON 与 SSE 两种形态，按请求序号取脚本。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {}
        server = self.server  # type: ignore[assignment]
        server.requests.append(body)
        index = len(server.requests) - 1
        script = server.script or [{"text": ""}]
        turn = script[min(index, len(script) - 1)]

        if turn.get("status"):
            self._send_error(turn)
            return
        if turn.get("abort"):
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        if body.get("stream"):
            self._send_stream(turn)
        else:
            self._send_json(turn)

    # ── 响应构造 ───────────────────────────────────────────────

    def _tool_calls(self, turn):
        tool = turn.get("tool") or {}
        return [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": tool.get("name", "create_task"),
                "arguments": json.dumps(tool.get("arguments", {}), ensure_ascii=False),
            },
        }]

    def _send_error(self, turn):
        """按脚本返回 provider 侧错误（用于验证客户端的错误分类与提示）。"""
        status = int(turn.get("status", 400))
        message = turn.get("error_message", "bad request")
        payload = {
            "error": {
                "message": message,
                "type": turn.get("error_type", "invalid_request_error"),
                "code": turn.get("error_code"),
            }
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, turn):
        text = turn.get("text")
        message = {"role": "assistant", "content": text}
        finish = "stop"
        if turn.get("tool"):
            message["content"] = text
            message["tool_calls"] = self._tool_calls(turn)
            finish = "tool_calls"
        payload = {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "created": 0,
            "model": "stub-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _frame(self, delta, finish=None):
        payload = {
            "id": "chatcmpl-stub",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "stub-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

    def _send_stream(self, turn):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(self._frame({"role": "assistant", "content": ""}))
        if turn.get("tool"):
            calls = self._tool_calls(turn)
            self.wfile.write(self._frame({"tool_calls": [
                {"index": 0, "id": calls[0]["id"], "type": "function", "function": calls[0]["function"]},
            ]}))
            self.wfile.write(self._frame({}, finish="tool_calls"))
        else:
            text = turn.get("text") or ""
            delay = float(turn.get("delay") or 0)
            for piece in turn.get("pieces") or [text[i:i + 3] for i in range(0, len(text), 3)]:
                self.wfile.write(self._frame({"content": piece}))
                self.wfile.flush()
                if delay:
                    time.sleep(delay)
            self.wfile.write(self._frame({}, finish="stop"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


@pytest.fixture
def stub_provider():
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _StubHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.script = [{"text": "默认回复"}]  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(server=server, base_url=f"http://127.0.0.1:{port}/v1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


