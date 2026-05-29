"""Momentum 全功能日志模块

特性：
- 控制台 + 文件双输出，格式分离（控制台简洁、文件详细）
- RotatingFileHandler 自动轮转（默认 10MB × 5 个备份）
- 默认日志目录 logs/，按日期命名文件（momentum-YYYY-MM-DD.log）
- 请求 ID 追踪（Web 请求自动注入 request_id）
- 进程/线程信息记录（文件日志包含 pid + thread）
- 环境变量配置：MOMENTUM_LOG_LEVEL / MOMENTUM_LOG_FILE / MOMENTUM_LOG_DIR / MOMENTUM_LOG_MAX_BYTES / MOMENTUM_LOG_BACKUPS
- CLI 参数：--verbose / --log-file / --log-dir
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── 格式定义 ──────────────────────────────────────────────────────────

# 控制台：简洁，一眼能看
_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
# 文件：详细，含进程/线程/文件位置，方便排查
_FILE_FORMAT = (
    "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s | "
    "pid=%(process)d tid=%(thread)d | %(funcName)s:%(lineno)d | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ── 默认参数 ──────────────────────────────────────────────────────────

_DEFAULT_LOG_DIR = "logs"
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_DEFAULT_BACKUPS = 5
_GLOBAL_REQUEST_ID = ""  # 全局请求 ID（Web 请求时设置）

_root_initialized = False


# ── 请求 ID 追踪 ─────────────────────────────────────────────────────

def set_request_id(request_id: str | None = None) -> str:
    """设置当前请求 ID，Web 请求开始时调用。返回设置的 ID。"""
    global _GLOBAL_REQUEST_ID
    _GLOBAL_REQUEST_ID = request_id or uuid.uuid4().hex[:12]
    return _GLOBAL_REQUEST_ID


def get_request_id() -> str:
    """获取当前请求 ID。"""
    return _GLOBAL_REQUEST_ID


def clear_request_id() -> None:
    """清除请求 ID，Web 请求结束时调用。"""
    global _GLOBAL_REQUEST_ID
    _GLOBAL_REQUEST_ID = ""


@contextlib.contextmanager
def request_context(request_id: str | None = None):
    """请求上下文管理器，自动设置/清除请求 ID。

    用法::

        with request_context():
            log.info("处理请求")  # 日志会自动带上 request_id
    """
    rid = set_request_id(request_id)
    try:
        yield rid
    finally:
        clear_request_id()


# ── 自定义 Filter：注入请求 ID ────────────────────────────────────────

class _RequestIdFilter(logging.Filter):
    """给日志记录注入 request_id 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _GLOBAL_REQUEST_ID or "-"  # type: ignore[attr-defined]
        return True


# ── 自定义 Formatter：文件日志带 request_id ───────────────────────────

class _DetailedFormatter(logging.Formatter):
    """文件专用格式，在详细格式基础上注入 request_id。"""

    _BASE = (
        "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s | "
        "pid=%(process)d tid=%(thread)d | %(funcName)s:%(lineno)d | "
        "req=%(request_id)s | %(message)s"
    )

    def __init__(self) -> None:
        super().__init__(self._BASE, datefmt=_DATE_FORMAT)


# ── 核心设置 ──────────────────────────────────────────────────────────

def setup_logging(
    *,
    level: int = logging.INFO,
    log_file: str | Path | None = None,
    log_dir: str | Path | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUPS,
    enable_console: bool = True,
    enable_file: bool | None = None,  # None = 自动（有 log_file 或 log_dir 就开）
) -> logging.Logger:
    """初始化日志系统。

    Args:
        level: 日志级别（logging.DEBUG/INFO/WARNING/ERROR/CRITICAL）
        log_file: 指定日志文件路径（优先级最高）
        log_dir: 日志目录，默认 ``logs/``；自动按日期命名文件
        max_bytes: 单个日志文件最大字节数，超过自动轮转
        backup_count: 保留的历史日志文件数量
        enable_console: 是否启用控制台输出
        enable_file: 是否启用文件输出；None 表示自动判断
    """
    global _root_initialized

    root = logging.getLogger("momentum")
    if _root_initialized:
        return root

    root.setLevel(logging.DEBUG)  # root 收所有级别，由 handler 各自过滤

    # 请求 ID filter（全局注入）
    req_filter = _RequestIdFilter()

    # ── 控制台 Handler ────────────────────────────────────────────────
    if enable_console and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    ):
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT))
        console.addFilter(req_filter)
        root.addHandler(console)

    # ── 确定 log_file 路径 ────────────────────────────────────────────
    resolved_file: Path | None = None
    if log_file:
        resolved_file = Path(log_file)
    elif log_dir or enable_file is True or enable_file is None:
        # 按日期自动命名；enable_file=True 且未指定路径时也走默认目录
        dir_path = Path(log_dir) if log_dir else Path(_DEFAULT_LOG_DIR)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resolved_file = dir_path / f"momentum-{today}.log"

    # ── 文件 Handler ──────────────────────────────────────────────────
    if resolved_file:
        # 自动创建目录
        resolved_file.parent.mkdir(parents=True, exist_ok=True)

        # 检查是否已有同路径的 RotatingFileHandler
        already_has_file = any(
            isinstance(h, RotatingFileHandler)
            and getattr(h, "baseFilename", None) == str(resolved_file)
            for h in root.handlers
        )

        if not already_has_file:
            file_handler = RotatingFileHandler(
                resolved_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)  # 文件始终记录最全
            file_handler.setFormatter(_DetailedFormatter())
            file_handler.addFilter(req_filter)
            root.addHandler(file_handler)

    _root_initialized = True

    get_logger("logger").info(
        "日志系统初始化: level=%s file=%s console=%s",
        logging.getLevelName(level),
        resolved_file or "无",
        enable_console,
    )
    return root


def get_logger(name: str) -> logging.Logger:
    """获取 momentum 命名空间下的子 logger。

    Args:
        name: 模块名，如 ``"storage"`` → ``momentum.storage``
    """
    return logging.getLogger(f"momentum.{name}")


def init_from_env() -> None:
    """从环境变量初始化日志（CLI 和 Web 入口调用）。

    默认启用文件日志，写入 ``logs/momentum-YYYY-MM-DD.log``。
    可通过 ``MOMENTUM_LOG_FILE=off`` 或 ``MOMENTUM_LOG_DIR=off`` 禁用。
    """
    level_name = os.environ.get("MOMENTUM_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = os.environ.get("MOMENTUM_LOG_FILE")
    log_dir = os.environ.get("MOMENTUM_LOG_DIR")
    max_bytes = int(os.environ.get("MOMENTUM_LOG_MAX_BYTES", _DEFAULT_MAX_BYTES))
    backup_count = int(os.environ.get("MOMENTUM_LOG_BACKUPS", _DEFAULT_BACKUPS))

    # 用户显式设为 "off" 则禁用文件日志
    enable_file = not (
        (log_file and log_file.lower() == "off")
        or (log_dir and log_dir.lower() == "off")
    )

    setup_logging(
        level=level,
        log_file=log_file if log_file and log_file.lower() != "off" else None,
        log_dir=log_dir if log_dir and log_dir.lower() != "off" else None,
        max_bytes=max_bytes,
        backup_count=backup_count,
        enable_file=enable_file,
    )


# ── 便捷函数 ──────────────────────────────────────────────────────────

def log_function_call(func_name: str, *args: object, **kwargs: object) -> None:
    """记录函数调用日志（DEBUG 级别）。"""
    logger = get_logger("call")
    args_str = ", ".join(repr(a) for a in args)
    kwargs_str = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    parts = [s for s in [args_str, kwargs_str] if s]
    logger.debug("%s(%s)", func_name, ", ".join(parts))


def log_performance(operation: str, duration_ms: float, **extra: object) -> None:
    """记录性能日志（耗时操作）。"""
    logger = get_logger("perf")
    extra_str = " ".join(f"{k}={v}" for k, v in extra.items())
    parts = [f"operation={operation}", f"duration={duration_ms:.1f}ms"]
    if extra_str:
        parts.append(extra_str)
    level = logging.WARNING if duration_ms > 5000 else logging.INFO
    logger.log(level, " | ".join(parts))


def log_db_query(sql: str, duration_ms: float | None = None, rows: int | None = None) -> None:
    """记录数据库查询日志。"""
    logger = get_logger("db")
    msg = f"sql={sql.strip()[:200]}"
    if duration_ms is not None:
        msg += f" duration={duration_ms:.1f}ms"
    if rows is not None:
        msg += f" rows={rows}"
    logger.debug(msg)


def log_api_request(method: str, path: str, status: int, duration_ms: float) -> None:
    """记录 API 请求日志。"""
    logger = get_logger("api")
    level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
    logger.log(
        level,
        "%s %s → %d (%.0fms)",
        method,
        path,
        status,
        duration_ms,
    )


def log_security_event(event: str, user_id: str = "-", detail: str = "") -> None:
    """记录安全事件日志（登录失败、权限异常等）。"""
    logger = get_logger("security")
    msg = f"event={event} user={user_id}"
    if detail:
        msg += f" detail={detail}"
    logger.warning(msg)


def log_exception(context: str, exc: BaseException) -> None:
    """记录异常日志（带完整堆栈）。"""
    logger = get_logger("exception")
    logger.error("%s: %s", context, exc, exc_info=True)
