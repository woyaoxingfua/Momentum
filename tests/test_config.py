from momentum_agent.config import DEFAULT_MODEL, load_provider_config


def test_provider_config_uses_local_fallback_without_key(monkeypatch) -> None:
    for name in (
        "MOMENTUM_API_KEY",
        "MOMENTUM_BASE_URL",
        "MOMENTUM_MODEL",
        "MOMENTUM_DISABLE_TRACING",
        "MOMENTUM_THINKING",
        "MOMENTUM_REASONING_EFFORT",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "OPENAI_THINKING",
        "OPENAI_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_provider_config()

    assert config.api_key is None
    assert config.base_url is None
    assert config.model == DEFAULT_MODEL
    assert not config.disable_tracing
    assert config.thinking is None
    assert config.reasoning_effort is None


def test_momentum_provider_config_takes_precedence(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai-compatible.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "openai-model")
    monkeypatch.setenv("MOMENTUM_API_KEY", "momentum-key")
    monkeypatch.setenv("MOMENTUM_BASE_URL", "https://custom.example/v1")
    monkeypatch.setenv("MOMENTUM_MODEL", "custom-model")

    config = load_provider_config()

    assert config.api_key == "momentum-key"
    assert config.base_url == "https://custom.example/v1"
    assert config.model == "custom-model"
    assert config.disable_tracing


def test_disable_tracing_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("MOMENTUM_API_KEY", "momentum-key")
    monkeypatch.setenv("MOMENTUM_BASE_URL", "https://custom.example/v1")
    monkeypatch.setenv("MOMENTUM_DISABLE_TRACING", "false")

    config = load_provider_config()

    assert not config.disable_tracing


def test_deepseek_thinking_config(monkeypatch) -> None:
    monkeypatch.setenv("MOMENTUM_API_KEY", "momentum-key")
    monkeypatch.setenv("MOMENTUM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("MOMENTUM_THINKING", "enabled")
    monkeypatch.setenv("MOMENTUM_REASONING_EFFORT", "max")

    config = load_provider_config()

    assert config.model == "deepseek-v4-flash"
    assert config.thinking == "enabled"
    assert config.reasoning_effort == "max"
