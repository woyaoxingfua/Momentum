from __future__ import annotations

import contextlib
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_FILE_FORMAT = (
    "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s | "
    "pid=%(process)d tid=%(thread)d | %(funcName)s:%(lineno)d | "
    "req=%(request_id)s | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_DEFAULT_LOG_DIR = "logs"
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_BACKUPS = 5

_GLOBAL_REQUEST_ID = ""
_root_initialized = False


def set_request_id(request_id: str | None = None) -> str:
    global _GLOBAL_REQUEST_ID
    _GLOBAL_REQUEST_ID = request_id or uuid.uuid4().hex[:12]
    return _GLOBAL_REQUEST_ID


def clear_request_id() -> None:
    global _GLOBAL_REQUEST_ID
    _GLOBAL_REQUEST_ID = ""


@contextlib.contextmanager
def request_context(request_id: str | None = None):
    rid = set_request_id(request_id)
    try:
        yield rid
    finally:
        clear_request_id()


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _GLOBAL_REQUEST_ID or "-"  # type: ignore[attr-defined]
        return True


def setup_logging(
    *,
    level: int = logging.INFO,
    log_file: str | Path | None = None,
    log_dir: str | Path | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUPS,
    enable_console: bool = True,
    enable_file: bool | None = None,
) -> logging.Logger:
    global _root_initialized

    root = logging.getLogger("momentum")
    if _root_initialized:
        return root

    root.setLevel(logging.DEBUG)
    req_filter = _RequestIdFilter()

    if enable_console and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    ):
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT))
        console.addFilter(req_filter)
        root.addHandler(console)

    resolved_file: Path | None = None
    if log_file:
        resolved_file = Path(log_file)
    elif log_dir or enable_file is True or enable_file is None:
        dir_path = Path(log_dir) if log_dir else Path(_DEFAULT_LOG_DIR)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resolved_file = dir_path / f"momentum-{today}.log"

    if resolved_file:
        resolved_file.parent.mkdir(parents=True, exist_ok=True)
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
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, _DATE_FORMAT))
            file_handler.addFilter(req_filter)
            root.addHandler(file_handler)

    _root_initialized = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"momentum.{name}")


def init_from_env() -> None:
    level_name = os.environ.get("MOMENTUM_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = os.environ.get("MOMENTUM_LOG_FILE")
    log_dir = os.environ.get("MOMENTUM_LOG_DIR")
    max_bytes = int(os.environ.get("MOMENTUM_LOG_MAX_BYTES", _DEFAULT_MAX_BYTES))
    backup_count = int(os.environ.get("MOMENTUM_LOG_BACKUPS", _DEFAULT_BACKUPS))

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


def log_api_request(method: str, path: str, status: int, duration_ms: float) -> None:
    logger = get_logger("api")
    level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
    logger.log(level, "%s %s → %d (%.0fms)", method, path, status, duration_ms)


def log_security_event(event: str, user_id: str = "-", detail: str = "") -> None:
    logger = get_logger("security")
    msg = f"event={event} user={user_id}"
    if detail:
        msg += f" detail={detail}"
    logger.warning(msg)


def log_db_query(sql: str, duration_ms: float | None = None, rows: int | None = None) -> None:
    logger = get_logger("db")
    msg = f"sql={sql.strip()[:200]}"
    if duration_ms is not None:
        msg += f" duration={duration_ms:.1f}ms"
    if rows is not None:
        msg += f" rows={rows}"
    logger.debug(msg)
