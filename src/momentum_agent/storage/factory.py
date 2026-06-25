"""存储后端工厂 — 根据 DATABASE_URL 创建对应后端。"""
from __future__ import annotations

import os
from urllib.parse import urlparse

from .mysql import MySQLTaskStore
from .sqlite import SQLiteTaskStore


def create_task_store(database_url: str | None = None) -> SQLiteTaskStore | MySQLTaskStore:
    """根据数据库 URL 创建存储后端。

    支持的 URL 格式：
      - sqlite:///absolute/path/to/db.db
      - sqlite:///:memory:
      - mysql://user:password@host:port/db

    未提供 URL 时默认使用环境变量 MOMENTUM_DATABASE_URL，
    否则回退到项目目录下的 .momentum/tasks.db。
    """
    if database_url is None:
        database_url = os.environ.get("MOMENTUM_DATABASE_URL", ".momentum/tasks.db")

    parsed = urlparse(database_url)
    scheme = parsed.scheme.lower()

    # Windows 绝对路径如 C:\Users\... 会被 urlparse 解析成 scheme='c'
    if len(scheme) == 1 and scheme.isalpha() and database_url[1:2] == ":":
        scheme = ""

    if scheme in ("sqlite", ""):
        # sqlite:///path 或裸路径都走 SQLite
        path = parsed.path
        if scheme == "sqlite" and path:
            # urlparse('sqlite:///:memory:').path == '/:memory:'
            db_path = ":memory:" if path.lstrip("/") == ":memory:" else path
        else:
            db_path = database_url
        return SQLiteTaskStore(db_path)

    if scheme == "mysql":
        return MySQLTaskStore(database_url)

    raise ValueError(f"不支持的数据库 URL: {database_url}")
