"""momentum.config.json — 由配置向导生成的运行时配置文件。

存储 web / mcp 子命令的 host/port 等启动级参数，与 .env 互补：
  - .env：存敏感凭据和进程级配置（DB URL、API key、日志参数）
  - momentum.config.json：存非敏感的服务监听地址、端口

回退链（cli.py 的 serve/mcp 子命令会用到）：
    CLI flag (--host/--port)  ← 最高优先级
      ↓
    环境变量 (MOMENTUM_WEB_HOST/PORT, MOMENTUM_MCP_HOST/PORT)
      ↓
    momentum.config.json  ← 本模块管理
      ↓
    硬编码默认 (127.0.0.1:8765 / 127.0.0.1:8766)  ← 最低

文件位置：项目根目录（与 .env 同级），即 cwd 下的 momentum.config.json。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 硬编码默认值（与 cli.py / mcp_server.py 保持一致）
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8765
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8766

CONFIG_FILENAME = "momentum.config.json"


def _config_path() -> Path:
    """配置文件路径：当前工作目录下。"""
    return Path.cwd() / CONFIG_FILENAME


def load_config() -> dict[str, Any]:
    """读取 momentum.config.json，文件不存在或损坏返回空 dict。

    绝不抛异常——配置文件损坏时退化为默认值，不阻塞启动。
    """
    path = _config_path()
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict[str, Any]) -> None:
    """写入 momentum.config.json，格式化易读。

    merge 模式：读取现有 config，合并传入的字段（不覆盖未传入的段）。
    """
    path = _config_path()
    existing = load_config() if path.exists() else {}
    existing.update(config)
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Web 服务 ───────────────────────────────────────


def get_web_host(cli_host: str | None = None) -> str:
    """解析 Web 服务监听地址：CLI flag > env > config.json > 默认。"""
    if cli_host:
        return cli_host
    env = os.environ.get("MOMENTUM_WEB_HOST")
    if env:
        return env
    cfg = load_config()
    return cfg.get("web", {}).get("host") or DEFAULT_WEB_HOST


def get_web_port(cli_port: int | None = None) -> int:
    """解析 Web 服务监听端口：CLI flag > env > config.json > 默认。"""
    if cli_port is not None:
        return cli_port
    env = os.environ.get("MOMENTUM_WEB_PORT")
    if env and env.isdigit():
        return int(env)
    cfg = load_config()
    port = cfg.get("web", {}).get("port")
    if isinstance(port, int):
        return port
    return DEFAULT_WEB_PORT


def set_web_config(host: str, port: int) -> None:
    """更新 momentum.config.json 的 web 段（merge，不覆盖其他段）。"""
    cfg = load_config()
    cfg.setdefault("web", {})
    cfg["web"]["host"] = host
    cfg["web"]["port"] = port
    save_config(cfg)


# ── MCP Server ─────────────────────────────────────


def get_mcp_host(cli_host: str | None = None) -> str:
    """解析 MCP SSE 监听地址：CLI flag > env > config.json > 默认。"""
    if cli_host:
        return cli_host
    env = os.environ.get("MOMENTUM_MCP_HOST")
    if env:
        return env
    cfg = load_config()
    return cfg.get("mcp", {}).get("host") or DEFAULT_MCP_HOST


def get_mcp_port(cli_port: int | None = None) -> int:
    """解析 MCP SSE 监听端口：CLI flag > env > config.json > 默认。"""
    if cli_port is not None:
        return cli_port
    env = os.environ.get("MOMENTUM_MCP_PORT")
    if env and env.isdigit():
        return int(env)
    cfg = load_config()
    port = cfg.get("mcp", {}).get("port")
    if isinstance(port, int):
        return port
    return DEFAULT_MCP_PORT


def set_mcp_config(host: str, port: int) -> None:
    """更新 momentum.config.json 的 mcp 段（merge，不覆盖其他段）。"""
    cfg = load_config()
    cfg.setdefault("mcp", {})
    cfg["mcp"]["host"] = host
    cfg["mcp"]["port"] = port
    save_config(cfg)


__all__ = [
    "load_config",
    "save_config",
    "get_web_host",
    "get_web_port",
    "set_web_config",
    "get_mcp_host",
    "get_mcp_port",
    "set_mcp_config",
    "DEFAULT_WEB_HOST",
    "DEFAULT_WEB_PORT",
    "DEFAULT_MCP_HOST",
    "DEFAULT_MCP_PORT",
    "CONFIG_FILENAME",
]
