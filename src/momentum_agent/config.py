from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .logger import get_logger

log = get_logger("config")

DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_USER_ID = "default"


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

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def provider_label(self) -> str:
        return self.base_url or "OpenAI default endpoint"


def load_provider_config(user_config: dict[str, str] | None = None) -> ProviderConfig:
    load_dotenv()
    disable_tracing = parse_optional_bool(first_env("MOMENTUM_DISABLE_TRACING"))
    if disable_tracing is None:
        disable_tracing = bool(first_env("MOMENTUM_BASE_URL", "OPENAI_BASE_URL"))

    # 优先从用户配置读取，其次从环境变量读取
    api_key = None
    base_url = None
    model = None
    
    if user_config:
        api_key = user_config.get("api_key")
        base_url = user_config.get("api_base")
        model = user_config.get("model")
    
    # 如果用户配置没有，则从环境变量读取
    if not api_key:
        api_key = first_env("MOMENTUM_API_KEY", "OPENAI_API_KEY")
    if not base_url:
        base_url = first_env("MOMENTUM_BASE_URL", "OPENAI_BASE_URL")
    if not model:
        model = first_env("MOMENTUM_MODEL", "OPENAI_MODEL") or DEFAULT_MODEL

    config = ProviderConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        disable_tracing=disable_tracing,
        thinking=normalize_thinking(first_env("MOMENTUM_THINKING", "OPENAI_THINKING")),
        reasoning_effort=normalize_reasoning_effort(
            first_env("MOMENTUM_REASONING_EFFORT", "OPENAI_REASONING_EFFORT")
        ),
    )
    log.info(
        "provider loaded: configured=%s model=%s base_url=%s",
        config.is_configured,
        config.model,
        config.provider_label,
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
