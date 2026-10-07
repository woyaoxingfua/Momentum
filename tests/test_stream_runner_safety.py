import asyncio
from types import SimpleNamespace

from agents.stream_events import RunItemStreamEvent

from momentum_agent import agent_app
from momentum_agent.config import ProviderConfig


class _Store:
    def get_all_memory(self, *, user_id):
        return {}


class _StreamResult:
    is_complete = False

    def __init__(self, stream_factory):
        self._stream_factory = stream_factory

    async def stream_events(self):
        async for event in self._stream_factory():
            yield event


def _install_fake_runner(
    monkeypatch,
    stream_factory,
    *,
    counters=None,
    replay_write_on_fallback=False,
):
    import agents

    counters = counters if counters is not None else {"fallback_runs": 0, "write_effects": 0}
    provider = ProviderConfig(
        api_key="test-key",
        base_url="https://compatible.example/v1",
        model="test-model",
        disable_tracing=True,
        thinking=None,
        reasoning_effort=None,
        provider="openai",
    )

    class _Runner:
        @staticmethod
        def run_streamed(*args, **kwargs):
            return _StreamResult(stream_factory)

        @staticmethod
        async def run(*args, **kwargs):
            counters["fallback_runs"] += 1
            if replay_write_on_fallback:
                counters["write_effects"] += 1
            return SimpleNamespace(final_output="fallback response", to_input_list=lambda: [])

    async def _guardrail():
        return object()

    monkeypatch.setattr(agents, "Runner", _Runner)
    monkeypatch.setattr(agents, "set_default_openai_client", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_app, "create_task_store", lambda _url: _Store())
    monkeypatch.setattr(agent_app, "load_provider_config", lambda _config: provider)
    monkeypatch.setattr(agent_app, "build_openai_client", lambda _provider: object())
    monkeypatch.setattr(agent_app, "_build_agent", lambda *args, **kwargs: object())
    monkeypatch.setattr(agent_app, "_build_input_guardrail", _guardrail)
    monkeypatch.setattr(agent_app, "_build_output_guardrail", _guardrail)
    monkeypatch.setattr(agent_app, "_build_run_config", lambda *args, **kwargs: object())
    monkeypatch.setattr(agent_app, "_make_hooks", lambda: object())
    monkeypatch.setattr(agent_app, "_get_history", lambda _user_id: [])
    saved_history = []
    monkeypatch.setattr(agent_app, "_save_history", lambda _user_id, history: saved_history.append(history))
    return counters, saved_history


def _collect_events():
    async def _collect():
        return [
            event
            async for event in agent_app.run_agent_message_stream(
                "sqlite:///unused-test-db",
                "mark the task done",
                user_id="stream-test",
            )
        ]

    return asyncio.run(_collect())


def test_stream_failure_after_write_tool_never_replays_side_effect(monkeypatch):
    counters = {"fallback_runs": 0, "write_effects": 0}

    async def stream_factory():
        yield RunItemStreamEvent(
            name="tool_called", item=SimpleNamespace(tool_name="mark_task_done")
        )
        # A controllable stand-in for a write tool that has committed its effect.
        counters["write_effects"] += 1
        yield RunItemStreamEvent(
            name="tool_output", item=SimpleNamespace(tool_name="mark_task_done")
        )
        raise RuntimeError("injected failure after write")

    counters, saved_history = _install_fake_runner(
        monkeypatch,
        stream_factory,
        counters=counters,
        replay_write_on_fallback=True,
    )

    events = _collect_events()

    assert counters["write_effects"] == 1
    assert counters["fallback_runs"] == 0
    assert [event["type"] for event in events] == ["tool_start", "tool_end", "error", "done"]
    assert "没有自动重跑" in events[-2]["message"]
    assert saved_history == []


def test_stream_failure_before_any_tool_is_an_explicit_error_without_fallback(monkeypatch):
    async def stream_factory():
        if False:  # Keep this a well-formed async generator without emitting a tool event.
            yield None
        raise RuntimeError("injected failure before tools")

    counters, saved_history = _install_fake_runner(monkeypatch, stream_factory)

    events = _collect_events()

    assert counters["write_effects"] == 0
    assert counters["fallback_runs"] == 0
    assert [event["type"] for event in events] == ["error", "done"]
    assert "没有自动重跑" in events[0]["message"]
    assert saved_history == []
