from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .logger import get_logger

log = get_logger("config")

DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_USER_ID = "default"
OLLAMA_DEFAULT_BASE = "http://localhost:11434/v1"


def get_current_user() -> str:
    return os.environ.get("MOMENTUM_USER", DEFAULT_USER_ID)


@dataclass(frozen=True)
class ProviderConfig:
    api_key: str | None
    base_url: str | None
    model: str
    disable_tracing: bool
    thinking: str | None
    reasoning_effort: str | None
    provider: str

    @property
    def is_configured(self) -> bool:
        if self.provider == "ollama":
            return bool(self.base_url)
        return bool(self.api_key)

    @property
    def is_ollama(self) -> bool:
        return self.provider == "ollama"

    @property
    def provider_label(self) -> str:
        if self.is_ollama:
            return "Ollama"
        return self.base_url or "OpenAI default endpoint"


def _looks_like_ollama(base_url: str | None, api_key: str | None, model: str | None, provider: str | None) -> bool:
    if provider == "ollama":
        return True
    if api_key and api_key.strip().lower() == "ollama":
        return True
    if base_url:
        lowered = base_url.lower()
        if "ollama" in lowered:
            return True
        if re.search(r"(localhost|127\.0\.0\.1):11434", lowered):
            return True
    if model and model.lower().startswith("ollama/"):
        return True
    return False


def _normalize_ollama_base(base_url: str | None) -> str:
    if not base_url:
        return OLLAMA_DEFAULT_BASE
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


def load_provider_config(user_config: dict[str, str] | None = None) -> ProviderConfig:
    project_root = Path(__file__).resolve().parent.parent.parent
    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(dotenv_path=str(env_file))
    disable_tracing = parse_optional_bool(first_env("MOMENTUM_DISABLE_TRACING"))
    if disable_tracing is None:
        disable_tracing = bool(first_env("MOMENTUM_BASE_URL", "OPENAI_BASE_URL"))

    api_key = None
    base_url = None
    model = None
    provider = None

    if user_config:
        api_key = user_config.get("api_key")
        base_url = user_config.get("api_base")
        model = user_config.get("model")
        provider = user_config.get("provider")

    if not api_key:
        api_key = first_env("MOMENTUM_API_KEY", "OPENAI_API_KEY")
    if not base_url:
        base_url = first_env("MOMENTUM_BASE_URL", "OPENAI_BASE_URL")
    if not model:
        model = first_env("MOMENTUM_MODEL", "OPENAI_MODEL") or DEFAULT_MODEL
    if not provider:
        provider = os.environ.get("MOMENTUM_PROVIDER")

    is_ollama = _looks_like_ollama(base_url, api_key, model, provider)
    provider = "ollama" if is_ollama else (provider or "openai")

    if is_ollama:
        base_url = _normalize_ollama_base(base_url)
        if not api_key:
            api_key = "ollama"
        if model.lower().startswith("ollama/"):
            model = model[len("ollama/"):]

    config = ProviderConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        disable_tracing=True if is_ollama else disable_tracing,
        thinking=normalize_thinking(first_env("MOMENTUM_THINKING", "OPENAI_THINKING")),
        reasoning_effort=normalize_reasoning_effort(
            first_env("MOMENTUM_REASONING_EFFORT", "OPENAI_REASONING_EFFORT")
        ),
        provider=provider,
    )
    log.info(
        "provider loaded: provider=%s configured=%s model=%s base_url=%s",
        config.provider,
        config.is_configured,
        config.model,
        config.base_url,
    )
    return config


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def parse_optional_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_thinking(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled", "enable"}:
        return "enabled"
    if normalized in {"0", "false", "no", "off", "disabled", "disable"}:
        return "disabled"
    return normalized


def normalize_reasoning_effort(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower()
