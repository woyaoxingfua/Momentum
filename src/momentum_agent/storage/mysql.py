"""MySQL 存储后端 — 支持多用户部署。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from urllib.parse import urlparse

from ..logger import get_logger, log_db_query, log_security_event
from ..models import Priority, Task, TaskStatus, TaskRelation, TaskRelationType
from .sqlite import (
    DEFAULT_USER,
    SESSION_LIFETIME,
    decode_dt,
    encode_dt,
    row_to_task,
    row_to_task_relation,
    utcnow,
    _deserialize_tags,
    _next_recurrence_due,
    _serialize_tags,
)

log = get_logger("storage.mysql")

__all__ = ["MySQLTaskStore"]


class _DoneLedgerDuplicate(Exception):
    """The done ledger unique key lost a race and must be read after rollback."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(64) PRIMARY KEY,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS sessions (
    token VARCHAR(128) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tasks (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'todo',
    priority VARCHAR(16) NOT NULL DEFAULT 'medium',
    due_at VARCHAR(64),
    estimated_minutes INT,
    notes TEXT,
    parent_task_id INT REFERENCES tasks(id) ON DELETE CASCADE,
    recurrence TEXT,
    user_id VARCHAR(64) NOT NULL DEFAULT 'default' REFERENCES users(id) ON DELETE CASCADE,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    tags TEXT,
    INDEX idx_tasks_user_status (user_id, status),
    INDEX idx_tasks_user_due (user_id, due_at),
    INDEX idx_tasks_parent (parent_task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_relations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    source_task_id INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    target_task_id INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    relation_type VARCHAR(16) NOT NULL,
    user_id VARCHAR(64) NOT NULL DEFAULT 'default' REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    UNIQUE KEY uk_relation (source_task_id, target_task_id, relation_type),
    INDEX idx_relations_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_memory (
    user_id VARCHAR(64) NOT NULL DEFAULT 'local',
    `key` VARCHAR(128) NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, `key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_events (
    id INT AUTO_INCREMENT PRIMARY KEY,
    task_id INT REFERENCES tasks(id) ON DELETE CASCADE,
    event_type VARCHAR(32) NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL,
    INDEX idx_events_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_postpone_idempotency (
    user_id VARCHAR(64) NOT NULL,
    idempotency_key CHAR(36) NOT NULL,
    request_fingerprint VARCHAR(255) NOT NULL,
    response_status SMALLINT NOT NULL,
    response_json TEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (user_id, idempotency_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_done_idempotency (
    user_id VARCHAR(64) NOT NULL,
    idempotency_key CHAR(36) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    response_status SMALLINT NOT NULL,
    response_json TEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (user_id, idempotency_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_done_occurrences (
    user_id VARCHAR(64) NOT NULL,
    source_task_id INT NOT NULL,
    source_done_event_id INT NOT NULL,
    next_task_id INT NULL,
    response_status SMALLINT NOT NULL,
    response_json TEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uk_done_occurrence (user_id, source_task_id, source_done_event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _parse_mysql_url(url: str) -> dict[str, Any]:
    """把 mysql:// URL 解析成 pymysql.connect 参数。

    支持查询参数：
      - ssl=true 启用 SSL 连接（Azure MySQL 需要）
      - ssl_ca=path 自定义 CA 证书路径

    注意：密码中的特殊字符（如 @ 等）需要 URL 编码，例如 @ -> %40。
    """
    parsed = urlparse(url)
    from urllib.parse import parse_qs, unquote
    query = parse_qs(parsed.query)
    ssl_enabled = query.get("ssl", ["false"])[0].lower() in ("true", "1", "yes", "on")

    result = {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username) if parsed.username else "root",
        "password": unquote(parsed.password) if parsed.password else "",
        "database": parsed.path.lstrip("/") or None,
        "charset": "utf8mb4",
        "cursorclass": "pymysql.cursors.DictCursor",
        "autocommit": False,
    }

    if ssl_enabled:
        import ssl as _ssl
        ssl_ca = query.get("ssl_ca", [None])[0]
        if ssl_ca:
            ssl_ctx = _ssl.create_default_context(cafile=ssl_ca)
        else:
            ssl_ctx = _ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = _ssl.CERT_NONE
        result["ssl"] = ssl_ctx

    return result


class MySQLTaskStore:
    """基于 MySQL 的任务存储后端，适合多用户部署。"""

    # 已初始化 schema 的 DSN 缓存，避免每次实例化都执行 migration
    _schema_initialized: set[str] = set()

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._connect_kwargs = _parse_mysql_url(dsn)
        self.host = self._connect_kwargs["host"]
        self.port = self._connect_kwargs["port"]
        self.user = self._connect_kwargs["user"]
        self.password = self._connect_kwargs["password"]
        self.database = self._connect_kwargs["database"]
        self._init_schema()
        log.info("mysql store opened: %s@%s/%s", self.user, self.host, self.database)

    def _init_schema(self) -> None:
        key = self.dsn
        if key in MySQLTaskStore._schema_initialized:
            log.debug("mysql schema already initialized for %s", self.dsn)
            return
        log.debug("initializing mysql schema")
        with self._connect() as conn:
            self._migrate(conn)
            self._ensure_default_user(conn)
        MySQLTaskStore._schema_initialized.add(key)

    @contextmanager
    def _connect(self) -> Generator[Any, None, None]:
        import pymysql

        connect_kwargs = dict(self._connect_kwargs)
        cursorclass_name = connect_kwargs.pop("cursorclass")
        connect_kwargs["cursorclass"] = _import_cursor_class(cursorclass_name)
        conn = pymysql.connect(**connect_kwargs)
        try:
            yield conn
            conn.commit()
        except Exception as e:
            log.error("database error: %s", e)
            conn.rollback()
            raise
        finally:
            conn.close()

    def _cursor(self, conn: Any) -> Any:
        return conn.cursor()

    def _execute(self, conn: Any, sql: str, params: tuple | list | None = None) -> Any:
        log_db_query(sql)
        cur = self._cursor(conn)
        cur.execute(sql, params)
        return cur

    def _ensure_default_user(self, conn: Any) -> None:
        from ..auth import hash_password

        cur = self._execute(conn, "SELECT COUNT(*) AS cnt FROM users")
        existing = cur.fetchone()["cnt"]
        if existing == 0:
            self._execute(
                conn,
                "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (%s, %s, %s, %s)",
                (DEFAULT_USER, "默认用户", hash_password("momentum"), encode_dt(utcnow())),
            )
            log.info("created default user (password: momentum)")

    def _migrate(self, conn: Any) -> None:
        from ..auth import hash_password

        cur = self._cursor(conn)
        for stmt in _split_schema(SCHEMA):
            cur.execute(stmt)

        # 迁移 users 表：添加 password_hash 列（兼容旧数据库）
        cur.execute(
            """
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'users' AND COLUMN_NAME = 'password_hash'
            """
        )
        if not cur.fetchone():
            log.info("migration: adding password_hash column to users")
            cur.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
            default_hash = hash_password("momentum")
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE password_hash IS NULL",
                (default_hash,),
            )

        # 迁移 tasks 表：添加各列
        cur.execute(
            """
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'tasks' AND COLUMN_NAME IN ('recurrence', 'user_id', 'tags')
            """
        )
        existing_cols = {row["COLUMN_NAME"] for row in cur.fetchall()}
        if "recurrence" not in existing_cols:
            log.info("migration: adding recurrence column")
            cur.execute("ALTER TABLE tasks ADD COLUMN recurrence TEXT")
        if "user_id" not in existing_cols:
            log.info("migration: adding user_id column")
            cur.execute("ALTER TABLE tasks ADD COLUMN user_id VARCHAR(64) NOT NULL DEFAULT 'default'")
        if "tags" not in existing_cols:
            log.info("migration: adding tags column")
            cur.execute("ALTER TABLE tasks ADD COLUMN tags TEXT")

        # 迁移 sessions 表：添加过期时间列
        cur.execute(
            """
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'sessions' AND COLUMN_NAME = 'expires_at'
            """
        )
        if not cur.fetchone():
            log.info("migration: adding expires_at column to sessions")
            cur.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT")
            from ..auth import utcnow as auth_now
            default_expires = encode_dt(auth_now() + SESSION_LIFETIME)
            cur.execute(
                "UPDATE sessions SET expires_at = %s WHERE expires_at IS NULL",
                (default_expires,),
            )

    # ── auth ───────────────────────────────────────────────────────

    def register_user(self, user_id: str, display_name: str, password_hash: str) -> None:
        from ..auth import utcnow as auth_now

        log.info("register user=%r", user_id)
        with self._connect() as conn:
            self._execute(
                conn,
                "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (%s, %s, %s, %s)",
                (user_id, display_name, password_hash, encode_dt(auth_now())),
            )

    def login_user(self, user_id: str, password: str) -> str | None:
        from ..auth import generate_token, utcnow as auth_now, verify_password

        with self._connect() as conn:
            cur = self._execute(conn, "SELECT password_hash FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                log_security_event("login_failed", user_id, "用户不存在")
                return None
            if not verify_password(password, row["password_hash"]):
                log_security_event("login_failed", user_id, "密码错误")
                return None
            token = generate_token()
            now = auth_now()
            self._execute(
                conn,
                """
                INSERT INTO sessions (token, user_id, created_at, expires_at)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    user_id = VALUES(user_id),
                    created_at = VALUES(created_at),
                    expires_at = VALUES(expires_at)
                """,
                (token, user_id, encode_dt(now), encode_dt(now + SESSION_LIFETIME)),
            )
        log.info("login user=%r", user_id)
        return token

    def validate_session(self, token: str) -> str | None:
        from ..auth import utcnow as auth_now

        with self._connect() as conn:
            cur = self._execute(conn, "SELECT user_id, expires_at FROM sessions WHERE token = %s", (token,))
            row = cur.fetchone()
            if not row:
                return None
            expires = decode_dt(row["expires_at"])
            if expires and expires < auth_now():
                self._execute(conn, "DELETE FROM sessions WHERE token = %s", (token,))
                return None
            return row["user_id"]

    def logout_user(self, token: str) -> None:
        log.info("logout token=...%s", token[-8:])
        with self._connect() as conn:
            self._execute(conn, "DELETE FROM sessions WHERE token = %s", (token,))

    def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        from ..auth import verify_password, hash_password

        with self._connect() as conn:
            cur = self._execute(conn, "SELECT password_hash FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row or not verify_password(old_password, row["password_hash"]):
                return False
            self._execute(
                conn,
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (hash_password(new_password), user_id),
            )
        log.info("password changed for user=%r", user_id)
        return True

    def list_users(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            cur = self._execute(conn, "SELECT id, display_name FROM users ORDER BY id")
            rows = cur.fetchall()
        return [{"id": row["id"], "display_name": row["display_name"]} for row in rows]

    # ── tasks ──────────────────────────────────────────────────────

    def create_task(
        self,
        title: str,
        *,
        due_at: datetime | None = None,
        priority: Priority = Priority.MEDIUM,
        estimated_minutes: int | None = None,
        notes: str | None = None,
        parent_task_id: int | None = None,
        recurrence: str | None = None,
        tags: list[str] | None = None,
        user_id: str = DEFAULT_USER,
    ) -> Task:
        now = utcnow()
        log.info("create_task title=%r user=%r priority=%s", title.strip(), user_id, priority.value)
        tags_str = _serialize_tags(tags)
        with self._connect() as conn:
            self._execute(
                conn,
                """
                INSERT INTO tasks (
                    title, status, priority, due_at, estimated_minutes, notes,
                    parent_task_id, recurrence, user_id, created_at, updated_at, tags
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    title.strip(),
                    TaskStatus.TODO.value,
                    priority.value,
                    encode_dt(due_at),
                    estimated_minutes,
                    notes,
                    parent_task_id,
                    recurrence,
                    user_id,
                    encode_dt(now),
                    encode_dt(now),
                    tags_str,
                ),
            )
            task_id = int(conn.insert_id())
            self._execute(
                conn,
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                (task_id, "created", None, encode_dt(now)),
            )
            cur = self._execute(conn, "SELECT * FROM tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
        task = row_to_task(row)
        log.debug("created task #%d", task.id)
        return task

    def list_tasks(
        self, status: TaskStatus | None = TaskStatus.TODO, *, user_id: str = DEFAULT_USER
    ) -> list[Task]:
        log.debug("list_tasks status=%s user=%r", status, user_id)
        with self._connect() as conn:
            if status is None:
                cur = self._execute(
                    conn,
                    "SELECT * FROM tasks WHERE user_id = %s ORDER BY due_at IS NULL, due_at, id",
                    (user_id,),
                )
            else:
                cur = self._execute(
                    conn,
                    "SELECT * FROM tasks WHERE status = %s AND user_id = %s ORDER BY due_at IS NULL, due_at, id",
                    (status.value, user_id),
                )
            rows = cur.fetchall()
        return [row_to_task(row) for row in rows]

    def _get_task(self, task_id: int) -> Task | None:
        with self._connect() as conn:
            cur = self._execute(conn, "SELECT * FROM tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
        return row_to_task(row) if row else None

    def get_task_for_user(self, task_id: int, user_id: str) -> Task | None:
        """按 ID 和 owner 一次查询任务，不暴露其他用户的记录。"""
        with self._connect() as conn:
            cur = self._execute(
                conn, "SELECT * FROM tasks WHERE id = %s AND user_id = %s", (task_id, user_id)
            )
            row = cur.fetchone()
        return row_to_task(row) if row else None

    def update_status(
        self, task_id: int, status: TaskStatus, *, user_id: str | None = None
    ) -> Task | None:
        now = utcnow()
        log.info("update_status task=%d status=%s user=%r", task_id, status.value, user_id)
        with self._connect() as conn:
            conn.begin()
            if user_id is not None:
                cur = self._execute(
                    conn,
                    "SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s",
                    (task_id, user_id),
                )
            else:
                cur = self._execute(conn, "SELECT parent_task_id FROM tasks WHERE id = %s", (task_id,))
            target_meta = cur.fetchone()
            ancestor_ids = []
            parent_id = target_meta["parent_task_id"] if target_meta else None
            while parent_id is not None and parent_id not in ancestor_ids:
                ancestor_ids.append(parent_id)
                if user_id is not None:
                    cur = self._execute(
                        conn,
                        "SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s",
                        (parent_id, user_id),
                    )
                else:
                    cur = self._execute(conn, "SELECT parent_task_id FROM tasks WHERE id = %s", (parent_id,))
                parent_meta = cur.fetchone()
                parent_id = parent_meta["parent_task_id"] if parent_meta else None
            # Lock from the root toward the target. Sibling transitions then
            # serialize on a common ancestor before either child row is locked.
            for ancestor_id in reversed(ancestor_ids):
                if user_id is not None:
                    self._execute(
                        conn,
                        "SELECT id FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                        (ancestor_id, user_id),
                    )
                else:
                    self._execute(conn, "SELECT id FROM tasks WHERE id = %s FOR UPDATE", (ancestor_id,))
            if user_id is not None:
                cur = self._execute(
                    conn,
                    "SELECT * FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                    (task_id, user_id),
                )
            else:
                cur = self._execute(conn, "SELECT * FROM tasks WHERE id = %s FOR UPDATE", (task_id,))
            row = cur.fetchone()
            if row is None:
                log.warning("update_status: task #%d not owned by %r", task_id, user_id)
                return None
            if row["status"] != status.value:
                if user_id is None:
                    self._execute(
                        conn, "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s",
                        (status.value, encode_dt(now), task_id),
                    )
                else:
                    self._execute(
                        conn, "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                        (status.value, encode_dt(now), task_id, user_id),
                    )
                self._execute(
                    conn,
                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                    (task_id, "status_changed", status.value, encode_dt(now)),
                )

                if status == TaskStatus.DONE:
                    queue = [task_id]
                    while queue:
                        completed_id = queue.pop(0)
                        if user_id is None:
                            cur = self._execute(
                                conn, "SELECT parent_task_id FROM tasks WHERE id = %s FOR UPDATE", (completed_id,)
                            )
                            completed = cur.fetchone()
                            cur = self._execute(
                                conn,
                                "SELECT id FROM tasks WHERE parent_task_id = %s AND status != %s ORDER BY id FOR UPDATE",
                                (completed_id, TaskStatus.DONE.value),
                            )
                        else:
                            cur = self._execute(
                                conn, "SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                                (completed_id, user_id),
                            )
                            completed = cur.fetchone()
                            cur = self._execute(
                                conn,
                                "SELECT id FROM tasks WHERE parent_task_id = %s AND status != %s AND user_id = %s ORDER BY id FOR UPDATE",
                                (completed_id, TaskStatus.DONE.value, user_id),
                            )
                        children = cur.fetchall()
                        changed_children = 0
                        for child in children:
                            if user_id is None:
                                update = self._execute(
                                    conn,
                                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND status != %s",
                                    (TaskStatus.DONE.value, encode_dt(now), child["id"], TaskStatus.DONE.value),
                                )
                            else:
                                update = self._execute(
                                    conn,
                                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND status != %s AND user_id = %s",
                                    (TaskStatus.DONE.value, encode_dt(now), child["id"], TaskStatus.DONE.value, user_id),
                                )
                            if update.rowcount:
                                changed_children += 1
                                self._execute(
                                    conn,
                                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                                    (child["id"], "status_changed", TaskStatus.DONE.value, encode_dt(now)),
                                )
                                queue.append(child["id"])
                        if changed_children:
                            self._execute(
                                conn,
                                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                                (completed_id, "subtasks_completed", None, encode_dt(now)),
                            )

                        parent_id = completed["parent_task_id"] if completed else None
                        if parent_id is None:
                            continue
                        if user_id is None:
                            cur = self._execute(
                                conn, "SELECT status FROM tasks WHERE id = %s FOR UPDATE", (parent_id,)
                            )
                            parent = cur.fetchone()
                            cur = self._execute(
                                conn, "SELECT status FROM tasks WHERE parent_task_id = %s FOR UPDATE", (parent_id,)
                            )
                        else:
                            cur = self._execute(
                                conn, "SELECT status FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                                (parent_id, user_id),
                            )
                            parent = cur.fetchone()
                            cur = self._execute(
                                conn, "SELECT status FROM tasks WHERE parent_task_id = %s AND user_id = %s FOR UPDATE",
                                (parent_id, user_id),
                            )
                        siblings = cur.fetchall()
                        if parent and parent["status"] != TaskStatus.DONE.value and siblings and all(
                            sibling["status"] == TaskStatus.DONE.value for sibling in siblings
                        ):
                            if user_id is None:
                                update = self._execute(
                                    conn,
                                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND status != %s",
                                    (TaskStatus.DONE.value, encode_dt(now), parent_id, TaskStatus.DONE.value),
                                )
                            else:
                                update = self._execute(
                                    conn,
                                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND status != %s AND user_id = %s",
                                    (TaskStatus.DONE.value, encode_dt(now), parent_id, TaskStatus.DONE.value, user_id),
                                )
                            if update.rowcount:
                                self._execute(
                                    conn,
                                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                                    (parent_id, "status_changed", TaskStatus.DONE.value, encode_dt(now)),
                                )
                                queue.append(parent_id)
            cur = self._execute(conn, "SELECT * FROM tasks WHERE id = %s", (task_id,))
            final_row = cur.fetchone()
        return row_to_task(final_row) if final_row else None

    def update_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        due_at: datetime | None = None,
        priority: Priority | None = None,
        estimated_minutes: int | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        parent_task_id: int | None = None,
        user_id: str = DEFAULT_USER,
    ) -> Task | None:
        log.info("update_task id=%d user=%r", task_id, user_id)
        now = utcnow()
        sets: list[str] = []
        params: list[object] = []
        if title is not None:
            sets.append("title = %s")
            params.append(title.strip())
        if due_at is not None:
            sets.append("due_at = %s")
            params.append(encode_dt(due_at))
        if priority is not None:
            sets.append("priority = %s")
            params.append(priority.value)
        if estimated_minutes is not None:
            sets.append("estimated_minutes = %s")
            params.append(estimated_minutes)
        if notes is not None:
            sets.append("notes = %s")
            params.append(notes)
        if tags is not None:
            sets.append("tags = %s")
            params.append(_serialize_tags(tags))
        if parent_task_id is not None:
            sets.append("parent_task_id = %s")
            params.append(parent_task_id)
        if not sets:
            return self.get_task_for_user(task_id, user_id)
        sets.append("updated_at = %s")
        params.append(encode_dt(now))
        params.append(task_id)
        params.append(user_id)
        with self._connect() as conn:
            conn.begin()
            owned = self._execute(
                conn,
                "SELECT id FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                (task_id, user_id),
            ).fetchone()
            if owned is None:
                log.warning("update_task: task #%d not owned by %r", task_id, user_id)
                return None
            self._execute(
                conn,
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = %s AND user_id = %s",
                params,
            )
            self._execute(
                conn,
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                (task_id, "updated", None, encode_dt(now)),
            )
            cur = self._execute(
                conn, "SELECT * FROM tasks WHERE id = %s AND user_id = %s", (task_id, user_id)
            )
            row = cur.fetchone()
        return row_to_task(row) if row else None

    def postpone_task(self, task_id: int, days: int, *, user_id: str | None = None) -> Task | None:
        from .errors import TaskCannotBePostponed

        owner = user_id or DEFAULT_USER
        now = utcnow()
        with self._connect() as conn:
            conn.begin()
            cur = self._execute(
                conn,
                "SELECT * FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                (task_id, owner),
            )
            row = cur.fetchone()
            if row is None:
                log.warning("postpone_task: task #%d not owned by %r", task_id, owner)
                return None
            task = row_to_task(row)
            if task.due_at is None or task.status not in (TaskStatus.TODO, TaskStatus.DOING):
                raise TaskCannotBePostponed
            new_due = task.due_at + timedelta(days=days)
            log.info("postpone task=%d days=%d new_due=%s", task_id, days, new_due.isoformat())
            self._execute(
                conn,
                "UPDATE tasks SET due_at = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                (encode_dt(new_due), encode_dt(now), task_id, owner),
            )
            self._execute(
                conn,
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                (task_id, "updated", None, encode_dt(now)),
            )
            updated = self._execute(
                conn,
                "SELECT * FROM tasks WHERE id = %s AND user_id = %s",
                (task_id, owner),
            ).fetchone()
        return row_to_task(updated) if updated else None

    def postpone_task_idempotent(
        self,
        task_id: int,
        days: Any,
        *,
        user_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> tuple[int, dict[str, str]]:
        """Serialize a user/key pair and atomically persist its postpone response."""
        import hashlib
        import json

        lock_name = hashlib.sha256(f"{user_id}\0{idempotency_key}".encode("utf-8")).hexdigest()
        result: tuple[int, dict[str, str]]
        with self._connect() as conn:
            lock = self._execute(
                conn, "SELECT GET_LOCK(%s, %s) AS acquired", (lock_name, 10)
            ).fetchone()
            if not lock or int(lock.get("acquired") or 0) != 1:
                raise TimeoutError("could not acquire postpone idempotency lock")
            try:
                conn.begin()
                try:
                    previous = self._execute(
                        conn,
                        """
                        SELECT request_fingerprint, response_status, response_json
                        FROM task_postpone_idempotency
                        WHERE user_id = %s AND idempotency_key = %s FOR UPDATE
                        """,
                        (user_id, idempotency_key),
                    ).fetchone()
                    if previous is not None:
                        if previous["request_fingerprint"] != request_fingerprint:
                            result = (409, {"error": "idempotency_conflict"})
                        else:
                            result = (
                                int(previous["response_status"]),
                                json.loads(previous["response_json"]),
                            )
                    elif type(days) is not int or days <= 0:
                        result = (400, {"error": "days_invalid"})
                    else:
                        row = self._execute(
                            conn,
                            "SELECT * FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                            (task_id, user_id),
                        ).fetchone()
                        status = None
                        if row is None:
                            status, response = 404, {"error": "没有找到这个任务。"}
                        else:
                            task = row_to_task(row)
                            if task.due_at is None or task.status not in (TaskStatus.TODO, TaskStatus.DOING):
                                status, response = 409, {"error": "该任务当前无法顺延截止日。"}
                            else:
                                try:
                                    new_due = task.due_at + timedelta(days=days)
                                except OverflowError:
                                    result = (400, {"error": "days_out_of_range"})
                                else:
                                    now = utcnow()
                                    self._execute(
                                        conn,
                                        "UPDATE tasks SET due_at = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                                        (encode_dt(new_due), encode_dt(now), task_id, user_id),
                                    )
                                    self._execute(
                                        conn,
                                        "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                                        (task_id, "updated", None, encode_dt(now)),
                                    )
                                    updated = self._execute(
                                        conn,
                                        "SELECT * FROM tasks WHERE id = %s AND user_id = %s",
                                        (task_id, user_id),
                                    ).fetchone()
                                    if updated is None:
                                        raise RuntimeError("postponed task disappeared before response persistence")
                                    updated_task = row_to_task(updated)
                                    due = updated_task.due_at.strftime("%Y-%m-%d %H:%M") if updated_task.due_at else "无截止"
                                    status, response = 200, {
                                        "message": f"任务 #{updated_task.id}「{updated_task.title}」已推迟至 {due}"
                                    }
                        if status is not None:
                            self._execute(
                                conn,
                                """
                                INSERT INTO task_postpone_idempotency
                                    (user_id, idempotency_key, request_fingerprint, response_status, response_json, created_at)
                                VALUES (%s, %s, %s, %s, %s, %s)
                                """,
                                (user_id, idempotency_key, request_fingerprint, status,
                                 json.dumps(response, ensure_ascii=False), encode_dt(utcnow())),
                            )
                            result = (status, response)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            finally:
                self._execute(conn, "SELECT RELEASE_LOCK(%s)", (lock_name,))
        return result

    def drop_task(self, task_id: int, *, user_id: str | None = None) -> Task | None:
        log.info("drop_task id=%d user=%r", task_id, user_id)
        return self.update_status(task_id, TaskStatus.DROPPED, user_id=user_id)

    def start_task(self, task_id: int, *, user_id: str | None = None) -> Task | None:
        log.info("start_task id=%d user=%r", task_id, user_id)
        return self.update_status(task_id, TaskStatus.DOING, user_id=user_id)

    def reopen_task(self, task_id: int, *, user_id: str | None = None) -> Task | None:
        log.info("reopen_task id=%d user=%r", task_id, user_id)
        task = self.update_status(task_id, TaskStatus.TODO, user_id=user_id)
        if task and task.parent_task_id:
            self._ensure_parent_active(task.parent_task_id)
        return task

    def _ensure_parent_active(self, parent_id: int) -> None:
        parent = self._get_task(parent_id)
        if parent and parent.status in (TaskStatus.DONE, TaskStatus.DROPPED):
            self.update_status(parent_id, TaskStatus.TODO)

    def _auto_complete_parent(self, parent_id: int) -> None:
        with self._connect() as conn:
            cur = self._execute(conn, "SELECT * FROM tasks WHERE parent_task_id = %s", (parent_id,))
            children = cur.fetchall()
        if children and all(row["status"] == TaskStatus.DONE.value for row in children):
            log.info("auto-completing parent task #%d", parent_id)
            self.update_status(parent_id, TaskStatus.DONE)

    def _cascade_done_in_transaction(self, conn: Any, task_id: int, user_id: str, now: datetime) -> None:
        """Apply the existing cascade rules using the caller's InnoDB transaction."""
        queue = [task_id]
        while queue:
            completed_id = queue.pop(0)
            completed = self._execute(
                conn,
                "SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                (completed_id, user_id),
            ).fetchone()
            children = self._execute(
                conn,
                "SELECT id FROM tasks WHERE parent_task_id = %s AND status != %s AND user_id = %s ORDER BY id FOR UPDATE",
                (completed_id, TaskStatus.DONE.value, user_id),
            ).fetchall()
            changed_children = 0
            for child in children:
                updated = self._execute(
                    conn,
                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND status != %s AND user_id = %s",
                    (TaskStatus.DONE.value, encode_dt(now), child["id"], TaskStatus.DONE.value, user_id),
                )
                if updated.rowcount:
                    changed_children += 1
                    self._execute(
                        conn,
                        "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                        (child["id"], "status_changed", TaskStatus.DONE.value, encode_dt(now)),
                    )
                    queue.append(child["id"])
            if changed_children:
                self._execute(
                    conn,
                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                    (completed_id, "subtasks_completed", None, encode_dt(now)),
                )

            parent_id = completed["parent_task_id"] if completed else None
            if parent_id is None:
                continue
            parent = self._execute(
                conn,
                "SELECT status FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                (parent_id, user_id),
            ).fetchone()
            siblings = self._execute(
                conn,
                "SELECT status FROM tasks WHERE parent_task_id = %s AND user_id = %s FOR UPDATE",
                (parent_id, user_id),
            ).fetchall()
            if parent and parent["status"] != TaskStatus.DONE.value and siblings and all(
                sibling["status"] == TaskStatus.DONE.value for sibling in siblings
            ):
                updated = self._execute(
                    conn,
                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND status != %s AND user_id = %s",
                    (TaskStatus.DONE.value, encode_dt(now), parent_id, TaskStatus.DONE.value, user_id),
                )
                if updated.rowcount:
                    self._execute(
                        conn,
                        "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                        (parent_id, "status_changed", TaskStatus.DONE.value, encode_dt(now)),
                    )
                    queue.append(parent_id)

    def complete_task_idempotent(
        self,
        task_id: int,
        *,
        user_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> tuple[int, dict[str, str]]:
        """Complete one occurrence atomically and persist the user's response."""
        import hashlib
        import json

        lock_name = hashlib.sha256(f"{user_id}\0{idempotency_key}".encode("utf-8")).hexdigest()
        result: tuple[int, dict[str, str]]
        try:
            with self._connect() as conn:
                lock = self._execute(
                    conn, "SELECT GET_LOCK(%s, %s) AS acquired", (lock_name, 10)
                ).fetchone()
                if not lock or int(lock.get("acquired") or 0) != 1:
                    raise TimeoutError("could not acquire done idempotency lock")
                try:
                    conn.begin()
                    try:
                        # Recheck the request ledger after acquiring the key lock.
                        previous = self._execute(
                            conn,
                            """
                            SELECT request_fingerprint, response_status, response_json
                            FROM task_done_idempotency
                            WHERE user_id = %s AND idempotency_key = %s FOR UPDATE
                            """,
                            (user_id, idempotency_key),
                        ).fetchone()
                        if previous is not None:
                            if previous["request_fingerprint"] != request_fingerprint:
                                result = (409, {"error": "idempotency_conflict"})
                            else:
                                result = (
                                    int(previous["response_status"]),
                                    json.loads(previous["response_json"]),
                                )
                        else:
                            target_meta = self._execute(
                                conn,
                                "SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s",
                                (task_id, user_id),
                            ).fetchone()
                            ancestor_ids = []
                            parent_id = target_meta["parent_task_id"] if target_meta else None
                            while parent_id is not None and parent_id not in ancestor_ids:
                                ancestor_ids.append(parent_id)
                                parent_meta = self._execute(
                                    conn,
                                    "SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s",
                                    (parent_id, user_id),
                                ).fetchone()
                                parent_id = parent_meta["parent_task_id"] if parent_meta else None
                            for ancestor_id in reversed(ancestor_ids):
                                self._execute(
                                    conn,
                                    "SELECT id FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                                    (ancestor_id, user_id),
                                )
                            row = self._execute(
                                conn,
                                "SELECT * FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                                (task_id, user_id),
                            ).fetchone()
                            if row is None:
                                status, response = 404, {"error": "没有找到这个任务。"}
                            elif row["status"] == TaskStatus.DONE.value:
                                status, response = 200, {"message": "任务已完成。"}
                            else:
                                now = utcnow()
                                self._execute(
                                    conn,
                                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                                    (TaskStatus.DONE.value, encode_dt(now), task_id, user_id),
                                )
                                self._execute(
                                    conn,
                                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                                    (task_id, "status_changed", TaskStatus.DONE.value, encode_dt(now)),
                                )
                                source_done_event_id = int(conn.insert_id())
                                self._cascade_done_in_transaction(conn, task_id, user_id, now)
                                occurrence = self._execute(
                                    conn,
                                    """
                                    SELECT next_task_id, response_status, response_json
                                    FROM task_done_occurrences
                                    WHERE user_id = %s AND source_task_id = %s AND source_done_event_id = %s
                                    FOR UPDATE
                                    """,
                                    (user_id, task_id, source_done_event_id),
                                ).fetchone()
                                if occurrence is not None:
                                    next_task_id = occurrence["next_task_id"]
                                    status = int(occurrence["response_status"])
                                    response = json.loads(occurrence["response_json"])
                                else:
                                    next_task_id = None
                                    if row["recurrence"]:
                                        next_due = _next_recurrence_due(
                                            decode_dt(row["due_at"]), row["recurrence"]
                                        )
                                        created_at = encode_dt(now)
                                        self._execute(
                                            conn,
                                            """
                                            INSERT INTO tasks (
                                                title, status, priority, due_at, estimated_minutes, notes,
                                                parent_task_id, recurrence, user_id, created_at, updated_at, tags
                                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                            """,
                                            (
                                                row["title"], TaskStatus.TODO.value, row["priority"],
                                                encode_dt(next_due), row["estimated_minutes"], row["notes"],
                                                None, row["recurrence"], user_id, created_at, created_at, None,
                                            ),
                                        )
                                        next_task_id = int(conn.insert_id())
                                        self._execute(
                                            conn,
                                            "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                                            (next_task_id, "created", None, created_at),
                                        )
                                        response = {
                                            "message": f"已创建下一期任务 #{next_task_id}：{row['title']}"
                                        }
                                    else:
                                        response = {
                                            "message": f"已完成任务 #{task_id}：{row['title']}"
                                        }
                                    status = 200
                                    response_json = json.dumps(response, ensure_ascii=False)
                                    self._execute(
                                        conn,
                                        """
                                        INSERT INTO task_done_occurrences
                                            (user_id, source_task_id, source_done_event_id, next_task_id,
                                             response_status, response_json, created_at)
                                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                                        """,
                                        (
                                            user_id, task_id, source_done_event_id, next_task_id,
                                            status, response_json, encode_dt(now),
                                        ),
                                    )

                            if row is None:
                                # Preserve the generic 404 without reserving a fresh key.
                                result = (status, response)
                            else:
                                response_json = json.dumps(response, ensure_ascii=False)
                                try:
                                    self._execute(
                                        conn,
                                        """
                                        INSERT INTO task_done_idempotency
                                            (user_id, idempotency_key, request_fingerprint,
                                             response_status, response_json, created_at)
                                        VALUES (%s, %s, %s, %s, %s, %s)
                                        """,
                                        (
                                            user_id, idempotency_key, request_fingerprint,
                                            status, response_json, encode_dt(utcnow()),
                                        ),
                                    )
                                except Exception as exc:
                                    if getattr(exc, "args", ()) and exc.args[0] == 1062:
                                        raise _DoneLedgerDuplicate from exc
                                    raise
                                result = (status, response)
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                finally:
                    self._execute(conn, "SELECT RELEASE_LOCK(%s)", (lock_name,))
            return result
        except _DoneLedgerDuplicate:
            # The failed transaction is rolled back; read the winner separately.
            with self._connect() as conn:
                conn.begin()
                previous = self._execute(
                    conn,
                    """
                    SELECT request_fingerprint, response_status, response_json
                    FROM task_done_idempotency
                    WHERE user_id = %s AND idempotency_key = %s
                    """,
                    (user_id, idempotency_key),
                ).fetchone()
            if previous is None:
                raise RuntimeError("duplicate done ledger key has no committed canonical row")
            if previous["request_fingerprint"] != request_fingerprint:
                return 409, {"error": "idempotency_conflict"}
            return int(previous["response_status"]), json.loads(previous["response_json"])

    def complete_task_agent(
        self, task_id: int, *, user_id: str
    ) -> tuple[Task | None, bool, Task | None]:
        """原子完成 Agent 目标；返回任务、本次是否发生转换及本次创建的 next。"""
        import json

        with self._connect() as conn:
            conn.begin()
            target_meta = self._execute(
                conn,
                "SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s",
                (task_id, user_id),
            ).fetchone()
            ancestor_ids = []
            parent_id = target_meta["parent_task_id"] if target_meta else None
            while parent_id is not None and parent_id not in ancestor_ids:
                ancestor_ids.append(parent_id)
                parent_meta = self._execute(
                    conn,
                    "SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s",
                    (parent_id, user_id),
                ).fetchone()
                parent_id = parent_meta["parent_task_id"] if parent_meta else None
            for ancestor_id in reversed(ancestor_ids):
                self._execute(
                    conn,
                    "SELECT id FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                    (ancestor_id, user_id),
                )

            row = self._execute(
                conn,
                "SELECT * FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE",
                (task_id, user_id),
            ).fetchone()
            if row is None:
                return None, False, None
            if row["status"] == TaskStatus.DONE.value:
                # 旧 DONE 行或已完成 occurrence 均为稳定 no-op，不回填 mapping。
                return row_to_task(row), False, None

            now = utcnow()
            self._execute(
                conn,
                "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                (TaskStatus.DONE.value, encode_dt(now), task_id, user_id),
            )
            self._execute(
                conn,
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                (task_id, "status_changed", TaskStatus.DONE.value, encode_dt(now)),
            )
            source_done_event_id = int(conn.insert_id())
            self._cascade_done_in_transaction(conn, task_id, user_id, now)

            next_row = None
            next_task_id = None
            if row["recurrence"]:
                next_due = _next_recurrence_due(decode_dt(row["due_at"]), row["recurrence"])
                created_at = encode_dt(now)
                self._execute(
                    conn,
                    """
                    INSERT INTO tasks (
                        title, status, priority, due_at, estimated_minutes, notes,
                        parent_task_id, recurrence, user_id, created_at, updated_at, tags
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        row["title"], TaskStatus.TODO.value, row["priority"], encode_dt(next_due),
                        row["estimated_minutes"], row["notes"], None, row["recurrence"],
                        user_id, created_at, created_at, None,
                    ),
                )
                next_task_id = int(conn.insert_id())
                self._execute(
                    conn,
                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                    (next_task_id, "created", None, created_at),
                )
                next_row = self._execute(
                    conn,
                    "SELECT * FROM tasks WHERE id = %s AND user_id = %s",
                    (next_task_id, user_id),
                ).fetchone()
                response = {"message": f"已创建下一期任务 #{next_task_id}：{row['title']}"}
            else:
                response = {"message": f"已完成任务 #{task_id}：{row['title']}"}

            self._execute(
                conn,
                """
                INSERT INTO task_done_occurrences
                    (user_id, source_task_id, source_done_event_id, next_task_id,
                     response_status, response_json, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id, task_id, source_done_event_id, next_task_id,
                    200, json.dumps(response, ensure_ascii=False), encode_dt(now),
                ),
            )
            completed_row = self._execute(
                conn,
                "SELECT * FROM tasks WHERE id = %s AND user_id = %s",
                (task_id, user_id),
            ).fetchone()

        return (
            row_to_task(completed_row) if completed_row else None,
            True,
            row_to_task(next_row) if next_row else None,
        )

    def complete_recurring_task(self, task_id: int, *, user_id: str | None = None) -> Task | None:
        task = self.update_status(task_id, TaskStatus.DONE, user_id=user_id)
        if task is None or not task.recurrence:
            return task
        log.info("recurring task #%d completed, creating next instance", task_id)
        next_due = _next_recurrence_due(task.due_at, task.recurrence)
        next_task = self.create_task(
            task.title,
            due_at=next_due,
            priority=task.priority,
            estimated_minutes=task.estimated_minutes,
            notes=task.notes,
            recurrence=task.recurrence,
            user_id=task.user_id or DEFAULT_USER,
        )
        return next_task

    # ── subtasks ──────────────────────────────────────────────────────

    def get_subtasks(self, parent_task_id: int, *, user_id: str = DEFAULT_USER) -> list[Task]:
        log.debug("get_subtasks parent=%d user=%r", parent_task_id, user_id)
        with self._connect() as conn:
            cur = self._execute(
                conn,
                "SELECT * FROM tasks WHERE parent_task_id = %s AND user_id = %s ORDER BY id",
                (parent_task_id, user_id),
            )
            rows = cur.fetchall()
        return [row_to_task(row) for row in rows]

    def get_task_with_subtasks(self, task_id: int, *, user_id: str = DEFAULT_USER) -> Task | None:
        task = self._get_task(task_id)
        if not task or task.user_id != user_id:
            return None
        subtasks = self.get_subtasks(task_id, user_id=user_id)
        return Task(
            id=task.id,
            title=task.title,
            status=task.status,
            priority=task.priority,
            due_at=task.due_at,
            estimated_minutes=task.estimated_minutes,
            notes=task.notes,
            parent_task_id=task.parent_task_id,
            recurrence=task.recurrence,
            user_id=task.user_id,
            created_at=task.created_at,
            updated_at=task.updated_at,
            tags=task.tags,
            subtasks=subtasks,
            relations=task.relations,
        )

    def create_subtask(
        self,
        parent_task_id: int,
        title: str,
        *,
        due_at: datetime | None = None,
        priority: Priority = Priority.MEDIUM,
        estimated_minutes: int | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        user_id: str = DEFAULT_USER,
    ) -> Task:
        log.info("create_subtask parent=%d title=%r user=%r", parent_task_id, title, user_id)
        return self.create_task(
            title,
            due_at=due_at,
            priority=priority,
            estimated_minutes=estimated_minutes,
            notes=notes,
            parent_task_id=parent_task_id,
            tags=tags,
            user_id=user_id,
        )

    def bulk_create_subtasks(
        self,
        parent_task_id: int,
        subtasks: list[dict[str, Any]],
        *,
        user_id: str = DEFAULT_USER,
    ) -> list[Task]:
        log.info("bulk_create_subtasks parent=%d count=%d user=%r", parent_task_id, len(subtasks), user_id)
        created_tasks = []
        for subtask_data in subtasks:
            task = self.create_subtask(
                parent_task_id,
                title=subtask_data["title"],
                due_at=subtask_data.get("due_at"),
                priority=Priority(subtask_data.get("priority", "medium")),
                estimated_minutes=subtask_data.get("estimated_minutes"),
                notes=subtask_data.get("notes"),
                tags=subtask_data.get("tags"),
                user_id=user_id,
            )
            created_tasks.append(task)
        return created_tasks

    def get_parent_task(self, task_id: int, *, user_id: str = DEFAULT_USER) -> Task | None:
        task = self._get_task(task_id)
        if not task or task.parent_task_id is None:
            return None
        return self._get_task(task.parent_task_id)

    # ── task relations ──────────────────────────────────────────────────────

    def add_task_relation(
        self,
        source_task_id: int,
        target_task_id: int,
        relation_type: TaskRelationType,
        *,
        user_id: str = DEFAULT_USER,
    ) -> TaskRelation | None:
        now = utcnow()
        log.info("add_task_relation source=%d target=%d type=%s user=%r",
                 source_task_id, target_task_id, relation_type.value, user_id)
        try:
            with self._connect() as conn:
                self._execute(
                    conn,
                    """
                    INSERT INTO task_relations
                    (source_task_id, target_task_id, relation_type, user_id, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (source_task_id, target_task_id, relation_type.value, user_id, encode_dt(now)),
                )
                relation_id = int(conn.insert_id())
                cur = self._execute(conn, "SELECT * FROM task_relations WHERE id = %s", (relation_id,))
                row = cur.fetchone()
            return row_to_task_relation(row)
        except Exception as exc:
            import pymysql
            if isinstance(exc, pymysql.err.IntegrityError) and exc.args[0] == 1062:
                log.warning("task relation already exists")
                return None
            raise

    def remove_task_relation(
        self,
        source_task_id: int,
        target_task_id: int,
        relation_type: TaskRelationType,
        *,
        user_id: str = DEFAULT_USER,
    ) -> bool:
        log.info("remove_task_relation source=%d target=%d type=%s user=%r",
                 source_task_id, target_task_id, relation_type.value, user_id)
        with self._connect() as conn:
            cur = self._execute(
                conn,
                """
                DELETE FROM task_relations
                WHERE source_task_id = %s AND target_task_id = %s AND relation_type = %s AND user_id = %s
                """,
                (source_task_id, target_task_id, relation_type.value, user_id),
            )
        return cur.rowcount > 0

    def get_task_relations(
        self,
        task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> list[TaskRelation]:
        log.debug("get_task_relations task=%d user=%r", task_id, user_id)
        with self._connect() as conn:
            cur = self._execute(
                conn,
                """
                SELECT * FROM task_relations
                WHERE (source_task_id = %s OR target_task_id = %s) AND user_id = %s
                ORDER BY created_at
                """,
                (task_id, task_id, user_id),
            )
            rows = cur.fetchall()
        return [row_to_task_relation(row) for row in rows]

    def get_dependencies(
        self,
        task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> list[Task]:
        log.debug("get_dependencies task=%d user=%r", task_id, user_id)
        with self._connect() as conn:
            cur = self._execute(
                conn,
                """
                SELECT t.* FROM tasks t
                INNER JOIN task_relations r ON t.id = r.target_task_id
                WHERE r.source_task_id = %s AND r.relation_type = %s AND r.user_id = %s
                """,
                (task_id, TaskRelationType.DEPENDS_ON.value, user_id),
            )
            rows = cur.fetchall()
        return [row_to_task(row) for row in rows]

    def get_dependents(
        self,
        task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> list[Task]:
        log.debug("get_dependents task=%d user=%r", task_id, user_id)
        with self._connect() as conn:
            cur = self._execute(
                conn,
                """
                SELECT t.* FROM tasks t
                INNER JOIN task_relations r ON t.id = r.source_task_id
                WHERE r.target_task_id = %s AND r.relation_type = %s AND r.user_id = %s
                """,
                (task_id, TaskRelationType.DEPENDS_ON.value, user_id),
            )
            rows = cur.fetchall()
        return [row_to_task(row) for row in rows]

    def get_related_tasks(
        self,
        task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> list[Task]:
        log.debug("get_related_tasks task=%d user=%r", task_id, user_id)
        related_task_ids: set[int] = set()
        with self._connect() as conn:
            cur = self._execute(
                conn,
                """
                SELECT target_task_id FROM task_relations
                WHERE source_task_id = %s AND relation_type = %s AND user_id = %s
                """,
                (task_id, TaskRelationType.RELATES_TO.value, user_id),
            )
            for row in cur.fetchall():
                related_task_ids.add(row["target_task_id"])
            cur = self._execute(
                conn,
                """
                SELECT source_task_id FROM task_relations
                WHERE target_task_id = %s AND relation_type = %s AND user_id = %s
                """,
                (task_id, TaskRelationType.RELATES_TO.value, user_id),
            )
            for row in cur.fetchall():
                related_task_ids.add(row["source_task_id"])
        if not related_task_ids:
            return []
        with self._connect() as conn:
            placeholders = ", ".join("%s" for _ in related_task_ids)
            query = f"SELECT * FROM tasks WHERE id IN ({placeholders}) AND user_id = %s"
            cur = self._execute(conn, query, list(related_task_ids) + [user_id])
            rows = cur.fetchall()
        return [row_to_task(row) for row in rows]

    def add_dependency(
        self,
        task_id: int,
        depends_on_task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> TaskRelation | None:
        return self.add_task_relation(
            task_id, depends_on_task_id, TaskRelationType.DEPENDS_ON, user_id=user_id
        )

    def remove_dependency(
        self,
        task_id: int,
        depends_on_task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> bool:
        return self.remove_task_relation(
            task_id, depends_on_task_id, TaskRelationType.DEPENDS_ON, user_id=user_id
        )

    def is_task_blocked(
        self,
        task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> bool:
        dependencies = self.get_dependencies(task_id, user_id=user_id)
        for dep in dependencies:
            if dep.status != TaskStatus.DONE:
                return True
        return False

    # ── tags ──────────────────────────────────────────────────────

    def get_all_tags(self, *, user_id: str = DEFAULT_USER) -> list[str]:
        log.info("get_all_tags user=%r", user_id)
        with self._connect() as conn:
            cur = self._execute(
                conn,
                "SELECT tags FROM tasks WHERE user_id = %s AND tags IS NOT NULL",
                (user_id,),
            )
            rows = cur.fetchall()
        all_tags: set[str] = set()
        for row in rows:
            tags = _deserialize_tags(row["tags"])
            if tags:
                all_tags.update(tags)
        return sorted(list(all_tags))

    def get_tasks_by_tag(
        self, tag: str, *, user_id: str = DEFAULT_USER, status: TaskStatus | None = None
    ) -> list[Task]:
        log.info("get_tasks_by_tag tag=%r user=%r", tag, user_id)
        tag_lower = tag.strip().lower()
        with self._connect() as conn:
            if status:
                cur = self._execute(
                    conn,
                    "SELECT * FROM tasks WHERE user_id = %s AND status = %s ORDER BY due_at IS NULL, due_at, id",
                    (user_id, status.value),
                )
            else:
                cur = self._execute(
                    conn,
                    "SELECT * FROM tasks WHERE user_id = %s ORDER BY due_at IS NULL, due_at, id",
                    (user_id,),
                )
            rows = cur.fetchall()
        tasks = [row_to_task(row) for row in rows]
        return [
            t for t in tasks
            if t.tags and any(tag_lower == t_tag.lower() for t_tag in t.tags)
        ]

    # ── batch operations ──────────────────────────────────────────────────────

    def batch_update_status(
        self, task_ids: list[int], status: TaskStatus, *, user_id: str = DEFAULT_USER
    ) -> int:
        log.info("batch_update_status task_ids=%r status=%s user=%r", task_ids, status.value, user_id)
        updated = 0
        with self._connect() as conn:
            for task_id in task_ids:
                cur = self._execute(
                    conn,
                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND user_id = %s AND status != %s",
                    (status.value, encode_dt(utcnow()), task_id, user_id, status.value),
                )
                if cur.rowcount > 0:
                    updated += 1
                    self._execute(
                        conn,
                        "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                        (task_id, "status_changed", status.value, encode_dt(utcnow())),
                    )
        log.info("batch_update_status updated %d tasks", updated)
        return updated

    def batch_add_tags(
        self, task_ids: list[int], tags: list[str], *, user_id: str = DEFAULT_USER
    ) -> int:
        log.info("batch_add_tags task_ids=%r tags=%r user=%r", task_ids, tags, user_id)
        updated = 0
        with self._connect() as conn:
            for task_id in task_ids:
                cur = self._execute(
                    conn,
                    "SELECT tags FROM tasks WHERE id = %s AND user_id = %s",
                    (task_id, user_id),
                )
                row = cur.fetchone()
                if row is None:
                    continue
                existing_tags = _deserialize_tags(row["tags"]) or []
                combined_tags = list(set(existing_tags + tags))
                new_tags_str = _serialize_tags(combined_tags)
                self._execute(
                    conn,
                    "UPDATE tasks SET tags = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                    (new_tags_str, encode_dt(utcnow()), task_id, user_id),
                )
                updated += 1
        log.info("batch_add_tags updated %d tasks", updated)
        return updated

    # ── memory ─────────────────────────────────────────────────────

    def set_memory(self, key: str, value: str, user_id: str = DEFAULT_USER) -> None:
        now = utcnow()
        log.info("set_memory user=%r key=%r", user_id, key)
        with self._connect() as conn:
            self._execute(
                conn,
                """
                INSERT INTO user_memory (user_id, `key`, value, updated_at)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    value = VALUES(value),
                    updated_at = VALUES(updated_at)
                """,
                (user_id, key, value, encode_dt(now)),
            )

    def get_memory(self, key: str, user_id: str = DEFAULT_USER) -> str | None:
        with self._connect() as conn:
            cur = self._execute(
                conn,
                "SELECT value FROM user_memory WHERE user_id = %s AND `key` = %s",
                (user_id, key),
            )
            row = cur.fetchone()
        return row["value"] if row else None

    def get_all_memory(self, user_id: str = DEFAULT_USER) -> dict[str, str]:
        with self._connect() as conn:
            cur = self._execute(
                conn,
                "SELECT `key`, value FROM user_memory WHERE user_id = %s",
                (user_id,),
            )
            rows = cur.fetchall()
        return {row["key"]: row["value"] for row in rows}

    # ── search ───────────────────────────────────────────────────────

    def search_tasks(
        self, query: str, *, user_id: str = DEFAULT_USER, status: TaskStatus | None = None
    ) -> list[Task]:
        log.info("search_tasks q=%r user=%r", query, user_id)
        like = f"%{query}%"
        with self._connect() as conn:
            cur = self._execute(
                conn,
                """
                SELECT * FROM tasks WHERE user_id = %s
                AND (title LIKE %s OR notes LIKE %s OR tags LIKE %s)
                ORDER BY due_at IS NULL, due_at, id
                """,
                (user_id, like, like, like),
            )
            rows = cur.fetchall()
        return [row_to_task(row) for row in rows]

    # ── export / import ──────────────────────────────────────────────

    def export_user_data(self, user_id: str = DEFAULT_USER) -> dict:
        from .backup import export_user_data

        return export_user_data(self, user_id, "mysql")

    def import_user_data(self, data: dict, user_id: str = DEFAULT_USER) -> int:
        from .backup import import_user_data

        return import_user_data(self, data, user_id, "mysql")

    def import_user_data_with_summary(self, data: dict, user_id: str = DEFAULT_USER) -> dict[str, Any]:
        """导入 v1 备份并返回凭据 memory 排除摘要。"""
        from .backup import import_user_data_with_summary

        return import_user_data_with_summary(self, data, user_id, "mysql")

    def restore_user_data(self, data: dict, user_id: str = DEFAULT_USER) -> dict[str, Any]:
        """原子恢复 v2 备份到当前用户的空数据域。"""
        from .backup import restore_user_data

        return restore_user_data(self, data, user_id, "mysql")

    # ── heartbeat / 心跳功能 ─────────────────────────────────

    def get_heartbeat_config(self, user_id: str = DEFAULT_USER) -> dict:
        config_str = self.get_memory("heartbeat_config", user_id=user_id)
        if config_str:
            import json
            try:
                return json.loads(config_str)
            except json.JSONDecodeError:
                pass
        return {
            "enabled": False,
            "start_hour": 9,
            "end_hour": 21,
            "interval_hours": 4,
            "last_heartbeat_at": None,
        }

    def set_heartbeat_config(
        self,
        enabled: bool | None = None,
        start_hour: int | None = None,
        end_hour: int | None = None,
        interval_hours: int | None = None,
        user_id: str = DEFAULT_USER,
    ) -> dict:
        config = self.get_heartbeat_config(user_id=user_id)
        if enabled is not None:
            config["enabled"] = enabled
        if start_hour is not None:
            config["start_hour"] = max(0, min(23, start_hour))
        if end_hour is not None:
            config["end_hour"] = max(0, min(23, end_hour))
        if interval_hours is not None:
            config["interval_hours"] = max(1, min(24, interval_hours))
        import json
        self.set_memory("heartbeat_config", json.dumps(config), user_id=user_id)
        return config

    def update_last_heartbeat(self, user_id: str = DEFAULT_USER) -> dict:
        config = self.get_heartbeat_config(user_id=user_id)
        config["last_heartbeat_at"] = utcnow().isoformat()
        import json
        self.set_memory("heartbeat_config", json.dumps(config), user_id=user_id)
        return config

    def should_trigger_heartbeat(self, user_id: str = DEFAULT_USER) -> bool:
        config = self.get_heartbeat_config(user_id=user_id)
        if not config["enabled"]:
            return False
        now = datetime.now().astimezone()
        current_hour = now.hour
        if current_hour < config["start_hour"] or current_hour > config["end_hour"]:
            return False
        if config["last_heartbeat_at"]:
            last_heartbeat = datetime.fromisoformat(config["last_heartbeat_at"])
            last_heartbeat = (
                last_heartbeat.astimezone()
                if last_heartbeat.tzinfo
                else last_heartbeat.replace(tzinfo=timezone.utc).astimezone()
            )
            hours_since = (now - last_heartbeat).total_seconds() / 3600
            if hours_since < config["interval_hours"]:
                return False
        return True

    # ── focus sessions / 专注记录 ─────────────────────────────────

    def record_focus_session(
        self,
        task_id: int | None,
        duration_minutes: int,
        *,
        user_id: str = DEFAULT_USER,
        actual_seconds: int | None = None,
        planned_minutes: int | None = None,
        started_at: datetime | None = None,
        outcome: str | None = None,
        session_id: str | None = None,
        ended_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """记录专注时段；新格式保留 client-frozen UTC 时间并按用户幂等。"""
        import json
        from .errors import FocusTaskNotFound, IdempotencyConflict
        from .focus_utils import focus_result, same_focus_payload, validate_focus_session

        log.info("record_focus_session task=%s duration=%d user=%r", task_id, duration_minutes, user_id)
        now = utcnow()
        if actual_seconds is None:
            payload = {"duration_minutes": int(duration_minutes)}
        else:
            if task_id is None:
                raise FocusTaskNotFound
            effective_planned_minutes = duration_minutes if planned_minutes is None else planned_minutes
            validated_planned_minutes, normalized_started, normalized_ended = validate_focus_session(
                actual_seconds,
                effective_planned_minutes,
                started_at=started_at,
                ended_at=ended_at,
                outcome=outcome,
                session_id=session_id,
            )
            payload = {
                "duration_minutes": round(actual_seconds / 60, 2),
                "actual_seconds": actual_seconds,
                "planned_minutes": validated_planned_minutes,
                "started_at": encode_dt(normalized_started),
                "ended_at": encode_dt(normalized_ended),
                "outcome": outcome,
                "session_id": session_id,
            }
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            if session_id and actual_seconds is not None:
                # The advisory key is scoped to this user and session. The lock
                # is connection-owned and released when _connect closes.
                import hashlib

                lock_name = "focus:" + hashlib.sha256(f"{user_id}\0{session_id}".encode("utf-8")).hexdigest()[:48]
                lock = self._execute(
                    conn,
                    "SELECT GET_LOCK(%s, %s) AS acquired",
                    (lock_name, 30),
                ).fetchone()
                if not lock or lock.get("acquired") != 1:
                    raise TimeoutError("timed out waiting for focus session lock")
                # Start the transaction after obtaining the connection-level
                # lock so its reads see the preceding holder's committed event.
                conn.begin()
            elif actual_seconds is not None:
                conn.begin()
            if actual_seconds is not None:
                if task_id is None:
                    raise FocusTaskNotFound
                owner = self._execute(
                    conn, "SELECT id FROM tasks WHERE id = %s AND user_id = %s", (task_id, user_id)
                ).fetchone()
                if owner is None:
                    raise FocusTaskNotFound
            if session_id and actual_seconds is not None:
                candidates = self._execute(
                    conn,
                    """
                    SELECT e.task_id, e.payload FROM task_events e
                    JOIN tasks t ON e.task_id = t.id
                    WHERE e.event_type = %s AND t.user_id = %s
                    """,
                    ("focus_session", user_id),
                ).fetchall()
                for candidate in candidates:
                    try:
                        existing_payload = json.loads(candidate["payload"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if existing_payload.get("session_id") == session_id:
                        if not same_focus_payload(candidate["task_id"], existing_payload, task_id, payload):
                            raise IdempotencyConflict
                        return focus_result(candidate["task_id"], existing_payload)
            self._execute(
                conn,
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                (task_id, "focus_session", payload_json, encode_dt(now)),
            )
        return focus_result(task_id, payload) if actual_seconds is not None else None

    def get_focus_sessions(self, *, user_id: str = DEFAULT_USER) -> list[dict]:
        import json
        cutoff = (utcnow() - timedelta(days=30)).isoformat()
        with self._connect() as conn:
            cur = self._execute(
                conn,
                """
                SELECT e.task_id, e.payload, e.created_at
                FROM task_events e
                INNER JOIN tasks t ON e.task_id = t.id
                WHERE e.event_type = %s AND t.user_id = %s
                AND e.created_at >= %s
                ORDER BY e.created_at DESC
                """,
                ("focus_session", user_id, cutoff),
            )
            rows = cur.fetchall()
        sessions = []
        for row in rows:
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
            except (TypeError, json.JSONDecodeError):
                payload = {}
            raw_actual_seconds = payload.get("actual_seconds")
            try:
                actual_seconds = int(raw_actual_seconds) if raw_actual_seconds is not None else None
                if actual_seconds is not None and actual_seconds < 0:
                    actual_seconds = None
            except (TypeError, ValueError):
                actual_seconds = None
            started_at = decode_dt(payload.get("started_at")) if payload.get("started_at") else None
            from .focus_utils import parse_timestamp
            sessions.append({
                "task_id": row["task_id"],
                "duration_minutes": actual_seconds / 60 if actual_seconds is not None else payload.get("duration_minutes", 0),
                "planned_minutes": payload.get("planned_minutes", payload.get("duration_minutes", 0)),
                "actual_seconds": actual_seconds,
                "is_actual": actual_seconds is not None,
                "outcome": payload.get("outcome", "completed" if actual_seconds is not None else "legacy"),
                "session_id": payload.get("session_id"),
                "started_at": started_at or (decode_dt(row["created_at"]) if row["created_at"] else None),
                "ended_at": parse_timestamp(payload.get("ended_at")),
            })
        return sessions

    def get_review_data(self, *, user_id: str = DEFAULT_USER) -> dict[str, list[dict[str, Any]]]:
        """Read owner-scoped status/focus events in one MySQL snapshot."""
        with self._connect() as conn:
            conn.begin()
            cur = self._execute(
                conn,
                """
                SELECT e.id AS event_id, e.task_id, e.payload AS status_value,
                       e.created_at AS completed_at, t.title,
                       t.estimated_minutes AS estimated_minutes_reference
                FROM task_events e JOIN tasks t ON e.task_id = t.id
                WHERE t.user_id = %s AND e.event_type = 'status_changed'
                ORDER BY e.id
                """,
                (user_id,),
            )
            completed = cur.fetchall()
            cur = self._execute(
                conn,
                """
                SELECT e.task_id, e.payload, e.created_at
                FROM task_events e JOIN tasks t ON e.task_id = t.id
                WHERE t.user_id = %s AND e.event_type = 'focus_session'
                """,
                (user_id,),
            )
            focus = cur.fetchall()
        return {
            "status_events": [dict(row) for row in completed],
            "focus_sessions": [dict(row) for row in focus],
        }


def _import_cursor_class(name: str) -> Any:
    """延迟导入 pymysql DictCursor，避免顶层导入失败。"""
    import importlib

    module_name, class_name = name.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def _split_schema(schema: str) -> list[str]:
    """按分号拆分 MySQL schema 语句（不破坏存储过程等）。"""
    stmts = []
    for stmt in schema.split(";"):
        stmt = stmt.strip()
        if stmt:
            stmts.append(stmt)
    return stmts
