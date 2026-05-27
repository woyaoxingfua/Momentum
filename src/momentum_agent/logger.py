from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_root_initialized = False


def setup_logging(*, level: int = logging.INFO, log_file: str | Path | None = None) -> logging.Logger:
    global _root_initialized

    root = logging.getLogger("momentum")
    if _root_initialized:
        return root

    root.setLevel(level)

    if not root.handlers:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root.addHandler(file_handler)

    _root_initialized = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"momentum.{name}")


def init_from_env() -> None:
    level_name = os.environ.get("MOMENTUM_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = os.environ.get("MOMENTUM_LOG_FILE")
    setup_logging(level=level, log_file=log_file)
    get_logger("logger").debug("logging initialized level=%s file=%s", level_name, log_file or "none")
