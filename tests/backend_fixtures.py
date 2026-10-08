"""后端参数化的共享设施：同一套断言可以同时跑 SQLite 与 MySQL。

线上部署用的是 MySQL，而 MySQLTaskStore 实现了与 SQLite 同一份接口；
设置 MOMENTUM_TEST_MYSQL_URL 时把关键套件也跑一遍 MySQL，能提前发现只在该后端出现的问题。
"""
from __future__ import annotations

import os

import pytest

from momentum_agent.storage import MySQLTaskStore


def fresh_mysql_store() -> MySQLTaskStore:
    """清空该库并重建 schema，返回一个干净的 MySQL store；未配置则跳过。

    注意：MySQLTaskStore._init_schema() 有按 DSN 的类级缓存，必须先把它清掉，
    否则删表之后不会重建，用例会撞上「表不存在」。另外 store 的 cursor 是 DictCursor，
    取表名时要取值而不是键。
    """
    dsn = os.environ.get("MOMENTUM_TEST_MYSQL_URL")
    if not dsn:
        pytest.skip("MOMENTUM_TEST_MYSQL_URL is not set")
    store = MySQLTaskStore(dsn)
    with store._connect() as connection:
        cursor = connection.cursor()
        cursor.execute("SET FOREIGN_KEY_CHECKS=0")
        cursor.execute("SHOW TABLES")
        tables = [next(iter(row.values())) for row in cursor.fetchall()]
        for table in tables:
            cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
        cursor.execute("SET FOREIGN_KEY_CHECKS=1")
        connection.commit()
    MySQLTaskStore._schema_initialized.discard(dsn)
    return MySQLTaskStore(dsn)

