from momentum_agent.agent_app import _build_run_config
from momentum_agent.config import ProviderConfig


def _provider(*, disable_tracing: bool) -> ProviderConfig:
    return ProviderConfig(
        api_key="test-key",
        base_url="https://compatible.example/v1",
        model="test-model",
        disable_tracing=disable_tracing,
        thinking=None,
        reasoning_effort=None,
        provider="openai",
    )


def test_run_config_disables_tracing_for_compatible_provider():
    input_guardrail = object()
    output_guardrail = object()

    config = _build_run_config(
        _provider(disable_tracing=True),
        workflow_name="test-compatible",
        input_guardrails=[input_guardrail],
        output_guardrails=[output_guardrail],
    )

    assert config.tracing_disabled is True
    assert config.workflow_name == "test-compatible"
    assert config.input_guardrails == [input_guardrail]
    assert config.output_guardrails == [output_guardrail]


def test_run_config_preserves_tracing_for_openai_provider():
    config = _build_run_config(
        _provider(disable_tracing=False),
        workflow_name="test-openai",
    )

    assert config.tracing_disabled is False
    assert config.workflow_name == "test-openai"
