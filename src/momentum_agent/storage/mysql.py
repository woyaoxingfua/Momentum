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
    due_at TEXT,
    estimated_minutes INT,
    notes TEXT,
    parent_task_id INT REFERENCES tasks(id) ON DELETE CASCADE,
    recurrence TEXT,
    user_id VARCHAR(64) NOT NULL DEFAULT 'default' REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
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
    key VARCHAR(128) NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS task_events (
    id INT AUTO_INCREMENT PRIMARY KEY,
    task_id INT REFERENCES tasks(id) ON DELETE CASCADE,
    event_type VARCHAR(32) NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL,
    INDEX idx_events_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _parse_mysql_url(url: str) -> dict[str, Any]:
    """把 mysql:// URL 解析成 pymysql.connect 参数。"""
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": parsed.username or "root",
        "password": parsed.password or "",
        "database": parsed.path.lstrip("/") or None,
        "charset": "utf8mb4",
        "cursorclass": "pymysql.cursors.DictCursor",
        "autocommit": False,
    }


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

    def update_status(
        self, task_id: int, status: TaskStatus, *, user_id: str | None = None
    ) -> Task | None:
        now = utcnow()
        log.info("update_status task=%d status=%s user=%r", task_id, status.value, user_id)
        old_parent_id = None
        has_subtasks = False
        with self._connect() as conn:
            if user_id is not None:
                cur = self._execute(
                    conn,
                    "SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s",
                    (task_id, user_id),
                )
            else:
                cur = self._execute(conn, "SELECT parent_task_id FROM tasks WHERE id = %s", (task_id,))
            old = cur.fetchone()
            if old:
                old_parent_id = old["parent_task_id"]
            if user_id is not None and old is None:
                log.warning("update_status: task #%d not owned by %r", task_id, user_id)
                return None
            if user_id is not None:
                self._execute(
                    conn,
                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                    (status.value, encode_dt(now), task_id, user_id),
                )
            else:
                self._execute(
                    conn,
                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s",
                    (status.value, encode_dt(now), task_id),
                )
            self._execute(
                conn,
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                (task_id, "status_changed", status.value, encode_dt(now)),
            )
            cur = self._execute(conn, "SELECT * FROM tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
            cur = self._execute(conn, "SELECT id FROM tasks WHERE parent_task_id = %s", (task_id,))
            subtasks = cur.fetchall()
            has_subtasks = len(subtasks) > 0
        task = row_to_task(row) if row else None

        if task and status == TaskStatus.DONE and has_subtasks:
            self._complete_all_subtasks(task_id, user_id)

        if task and status == TaskStatus.DONE and old_parent_id:
            self._auto_complete_parent(old_parent_id)

        return task

    def _complete_all_subtasks(self, parent_task_id: int, user_id: str | None) -> None:
        log.info("auto-completing all subtasks for parent task #%d", parent_task_id)
        now = utcnow()
        with self._connect() as conn:
            if user_id:
                self._execute(
                    conn,
                    """
                    UPDATE tasks SET status = %s, updated_at = %s
                    WHERE parent_task_id = %s AND status != %s AND user_id = %s
                    """,
                    (TaskStatus.DONE.value, encode_dt(now), parent_task_id, TaskStatus.DONE.value, user_id),
                )
            else:
                self._execute(
                    conn,
                    """
                    UPDATE tasks SET status = %s, updated_at = %s
                    WHERE parent_task_id = %s AND status != %s
                    """,
                    (TaskStatus.DONE.value, encode_dt(now), parent_task_id, TaskStatus.DONE.value),
                )
            self._execute(
                conn,
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                (parent_task_id, "subtasks_completed", None, encode_dt(now)),
            )

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
            return self._get_task(task_id)
        sets.append("updated_at = %s")
        params.append(encode_dt(now))
        params.append(task_id)
        params.append(user_id)
        with self._connect() as conn:
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
            cur = self._execute(conn, "SELECT * FROM tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
        return row_to_task(row) if row else None

    def postpone_task(self, task_id: int, days: int, *, user_id: str | None = None) -> Task | None:
        task = self._get_task(task_id)
        if not task:
            log.warning("postpone_task: task #%d not found", task_id)
            return None
        if user_id is not None and task.user_id != user_id:
            log.warning("postpone_task: task #%d not owned by %r", task_id, user_id)
            return None
        if task.due_at is None:
            return task
        new_due = task.due_at + timedelta(days=days)
        log.info("postpone task=%d days=%d new_due=%s", task_id, days, new_due.isoformat())
        return self.update_task(task_id, due_at=new_due, user_id=user_id or DEFAULT_USER)

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
                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                    (status.value, encode_dt(utcnow()), task_id, user_id),
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
                INSERT INTO user_memory (user_id, key, value, updated_at)
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
                "SELECT value FROM user_memory WHERE user_id = %s AND key = %s",
                (user_id, key),
            )
            row = cur.fetchone()
        return row["value"] if row else None

    def get_all_memory(self, user_id: str = DEFAULT_USER) -> dict[str, str]:
        with self._connect() as conn:
            cur = self._execute(
                conn,
                "SELECT key, value FROM user_memory WHERE user_id = %s",
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
        tasks = self.list_tasks(status=None, user_id=user_id)
        memory = self.get_all_memory(user_id=user_id)
        log.info("export user=%r tasks=%d", user_id, len(tasks))
        return {
            "version": "1.0",
            "exported_at": utcnow().isoformat(),
            "user_id": user_id,
            "tasks": [
                {
                    "title": t.title,
                    "status": t.status.value,
                    "priority": t.priority.value,
                    "due_at": t.due_at.isoformat() if t.due_at else None,
                    "estimated_minutes": t.estimated_minutes,
                    "notes": t.notes,
                    "parent_task_id": t.parent_task_id,
                    "recurrence": t.recurrence,
                    "created_at": t.created_at.isoformat(),
                }
                for t in tasks
            ],
            "memory": memory,
        }

    def import_user_data(self, data: dict, user_id: str = DEFAULT_USER) -> int:
        imported = 0
        for item in data.get("tasks", []):
            due_at = datetime.fromisoformat(item["due_at"]) if item.get("due_at") else None
            self.create_task(
                item["title"],
                due_at=due_at,
                priority=Priority(item.get("priority", "medium")),
                estimated_minutes=item.get("estimated_minutes"),
                notes=item.get("notes"),
                recurrence=item.get("recurrence"),
                user_id=user_id,
            )
            imported += 1
        for key, value in data.get("memory", {}).items():
            self.set_memory(key, value, user_id=user_id)
        log.info("import user=%r tasks=%d", user_id, imported)
        return imported

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
    ) -> None:
        log.info("record_focus_session task=%s duration=%d user=%r", task_id, duration_minutes, user_id)
        now = utcnow()
        with self._connect() as conn:
            self._execute(
                conn,
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s)",
                (task_id, "focus_session", f'{{"duration_minutes": {duration_minutes}}}', encode_dt(now)),
            )

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
            payload = json.loads(row["payload"]) if row["payload"] else {}
            sessions.append({
                "task_id": row["task_id"],
                "duration_minutes": payload.get("duration_minutes", 0),
                "started_at": decode_dt(row["created_at"]) if row["created_at"] else None,
            })
        return sessions


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
