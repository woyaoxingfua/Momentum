"""存储层 — 可插拔后端。"""
from __future__ import annotations

from .factory import create_task_store
from .mysql import MySQLTaskStore
from .sqlite import (
    DEFAULT_USER,
    SESSION_LIFETIME,
    SQLiteTaskStore,
    decode_dt,
    encode_dt,
    row_to_task,
    row_to_task_relation,
    utcnow,
)

# 保持向后兼容：TaskStore 默认指向 SQLiteTaskStore
TaskStore = SQLiteTaskStore

__all__ = [
    "TaskStore",
    "SQLiteTaskStore",
    "MySQLTaskStore",
    "create_task_store",
    "DEFAULT_USER",
    "SESSION_LIFETIME",
    "utcnow",
    "encode_dt",
    "decode_dt",
    "row_to_task",
    "row_to_task_relation",
]
