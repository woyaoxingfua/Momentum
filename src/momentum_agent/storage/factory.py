"""存储后端工厂 — 根据 DATABASE_URL 创建对应后端。"""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from .mysql import MySQLTaskStore
from .sqlite import SQLiteTaskStore


def sqlite_path_from_url(database_url: str) -> str:
    """把 sqlite:// URL（或裸路径）解析成 SQLite 真正能用的文件路径。

    单独抽出来是为了能在不碰文件系统的前提下测试这些平台差异：
      - sqlite:///C:/dir/db   -> C:/dir/db      Windows 盘符
      - sqlite:///\\tmp\\db    -> \\tmp\\db       Windows 当前盘根目录；直接交给 Path
                              会变成 UNC 路径而失败
      - sqlite:////tmp/db     -> //tmp/db       POSIX 四斜杠写法，保持原样
      - sqlite:///:memory:    -> :memory:
    """
    parsed = urlparse(database_url)
    scheme = parsed.scheme.lower()
    # Windows 绝对路径如 C:\\Users\\... 会被 urlparse 解析成 scheme='c'
    if len(scheme) == 1 and scheme.isalpha() and database_url[1:2] == ":":
        scheme = ""
    if scheme not in ("sqlite", ""):
        raise ValueError(f"不是 SQLite URL: {database_url}")

    path = parsed.path
    if scheme == "sqlite" and path:
        stripped = path.lstrip("/")
        if stripped == ":memory:":
            return ":memory:"
        if re.match(r"^[A-Za-z]:[/\\]", stripped):
            return stripped
        if os.name == "nt" and stripped.startswith("\\"):
            return stripped
        return path
    return database_url

def create_task_store(database_url: str | None = None) -> SQLiteTaskStore | MySQLTaskStore:
    """根据数据库 URL 创建存储后端。

    支持的 URL 格式：
      - sqlite:///absolute/path/to/db.db  (本地 SQLite 数据库，默认)
      - sqlite:///:memory:                  (内存数据库，临时使用)
      - mysql://user:password@host:port/db  (MySQL 数据库)
      - azure://user:password@host:port/db  (Azure MySQL 数据库，自动启用 SSL)

    Azure MySQL URL 示例：
      azure://admin@servername.mysql.database.azure.com:3306/momentum

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
        return SQLiteTaskStore(sqlite_path_from_url(database_url))

    if scheme == "mysql":
        return MySQLTaskStore(database_url)

    if scheme == "azure":
        azure_url = f"mysql://{database_url[len('azure://'):]}?ssl=true"
        return MySQLTaskStore(azure_url)

    raise ValueError(f"不支持的数据库 URL: {database_url}")
