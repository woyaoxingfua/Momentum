"""用本地假 provider 驱动真实 Agents SDK 的能力矩阵（离线，可进 CI）。

归档 review/Momentum_REVIEW_archive_20261007.md 的「优先级建议」第 2、3 条指出：
  - 需要验证「工具已执行后流失败不会重放副作用」；
  - provider 能力矩阵（文本/工具/流式/视觉/handoff）因为需要凭据一直没有做。
既有测试把 Runner 整个 mock 掉了，所以真正的 SDK 路径从未被跑过。

这里起一个实现了 OpenAI Chat Completions 线格式的本地 stub，让真实的 Runner/Agent/工具
执行链跑起来，从而离线覆盖上述能力。
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from momentum_agent import agent_app
from momentum_agent.storage import SQLiteTaskStore


HANDOFF_TO_INSIGHT = "transfer_to_insightagent"  # SDK 的 function-style 命名规则


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
            for piece in turn.get("pieces") or [text[i:i + 3] for i in range(0, len(text), 3)]:
                self.wfile.write(self._frame({"content": piece}))
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


@pytest.fixture
def real_agent(monkeypatch, stub_provider, tmp_path):
    monkeypatch.setenv("MOMENTUM_API_KEY", "stub-key")
    # 先清掉一切可能指向真实 provider 的变量（仓库根的 .env 也会被 load_dotenv 读入），
    # 再显式指向本地 stub；否则测试会真的打到外部 API。
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "MOMENTUM_BASE_URL",
                 "MOMENTUM_MODEL", "MOMENTUM_PROVIDER", "MOMENTUM_THINKING",
                 "MOMENTUM_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOMENTUM_API_KEY", "stub-key")
    monkeypatch.setenv("MOMENTUM_BASE_URL", stub_provider.base_url)
    monkeypatch.setenv("MOMENTUM_MODEL", "stub-model")
    monkeypatch.setenv("MOMENTUM_DISABLE_TRACING", "true")
    monkeypatch.setenv("MOMENTUM_PROVIDER", "openai")

    # 硬门禁：解析出来的 base_url 必须是本地 stub，绝不允许漏到真实服务。
    from momentum_agent.config import load_provider_config

    resolved = load_provider_config({})
    assert resolved.base_url and resolved.base_url.startswith(stub_provider.base_url), (
        "provider 指向了非本地 stub，拒绝继续：" + str(resolved.base_url)
    )
    database_url = f"sqlite:///{tmp_path / 'agent.sqlite3'}"
    user_id = "stub-user"
    agent_app.clear_conversation_history(user_id)
    yield SimpleNamespace(
        database_url=database_url,
        user_id=user_id,
        server=stub_provider.server,
        base_url=stub_provider.base_url,
    )
    agent_app.clear_conversation_history(user_id)


def collect_stream(coro_factory):
    async def _run():
        events = []
        async for event in coro_factory():
            events.append(event)
        return events

    return asyncio.run(_run())


def task_titles(database_url: str, user_id: str = "default") -> list[str]:
    path = database_url.replace("sqlite:///", "")
    import sqlite3

    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute("SELECT title FROM tasks WHERE user_id = ?", (user_id,))]


# ── 1. 纯文本 ────────────────────────────────────────────────────

def test_text_reply_round_trip(real_agent):
    real_agent.server.script = [{"text": "这是 stub 的回复"}]
    reply = asyncio.run(agent_app.run_agent_message(real_agent.database_url, "你好", user_id=real_agent.user_id))
    assert reply == "这是 stub 的回复"
    assert len(real_agent.server.requests) == 1
    assert real_agent.server.requests[0]["model"] == "stub-model"


# ── 2. 工具调用真的落到数据库 ────────────────────────────────────

def test_tool_call_is_executed_against_the_real_store(real_agent):
    real_agent.server.script = [
        {"tool": {"name": "create_task", "arguments": {"title": "假 provider 建的任务"}}},
        {"text": "已经帮你建好了"},
    ]
    reply = asyncio.run(agent_app.run_agent_message(real_agent.database_url, "帮我建个任务", user_id=real_agent.user_id))
    assert reply == "已经帮你建好了"
    assert "假 provider 建的任务" in task_titles(real_agent.database_url, real_agent.user_id)
    assert len(real_agent.server.requests) == 2, "SDK 必须带着工具结果再来一轮"
    tool_messages = [m for m in real_agent.server.requests[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages, "第二轮请求里必须包含工具执行结果"


# ── 3. 真流式 ────────────────────────────────────────────────────

def test_streaming_delivers_incremental_chunks(real_agent):
    real_agent.server.script = [{"text": "流式回复内容", "pieces": ["流式", "回复", "内容"]}]
    events = collect_stream(lambda: agent_app.run_agent_message_stream(
        real_agent.database_url, "说点什么", user_id=real_agent.user_id))
    text = "".join(event.get("text", "") for event in events if event.get("type") == "chunk")
    assert text == "流式回复内容"
    assert events[-1] == {"type": "done"}
    assert sum(1 for event in events if event.get("type") == "chunk") >= 2, "必须是增量而不是一次性"


# ── 4. 视觉请求的线格式 ──────────────────────────────────────────

def test_vision_request_uses_openai_image_data_uri(real_agent):
    real_agent.server.script = [{"text": "我看到了一张图"}]
    asyncio.run(agent_app.run_agent_message(
        real_agent.database_url, "看看这张图", image_base64="QUJD", user_id=real_agent.user_id))
    content = real_agent.server.requests[0]["messages"][-1]["content"]
    assert isinstance(content, list), content
    image_parts = [part for part in content if part.get("type") == "image_url"]
    assert image_parts, content
    assert image_parts[0]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"


# ── 5. 工具执行后流失败：不重放副作用（归档优先级 2） ────────────

def test_stream_failure_after_a_write_does_not_replay_it(real_agent):
    real_agent.server.script = [
        {"tool": {"name": "create_task", "arguments": {"title": "只应被建一次"}}},
        {"abort": True},
    ]
    events = collect_stream(lambda: agent_app.run_agent_message_stream(
        real_agent.database_url, "建一个任务", user_id=real_agent.user_id))

    kinds = [event.get("type") for event in events]
    assert "error" in kinds, events
    assert "done" in kinds, "fail-closed 也要给前端一个结束信号：" + json.dumps(events, ensure_ascii=False)
    titles = task_titles(real_agent.database_url, real_agent.user_id)
    assert titles.count("只应被建一次") == 1, f"写副作用必须只发生一次：{titles}"
    # 说明：openai 客户端自身会对断线做有限重试（会重发请求），
    # 真正要证明的是「写副作用没有被重放」——即上面的任务数必须恰好为 1；
    # 若是应用层自动重跑，工具会再执行一次、任务数变成 2。
    assert len(real_agent.server.requests) <= 6, "重试次数异常多，像是应用层重跑"


# ── 6. handoff 真的转到专家 Agent ────────────────────────────────

def test_handoff_routes_to_the_specialist_agent(real_agent):
    real_agent.server.script = [
        {"tool": {"name": HANDOFF_TO_INSIGHT, "arguments": {}}},
        {"text": "洞察结果"},
    ]
    reply = asyncio.run(agent_app.run_agent_message(
        real_agent.database_url, "这周完成率怎么样", user_id=real_agent.user_id))
    assert reply == "洞察结果"
    assert len(real_agent.server.requests) == 2
    second = json.dumps(real_agent.server.requests[1], ensure_ascii=False)
    assert "洞察分析专家" in second, "handoff 之后必须由 InsightAgent 的指令接管"

