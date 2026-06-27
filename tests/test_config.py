from unittest.mock import patch

from momentum_agent.config import DEFAULT_MODEL, OLLAMA_DEFAULT_BASE, load_provider_config


def _clear_env(monkeypatch) -> None:
    for name in (
        "MOMENTUM_API_KEY",
        "MOMENTUM_BASE_URL",
        "MOMENTUM_MODEL",
        "MOMENTUM_DISABLE_TRACING",
        "MOMENTUM_THINKING",
        "MOMENTUM_REASONING_EFFORT",
        "MOMENTUM_PROVIDER",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "OPENAI_THINKING",
        "OPENAI_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_provider_config_uses_local_fallback_without_key(monkeypatch) -> None:
    _clear_env(monkeypatch)

    with patch("momentum_agent.config.load_dotenv"):
        config = load_provider_config()

    assert config.api_key is None
    assert config.base_url is None
    assert config.model == DEFAULT_MODEL
    assert not config.disable_tracing
    assert config.thinking is None
    assert config.reasoning_effort is None
    assert config.provider == "openai"


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
    assert config.provider == "openai"
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


def test_ollama_provider_from_user_config(monkeypatch) -> None:
    _clear_env(monkeypatch)

    with patch("momentum_agent.config.load_dotenv"):
        config = load_provider_config({"provider": "ollama", "model": "llama3.2"})

    assert config.provider == "ollama"
    assert config.base_url == OLLAMA_DEFAULT_BASE
    assert config.api_key == "ollama"
    assert config.model == "llama3.2"
    assert config.is_configured


def test_ollama_provider_detected_by_base_url(monkeypatch) -> None:
    _clear_env(monkeypatch)

    with patch("momentum_agent.config.load_dotenv"):
        config = load_provider_config({"api_base": "http://localhost:11434", "model": "qwen2.5"})

    assert config.provider == "ollama"
    assert config.base_url == "http://localhost:11434/v1"
    assert config.api_key == "ollama"


def test_ollama_provider_detected_by_api_key(monkeypatch) -> None:
    _clear_env(monkeypatch)

    with patch("momentum_agent.config.load_dotenv"):
        config = load_provider_config({"api_key": "ollama", "model": "phi4"})

    assert config.provider == "ollama"
    assert config.base_url == OLLAMA_DEFAULT_BASE


def test_ollama_model_prefix_stripped(monkeypatch) -> None:
    _clear_env(monkeypatch)

    with patch("momentum_agent.config.load_dotenv"):
        config = load_provider_config({"provider": "ollama", "model": "ollama/llama3.2"})

    assert config.model == "llama3.2"
