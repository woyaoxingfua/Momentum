from __future__ import annotations

import itertools
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from ..logger import get_logger, log_db_query, log_security_event
from ..models import Priority, Task, TaskStatus, TaskRelation, TaskRelationType

log = get_logger("storage")

# Session 有效期：7 天
SESSION_LIFETIME = timedelta(days=7)

__all__ = [
    "SQLiteTaskStore",
    "DEFAULT_USER",
    "utcnow",
    "encode_dt",
    "decode_dt",
    "row_to_task",
    "row_to_task_relation",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',
    priority TEXT NOT NULL DEFAULT 'medium',
    due_at TEXT,
    estimated_minutes INTEGER,
    notes TEXT,
    parent_task_id INTEGER REFERENCES tasks(id),
    recurrence TEXT,
    user_id TEXT NOT NULL DEFAULT 'default' REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    tags TEXT
);

CREATE TABLE IF NOT EXISTS task_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_task_id INTEGER NOT NULL REFERENCES tasks(id),
    target_task_id INTEGER NOT NULL REFERENCES tasks(id),
    relation_type TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'default' REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE(source_task_id, target_task_id, relation_type)
);

CREATE TABLE IF NOT EXISTS user_memory (
    user_id TEXT NOT NULL DEFAULT 'local',
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    event_type TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_postpone_idempotency (
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS task_done_idempotency (
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS task_done_occurrences (
    user_id TEXT NOT NULL,
    source_task_id INTEGER NOT NULL,
    source_done_event_id INTEGER NOT NULL,
    next_task_id INTEGER,
    response_status INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, source_task_id, source_done_event_id)
);
"""

DEFAULT_USER = "default"

# 内存库名称编号：保证同一进程内每个内存库唯一（不要用 id()，会被复用）
_memory_store_counter = itertools.count(1)


class SQLiteTaskStore:
    # 已初始化 schema 的数据库路径缓存，避免每次实例化都执行 migration
    _schema_initialized: set[str] = set()

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._memory_uri: str | None = None
        self._memory_anchor: sqlite3.Connection | None = None
        if str(db_path) == ":memory:":
            # 每个连接各自独立的内存库会让建表结果立刻消失（list_tasks 报 no such table）。
            # 改用命名 shared-cache 内存库，并保留一个常驻「锚」连接维持数据存活。
            # 用进程内单调递增的编号而不是 id(self)：CPython 会复用已回收对象的 id，
            # 一旦复用一个仍然存活（例如连接泄漏）的旧内存库，就会串到别人的数据。
            self._memory_uri = (
                f"file:momentum-memory-{os.getpid()}-{next(_memory_store_counter)}?mode=memory&cache=shared"
            )
            self._memory_anchor = sqlite3.connect(self._memory_uri, uri=True, check_same_thread=False)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        log.info("store opened: %s", self.db_path)

    def _init_schema(self) -> None:
        if self._memory_uri:
            # 内存库随实例存在，不能复用类级缓存，否则第二个实例会跳过建表
            with self._connect() as conn:
                conn.executescript(SCHEMA)
                self._migrate(conn)
                self._ensure_default_user(conn)
            return
        key = str(self.db_path.resolve())
        if key in SQLiteTaskStore._schema_initialized:
            log.debug("schema already initialized for %s", self.db_path)
            return
        log.debug("initializing schema")
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            self._ensure_default_user(conn)
        SQLiteTaskStore._schema_initialized.add(key)

    @classmethod
    def ensure_schema(cls, db_path: str | Path) -> "SQLiteTaskStore":
        """应用启动时显式调用，确保 schema 已初始化。"""
        return cls(db_path)

    def open_connection(self) -> sqlite3.Connection:
        """新建一个指向本 store 数据库的连接；内存库返回共享库连接。"""
        if self._memory_uri:
            conn = sqlite3.connect(self._memory_uri, uri=True)
        else:
            conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        """释放内存库的常驻连接；文件库无需调用。"""
        anchor = self._memory_anchor
        self._memory_anchor = None
        if anchor is not None:
            try:
                anchor.close()
            except sqlite3.Error:
                pass

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.open_connection()
        try:
            yield conn
            conn.commit()
        except sqlite3.Error as e:
            log.error("database error: %s", e)
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_default_user(self, conn: sqlite3.Connection) -> None:
        from ..auth import hash_password
        existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing == 0:
            conn.execute(
                "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (DEFAULT_USER, "默认用户", hash_password("momentum"), encode_dt(utcnow())),
            )
            log.info("created default user (password: momentum)")

    def _migrate(self, conn: sqlite3.Connection) -> None:
        # 迁移 users 表：添加 password_hash 列（兼容旧数据库）
        from ..auth import hash_password
        user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "password_hash" not in user_cols:
            log.info("migration: adding password_hash column to users")
            conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
            default_hash = hash_password("momentum")
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE password_hash IS NULL",
                (default_hash,),
            )
        # 迁移 tasks 表：添加各列
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "recurrence" not in cols:
            log.info("migration: adding recurrence column")
            conn.execute("ALTER TABLE tasks ADD COLUMN recurrence TEXT")
        if "user_id" not in cols:
            log.info("migration: adding user_id column")
            conn.execute("ALTER TABLE tasks ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default' REFERENCES users(id)")
        if "tags" not in cols:
            log.info("migration: adding tags column")
            conn.execute("ALTER TABLE tasks ADD COLUMN tags TEXT")

        # 迁移 sessions 表：添加过期时间列
        session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "expires_at" not in session_cols:
            log.info("migration: adding expires_at column to sessions")
            conn.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT")
            # 给已有 session 一个默认过期时间：7 天后
            from ..auth import utcnow as auth_now
            default_expires = encode_dt(auth_now() + SESSION_LIFETIME)
            conn.execute("UPDATE sessions SET expires_at = ? WHERE expires_at IS NULL", (default_expires,))

    # ── auth ───────────────────────────────────────────────────────

    def register_user(self, user_id: str, display_name: str, password_hash: str) -> None:
        from ..auth import utcnow as auth_now
        log.info("register user=%r", user_id)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, display_name, password_hash, encode_dt(auth_now())),
            )

    def login_user(self, user_id: str, password: str) -> str | None:
        from ..auth import generate_token, utcnow as auth_now, verify_password
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not row:
                log_security_event("login_failed", user_id, "用户不存在")
                return None
            if not verify_password(password, row["password_hash"]):
                log_security_event("login_failed", user_id, "密码错误")
                return None
            token = generate_token()
            now = auth_now()
            conn.execute(
                "INSERT OR REPLACE INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, encode_dt(now), encode_dt(now + SESSION_LIFETIME)),
            )
        log.info("login user=%r", user_id)
        return token

    def validate_session(self, token: str) -> str | None:
        from ..auth import utcnow as auth_now
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, expires_at FROM sessions WHERE token = ?", (token,)
            ).fetchone()
            if not row:
                return None
            expires = decode_dt(row["expires_at"])
            if expires and expires < auth_now():
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                return None
            return row["user_id"]

    def logout_user(self, token: str | None) -> None:
        if not token:
            return
        log.info("logout token=...%s", token[-8:])
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        from ..auth import verify_password, hash_password
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not row or not verify_password(old_password, row["password_hash"]):
                return False
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(new_password), user_id),
            )
        log.info("password changed for user=%r", user_id)
        return True

    def list_users(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, display_name FROM users ORDER BY id").fetchall()
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
        priority = _coerce_priority(priority)
        now = utcnow()
        log.info("create_task title=%r user=%r priority=%s", title.strip(), user_id, priority.value)
        tags_str = _serialize_tags(tags)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    title, status, priority, due_at, estimated_minutes, notes,
                    parent_task_id, recurrence, user_id, created_at, updated_at, tags
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            task_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "created", None, encode_dt(now)),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        task = row_to_task(row)
        log.debug("created task #%d", task.id)
        return task

    def list_tasks(
        self, status: TaskStatus | None = TaskStatus.TODO, *, user_id: str = DEFAULT_USER
    ) -> list[Task]:
        log.debug("list_tasks status=%s user=%r", status, user_id)
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE user_id = ? ORDER BY due_at IS NULL, due_at, id",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? AND user_id = ? ORDER BY due_at IS NULL, due_at, id",
                    (status.value, user_id),
                ).fetchall()
        return [row_to_task(row) for row in rows]

    def list_completed_task_history(
        self, *, user_id: str = DEFAULT_USER, since: datetime
    ) -> list[dict[str, Any]]:
        """真实完成事件（payload=done），排除周期任务，供「历史常完成任务」聚合。"""
        log.debug("list_completed_task_history user=%r since=%s", user_id, since)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.id AS task_id, t.title AS title, t.priority AS priority,
                       t.estimated_minutes AS estimated_minutes, t.tags AS tags,
                       e.created_at AS completed_at
                FROM task_events e
                JOIN tasks t ON t.id = e.task_id
                WHERE t.user_id = ?
                  AND e.event_type = 'status_changed'
                  AND e.payload = 'done'
                  AND e.created_at >= ?
                  AND (t.recurrence IS NULL OR t.recurrence = '')
                ORDER BY e.created_at DESC, t.id DESC
                """,
                (user_id, encode_dt(since)),
            ).fetchall()
        return [
            {
                "task_id": int(row["task_id"]),
                "title": row["title"],
                "priority": row["priority"],
                "estimated_minutes": row["estimated_minutes"],
                "tags": _deserialize_tags(row["tags"]),
                "completed_at": decode_dt(row["completed_at"]),
            }
            for row in rows
        ]

    def _get_task(self, task_id: int) -> Task | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row_to_task(row) if row else None

    def get_task_for_user(self, task_id: int, user_id: str) -> Task | None:
        """按 ID 和 owner 一次查询任务，不暴露其他用户的记录。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            ).fetchone()
        return row_to_task(row) if row else None

    def update_status(
        self, task_id: int, status: TaskStatus, *, user_id: str | None = None
    ) -> Task | None:
        now = utcnow()
        log.info("update_status task=%d status=%s user=%r", task_id, status.value, user_id)
        with self._connect() as conn:
            # Acquire the writer lock before reading the prior state, so two
            # concurrent DONE requests cannot both append a completion event.
            conn.execute("BEGIN IMMEDIATE")
            if user_id is not None:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
                ).fetchone()
            else:
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                log.warning("update_status: task #%d not owned by %r", task_id, user_id)
                return None
            if row["status"] != status.value:
                if user_id is None:
                    conn.execute(
                        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                        (status.value, encode_dt(now), task_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                        (status.value, encode_dt(now), task_id, user_id),
                    )
                conn.execute(
                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, "status_changed", status.value, encode_dt(now)),
                )

                if status == TaskStatus.DONE:
                    queue = [task_id]
                    while queue:
                        completed_id = queue.pop(0)
                        if user_id is None:
                            completed = conn.execute(
                                "SELECT parent_task_id FROM tasks WHERE id = ?", (completed_id,)
                            ).fetchone()
                            children = conn.execute(
                                "SELECT id FROM tasks WHERE parent_task_id = ? AND status != ? ORDER BY id",
                                (completed_id, TaskStatus.DONE.value),
                            ).fetchall()
                        else:
                            completed = conn.execute(
                                "SELECT parent_task_id FROM tasks WHERE id = ? AND user_id = ?",
                                (completed_id, user_id),
                            ).fetchone()
                            children = conn.execute(
                                "SELECT id FROM tasks WHERE parent_task_id = ? AND status != ? AND user_id = ? ORDER BY id",
                                (completed_id, TaskStatus.DONE.value, user_id),
                            ).fetchall()
                        changed_children = 0
                        for child in children:
                            if user_id is None:
                                result = conn.execute(
                                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status != ?",
                                    (TaskStatus.DONE.value, encode_dt(now), child["id"], TaskStatus.DONE.value),
                                )
                            else:
                                result = conn.execute(
                                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status != ? AND user_id = ?",
                                    (TaskStatus.DONE.value, encode_dt(now), child["id"], TaskStatus.DONE.value, user_id),
                                )
                            if result.rowcount:
                                changed_children += 1
                                conn.execute(
                                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                                    (child["id"], "status_changed", TaskStatus.DONE.value, encode_dt(now)),
                                )
                                queue.append(child["id"])
                        if changed_children:
                            conn.execute(
                                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                                (completed_id, "subtasks_completed", None, encode_dt(now)),
                            )

                        parent_id = completed["parent_task_id"] if completed else None
                        if parent_id is None:
                            continue
                        if user_id is None:
                            parent = conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,)).fetchone()
                            sibling_states = conn.execute(
                                "SELECT status FROM tasks WHERE parent_task_id = ?", (parent_id,)
                            ).fetchall()
                        else:
                            parent = conn.execute(
                                "SELECT status FROM tasks WHERE id = ? AND user_id = ?", (parent_id, user_id)
                            ).fetchone()
                            sibling_states = conn.execute(
                                "SELECT status FROM tasks WHERE parent_task_id = ? AND user_id = ?",
                                (parent_id, user_id),
                            ).fetchall()
                        if parent and parent["status"] != TaskStatus.DONE.value and sibling_states and all(
                            sibling["status"] == TaskStatus.DONE.value for sibling in sibling_states
                        ):
                            if user_id is None:
                                result = conn.execute(
                                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status != ?",
                                    (TaskStatus.DONE.value, encode_dt(now), parent_id, TaskStatus.DONE.value),
                                )
                            else:
                                result = conn.execute(
                                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status != ? AND user_id = ?",
                                    (TaskStatus.DONE.value, encode_dt(now), parent_id, TaskStatus.DONE.value, user_id),
                                )
                            if result.rowcount:
                                conn.execute(
                                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                                    (parent_id, "status_changed", TaskStatus.DONE.value, encode_dt(now)),
                                )
                                queue.append(parent_id)
            final_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
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
        if priority is not None:
            priority = _coerce_priority(priority)
        now = utcnow()
        sets: list[str] = []
        params: list[object] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title.strip())
        if due_at is not None:
            sets.append("due_at = ?")
            params.append(encode_dt(due_at))
        if priority is not None:
            sets.append("priority = ?")
            params.append(priority.value)
        if estimated_minutes is not None:
            sets.append("estimated_minutes = ?")
            params.append(estimated_minutes)
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes)
        if tags is not None:
            sets.append("tags = ?")
            params.append(_serialize_tags(tags))
        if parent_task_id is not None:
            sets.append("parent_task_id = ?")
            params.append(parent_task_id)
        if not sets:
            return self.get_task_for_user(task_id, user_id)
        sets.append("updated_at = ?")
        params.append(encode_dt(now))
        params.append(task_id)
        params.append(user_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owned = conn.execute(
                "SELECT id FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
            ).fetchone()
            if owned is None:
                log.warning("update_task: task #%d not owned by %r", task_id, user_id)
                return None
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND user_id = ?", params)
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "updated", None, encode_dt(now)),
            )
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
            ).fetchone()
        return row_to_task(row) if row else None

    def postpone_task(self, task_id: int, days: int, *, user_id: str | None = None) -> Task | None:
        from .errors import TaskCannotBePostponed

        owner = user_id or DEFAULT_USER
        now = utcnow()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, owner)
            ).fetchone()
            if row is None:
                log.warning("postpone_task: task #%d not owned by %r", task_id, owner)
                return None
            task = row_to_task(row)
            if task.due_at is None or task.status not in (TaskStatus.TODO, TaskStatus.DOING):
                raise TaskCannotBePostponed
            new_due = task.due_at + timedelta(days=days)
            log.info("postpone task=%d days=%d new_due=%s", task_id, days, new_due.isoformat())
            conn.execute(
                "UPDATE tasks SET due_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (encode_dt(new_due), encode_dt(now), task_id, owner),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "updated", None, encode_dt(now)),
            )
            updated = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, owner)
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
        """Atomically postpone once and persist the exact API response by user/key."""
        import json

        owner = user_id
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                """
                SELECT request_fingerprint, response_status, response_json
                FROM task_postpone_idempotency
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (owner, idempotency_key),
            ).fetchone()
            if previous is not None:
                if previous["request_fingerprint"] != request_fingerprint:
                    return 409, {"error": "idempotency_conflict"}
                return int(previous["response_status"]), json.loads(previous["response_json"])

            if type(days) is not int or days <= 0:
                return 400, {"error": "days_invalid"}

            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, owner)
            ).fetchone()
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
                        return 400, {"error": "days_out_of_range"}
                    now = utcnow()
                    conn.execute(
                        "UPDATE tasks SET due_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                        (encode_dt(new_due), encode_dt(now), task_id, owner),
                    )
                    conn.execute(
                        "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                        (task_id, "updated", None, encode_dt(now)),
                    )
                    updated = conn.execute(
                        "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, owner)
                    ).fetchone()
                    if updated is None:
                        raise RuntimeError("postponed task disappeared before response persistence")
                    updated_task = row_to_task(updated)
                    due = updated_task.due_at.strftime("%Y-%m-%d %H:%M") if updated_task.due_at else "无截止"
                    status, response = 200, {
                        "message": f"任务 #{updated_task.id}「{updated_task.title}」已推迟至 {due}"
                    }

            conn.execute(
                """
                INSERT INTO task_postpone_idempotency
                    (user_id, idempotency_key, request_fingerprint, response_status, response_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (owner, idempotency_key, request_fingerprint, status,
                 json.dumps(response, ensure_ascii=False), encode_dt(utcnow())),
            )
        return status, response

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
            children = conn.execute(
                "SELECT * FROM tasks WHERE parent_task_id = ?", (parent_id,)
            ).fetchall()
        if children and all(row["status"] == TaskStatus.DONE.value for row in children):
            log.info("auto-completing parent task #%d", parent_id)
            self.update_status(parent_id, TaskStatus.DONE)

    def _cascade_done_in_transaction(self, conn: sqlite3.Connection, task_id: int, user_id: str, now: datetime) -> None:
        """Apply the existing descendant/ancestor DONE rules on the caller's transaction."""
        queue = [task_id]
        while queue:
            completed_id = queue.pop(0)
            completed = conn.execute(
                "SELECT parent_task_id FROM tasks WHERE id = ? AND user_id = ?",
                (completed_id, user_id),
            ).fetchone()
            children = conn.execute(
                "SELECT id FROM tasks WHERE parent_task_id = ? AND status != ? AND user_id = ? ORDER BY id",
                (completed_id, TaskStatus.DONE.value, user_id),
            ).fetchall()
            changed_children = 0
            for child in children:
                result = conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status != ? AND user_id = ?",
                    (TaskStatus.DONE.value, encode_dt(now), child["id"], TaskStatus.DONE.value, user_id),
                )
                if result.rowcount:
                    changed_children += 1
                    conn.execute(
                        "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                        (child["id"], "status_changed", TaskStatus.DONE.value, encode_dt(now)),
                    )
                    queue.append(child["id"])
            if changed_children:
                conn.execute(
                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                    (completed_id, "subtasks_completed", None, encode_dt(now)),
                )

            parent_id = completed["parent_task_id"] if completed else None
            if parent_id is None:
                continue
            parent = conn.execute(
                "SELECT status FROM tasks WHERE id = ? AND user_id = ?", (parent_id, user_id)
            ).fetchone()
            sibling_states = conn.execute(
                "SELECT status FROM tasks WHERE parent_task_id = ? AND user_id = ?",
                (parent_id, user_id),
            ).fetchall()
            if parent and parent["status"] != TaskStatus.DONE.value and sibling_states and all(
                sibling["status"] == TaskStatus.DONE.value for sibling in sibling_states
            ):
                result = conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status != ? AND user_id = ?",
                    (TaskStatus.DONE.value, encode_dt(now), parent_id, TaskStatus.DONE.value, user_id),
                )
                if result.rowcount:
                    conn.execute(
                        "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
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
        """Complete one occurrence and persist its exact response atomically."""
        import json

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                """
                SELECT request_fingerprint, response_status, response_json
                FROM task_done_idempotency
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (user_id, idempotency_key),
            ).fetchone()
            if previous is not None:
                if previous["request_fingerprint"] != request_fingerprint:
                    return 409, {"error": "idempotency_conflict"}
                return int(previous["response_status"]), json.loads(previous["response_json"])

            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
            ).fetchone()
            if row is None:
                # A fresh key must not be reserved by a task the caller cannot access.
                return 404, {"error": "没有找到这个任务。"}
            if row["status"] == TaskStatus.DONE.value:
                # Legacy completed rows may not have an occurrence mapping. Do not
                # infer or backfill one: a new intent is a stable no-op.
                status, response = 200, {"message": "任务已完成。"}
            else:
                now = utcnow()
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (TaskStatus.DONE.value, encode_dt(now), task_id, user_id),
                )
                event = conn.execute(
                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, "status_changed", TaskStatus.DONE.value, encode_dt(now)),
                )
                source_done_event_id = int(event.lastrowid)
                self._cascade_done_in_transaction(conn, task_id, user_id, now)

                next_task_id: int | None = None
                if row["recurrence"]:
                    next_due = _next_recurrence_due(decode_dt(row["due_at"]), row["recurrence"])
                    created_at = encode_dt(now)
                    next_cursor = conn.execute(
                        """
                        INSERT INTO tasks (
                            title, status, priority, due_at, estimated_minutes, notes,
                            parent_task_id, recurrence, user_id, created_at, updated_at, tags
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["title"], TaskStatus.TODO.value, row["priority"], encode_dt(next_due),
                            row["estimated_minutes"], row["notes"], None, row["recurrence"],
                            user_id, created_at, created_at, None,
                        ),
                    )
                    next_task_id = int(next_cursor.lastrowid)
                    conn.execute(
                        "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                        (next_task_id, "created", None, created_at),
                    )
                    status, response = 200, {
                        "message": f"已创建下一期任务 #{next_task_id}：{row['title']}"
                    }
                else:
                    status, response = 200, {
                        "message": f"已完成任务 #{task_id}：{row['title']}"
                    }

                response_json = json.dumps(response, ensure_ascii=False)
                conn.execute(
                    """
                    INSERT INTO task_done_occurrences
                        (user_id, source_task_id, source_done_event_id, next_task_id,
                         response_status, response_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, task_id, source_done_event_id, next_task_id, status, response_json, encode_dt(now)),
                )

            conn.execute(
                """
                INSERT INTO task_done_idempotency
                    (user_id, idempotency_key, request_fingerprint, response_status, response_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, idempotency_key, request_fingerprint, status,
                 json.dumps(response, ensure_ascii=False), encode_dt(utcnow())),
            )
        return status, response

    def complete_task_agent(
        self, task_id: int, *, user_id: str
    ) -> tuple[Task | None, bool, Task | None]:
        """原子完成 Agent 目标；返回任务、本次是否发生转换及本次创建的 next。"""
        import json

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
            ).fetchone()
            if row is None:
                return None, False, None
            if row["status"] == TaskStatus.DONE.value:
                # 旧 DONE 行或已完成 occurrence 均为稳定 no-op，不回填 mapping。
                return row_to_task(row), False, None

            now = utcnow()
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (TaskStatus.DONE.value, encode_dt(now), task_id, user_id),
            )
            event = conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "status_changed", TaskStatus.DONE.value, encode_dt(now)),
            )
            source_done_event_id = int(event.lastrowid)
            self._cascade_done_in_transaction(conn, task_id, user_id, now)

            next_row = None
            next_task_id = None
            if row["recurrence"]:
                next_due = _next_recurrence_due(decode_dt(row["due_at"]), row["recurrence"])
                created_at = encode_dt(now)
                next_cursor = conn.execute(
                    """
                    INSERT INTO tasks (
                        title, status, priority, due_at, estimated_minutes, notes,
                        parent_task_id, recurrence, user_id, created_at, updated_at, tags
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["title"], TaskStatus.TODO.value, row["priority"], encode_dt(next_due),
                        row["estimated_minutes"], row["notes"], None, row["recurrence"],
                        user_id, created_at, created_at, None,
                    ),
                )
                next_task_id = int(next_cursor.lastrowid)
                conn.execute(
                    "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                    (next_task_id, "created", None, created_at),
                )
                next_row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (next_task_id, user_id)
                ).fetchone()
                response = {"message": f"已创建下一期任务 #{next_task_id}：{row['title']}"}
            else:
                response = {"message": f"已完成任务 #{task_id}：{row['title']}"}

            conn.execute(
                """
                INSERT INTO task_done_occurrences
                    (user_id, source_task_id, source_done_event_id, next_task_id,
                     response_status, response_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, task_id, source_done_event_id, next_task_id,
                    200, json.dumps(response, ensure_ascii=False), encode_dt(now),
                ),
            )
            completed_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
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
        """获取父任务的所有子任务"""
        log.debug("get_subtasks parent=%d user=%r", parent_task_id, user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE parent_task_id = ? AND user_id = ? ORDER BY id",
                (parent_task_id, user_id),
            ).fetchall()
        return [row_to_task(row) for row in rows]

    def get_task_with_subtasks(self, task_id: int, *, user_id: str = DEFAULT_USER) -> Task | None:
        """获取任务及其所有子任务"""
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
        """创建子任务"""
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
        """批量创建子任务"""
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
        """获取父任务"""
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
        """添加任务关系"""
        now = utcnow()
        log.info("add_task_relation source=%d target=%d type=%s user=%r",
                 source_task_id, target_task_id, relation_type.value, user_id)
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO task_relations
                    (source_task_id, target_task_id, relation_type, user_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (source_task_id, target_task_id, relation_type.value, user_id, encode_dt(now)),
                )
                relation_id = int(cursor.lastrowid)
                row = conn.execute("SELECT * FROM task_relations WHERE id = ?", (relation_id,)).fetchone()
            return row_to_task_relation(row)
        except sqlite3.IntegrityError:
            log.warning("task relation already exists")
            return None

    def remove_task_relation(
        self,
        source_task_id: int,
        target_task_id: int,
        relation_type: TaskRelationType,
        *,
        user_id: str = DEFAULT_USER,
    ) -> bool:
        """移除任务关系"""
        log.info("remove_task_relation source=%d target=%d type=%s user=%r",
                 source_task_id, target_task_id, relation_type.value, user_id)
        with self._connect() as conn:
            result = conn.execute(
                """
                DELETE FROM task_relations
                WHERE source_task_id = ? AND target_task_id = ? AND relation_type = ? AND user_id = ?
                """,
                (source_task_id, target_task_id, relation_type.value, user_id),
            )
        return result.rowcount > 0

    def get_task_relations(
        self,
        task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> list[TaskRelation]:
        """获取任务的所有关系"""
        log.debug("get_task_relations task=%d user=%r", task_id, user_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_relations
                WHERE (source_task_id = ? OR target_task_id = ?) AND user_id = ?
                ORDER BY created_at
                """,
                (task_id, task_id, user_id),
            ).fetchall()
        return [row_to_task_relation(row) for row in rows]

    def get_dependencies(
        self,
        task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> list[Task]:
        """获取任务所依赖的任务（task depends on ...）"""
        log.debug("get_dependencies task=%d user=%r", task_id, user_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM tasks t
                INNER JOIN task_relations r ON t.id = r.target_task_id
                WHERE r.source_task_id = ? AND r.relation_type = ? AND r.user_id = ?
                """,
                (task_id, TaskRelationType.DEPENDS_ON.value, user_id),
            ).fetchall()
        return [row_to_task(row) for row in rows]

    def get_dependents(
        self,
        task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> list[Task]:
        """获取依赖该任务的任务（... depends on task）"""
        log.debug("get_dependents task=%d user=%r", task_id, user_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM tasks t
                INNER JOIN task_relations r ON t.id = r.source_task_id
                WHERE r.target_task_id = ? AND r.relation_type = ? AND r.user_id = ?
                """,
                (task_id, TaskRelationType.DEPENDS_ON.value, user_id),
            ).fetchall()
        return [row_to_task(row) for row in rows]

    def get_related_tasks(
        self,
        task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> list[Task]:
        """获取相关任务"""
        log.debug("get_related_tasks task=%d user=%r", task_id, user_id)
        related_task_ids: set[int] = set()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT target_task_id FROM task_relations
                WHERE source_task_id = ? AND relation_type = ? AND user_id = ?
                """,
                (task_id, TaskRelationType.RELATES_TO.value, user_id),
            ).fetchall()
            for row in rows:
                related_task_ids.add(row["target_task_id"])
            rows = conn.execute(
                """
                SELECT source_task_id FROM task_relations
                WHERE target_task_id = ? AND relation_type = ? AND user_id = ?
                """,
                (task_id, TaskRelationType.RELATES_TO.value, user_id),
            ).fetchall()
            for row in rows:
                related_task_ids.add(row["source_task_id"])
        if not related_task_ids:
            return []
        with self._connect() as conn:
            placeholders = ", ".join("?" for _ in related_task_ids)
            query = f"SELECT * FROM tasks WHERE id IN ({placeholders}) AND user_id = ?"
            rows = conn.execute(query, list(related_task_ids) + [user_id]).fetchall()
        return [row_to_task(row) for row in rows]

    def add_dependency(
        self,
        task_id: int,
        depends_on_task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> TaskRelation | None:
        """添加依赖关系：task depends on depends_on_task"""
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
        """移除依赖关系"""
        return self.remove_task_relation(
            task_id, depends_on_task_id, TaskRelationType.DEPENDS_ON, user_id=user_id
        )

    def is_task_blocked(
        self,
        task_id: int,
        *,
        user_id: str = DEFAULT_USER,
    ) -> bool:
        """检查任务是否被依赖未完成的任务阻塞"""
        dependencies = self.get_dependencies(task_id, user_id=user_id)
        for dep in dependencies:
            if dep.status != TaskStatus.DONE:
                return True
        return False

    # ── tags ──────────────────────────────────────────────────────

    def get_all_tags(self, *, user_id: str = DEFAULT_USER) -> list[str]:
        """获取用户所有标签（去重排序）"""
        log.info("get_all_tags user=%r", user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tags FROM tasks WHERE user_id = ? AND tags IS NOT NULL",
                (user_id,),
            ).fetchall()
        all_tags: set[str] = set()
        for row in rows:
            tags = _deserialize_tags(row["tags"])
            if tags:
                all_tags.update(tags)
        return sorted(list(all_tags))

    def get_tasks_by_tag(
        self, tag: str, *, user_id: str = DEFAULT_USER, status: TaskStatus | None = None
    ) -> list[Task]:
        """按标签获取任务"""
        log.info("get_tasks_by_tag tag=%r user=%r", tag, user_id)
        tag_lower = tag.strip().lower()
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE user_id = ? AND status = ? "
                    "ORDER BY due_at IS NULL, due_at, id",
                    (user_id, status.value),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE user_id = ? "
                    "ORDER BY due_at IS NULL, due_at, id",
                    (user_id,),
                ).fetchall()
        # 内存中过滤标签
        tasks = [row_to_task(row) for row in rows]
        return [
            t for t in tasks
            if t.tags and any(tag_lower == t_tag.lower() for t_tag in t.tags)
        ]

    # ── batch operations ──────────────────────────────────────────────────────

    def batch_update_status(
        self, task_ids: list[int], status: TaskStatus, *, user_id: str = DEFAULT_USER
    ) -> int:
        """批量更新任务状态，返回成功更新的数量"""
        log.info("batch_update_status task_ids=%r status=%s user=%r", task_ids, status.value, user_id)
        updated = 0
        with self._connect() as conn:
            for task_id in task_ids:
                result = conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND user_id = ? AND status != ?",
                    (status.value, encode_dt(utcnow()), task_id, user_id, status.value),
                )
                if result.rowcount > 0:
                    updated += 1
                    conn.execute(
                        "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                        (task_id, "status_changed", status.value, encode_dt(utcnow())),
                    )
        log.info("batch_update_status updated %d tasks", updated)
        return updated

    def batch_add_tags(
        self, task_ids: list[int], tags: list[str], *, user_id: str = DEFAULT_USER
    ) -> int:
        """批量给任务添加标签，返回成功更新的数量"""
        log.info("batch_add_tags task_ids=%r tags=%r user=%r", task_ids, tags, user_id)
        updated = 0
        with self._connect() as conn:
            for task_id in task_ids:
                row = conn.execute(
                    "SELECT tags FROM tasks WHERE id = ? AND user_id = ?",
                    (task_id, user_id),
                ).fetchone()
                if row is None:
                    continue
                existing_tags = _deserialize_tags(row["tags"]) or []
                # 合并并去重
                combined_tags = list(set(existing_tags + tags))
                new_tags_str = _serialize_tags(combined_tags)
                conn.execute(
                    "UPDATE tasks SET tags = ?, updated_at = ? WHERE id = ? AND user_id = ?",
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
            conn.execute(
                "INSERT OR REPLACE INTO user_memory (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, key, value, encode_dt(now)),
            )

    def get_memory(self, key: str, user_id: str = DEFAULT_USER) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM user_memory WHERE user_id = ? AND key = ?", (user_id, key)
            ).fetchone()
        return row["value"] if row else None

    def get_all_memory(self, user_id: str = DEFAULT_USER) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_memory WHERE user_id = ?", (user_id,)
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    # ── search ───────────────────────────────────────────────────────

    def search_tasks(
        self, query: str, *, user_id: str = DEFAULT_USER, status: TaskStatus | None = None
    ) -> list[Task]:
        log.info("search_tasks q=%r user=%r", query, user_id)
        like = f"%{query}%"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? "
                "AND (title LIKE ? OR notes LIKE ? OR tags LIKE ?) "
                "ORDER BY due_at IS NULL, due_at, id",
                (user_id, like, like, like),
            ).fetchall()
        return [row_to_task(row) for row in rows]

    # ── export / import ──────────────────────────────────────────────

    def export_user_data(self, user_id: str = DEFAULT_USER) -> dict:
        from .backup import export_user_data

        return export_user_data(self, user_id, "sqlite")

    def import_user_data(self, data: dict, user_id: str = DEFAULT_USER) -> int:
        from .backup import import_user_data

        return import_user_data(self, data, user_id, "sqlite")

    def import_user_data_with_summary(self, data: dict, user_id: str = DEFAULT_USER) -> dict[str, Any]:
        """导入 v1 备份并返回凭据 memory 排除摘要。"""
        from .backup import import_user_data_with_summary

        return import_user_data_with_summary(self, data, user_id, "sqlite")

    def restore_user_data(self, data: dict, user_id: str = DEFAULT_USER) -> dict[str, Any]:
        """原子恢复 v2 备份到当前用户的空数据域。"""
        from .backup import restore_user_data

        return restore_user_data(self, data, user_id, "sqlite")

    # ── heartbeat / 心跳功能 ─────────────────────────────────

    def get_heartbeat_config(self, user_id: str = DEFAULT_USER) -> dict:
        """获取用户心跳配置。

        返回字段：
            enabled: bool - 是否启用心跳
            start_hour: int - 开始时间（0-23）
            end_hour: int - 结束时间（0-23）
            interval_hours: int - 两次建议之间的间隔（小时）
            last_heartbeat_at: str | None - 上次心跳的 ISO 时间，或 None
        """
        config_str = self.get_memory("heartbeat_config", user_id=user_id)
        if config_str:
            import json
            try:
                return json.loads(config_str)
            except json.JSONDecodeError:
                pass
        # 默认配置
        return {
            "enabled": False,
            "start_hour": 9,
            "end_hour": 21,
            "interval_hours": 4,
            "last_heartbeat_at": None
        }

    def set_heartbeat_config(
        self,
        enabled: bool | None = None,
        start_hour: int | None = None,
        end_hour: int | None = None,
        interval_hours: int | None = None,
        user_id: str = DEFAULT_USER,
    ) -> dict:
        """更新用户心跳配置，返回更新后的配置。"""
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
        """更新上次心跳时间为当前时间，返回更新后的配置。"""
        config = self.get_heartbeat_config(user_id=user_id)
        config["last_heartbeat_at"] = utcnow().isoformat()
        import json
        self.set_memory("heartbeat_config", json.dumps(config), user_id=user_id)
        return config

    def should_trigger_heartbeat(self, user_id: str = DEFAULT_USER) -> bool:
        """判断当前是否应该触发心跳建议。"""
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
            conn.execute("BEGIN IMMEDIATE")
            if actual_seconds is not None:
                owned_task = conn.execute(
                    "SELECT id FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
                ).fetchone()
                if owned_task is None:
                    raise FocusTaskNotFound
            if session_id and actual_seconds is not None:
                # Reserve SQLite's writer lock before checking: same-user retries
                # cannot both observe absence and append separate events.
                candidates = conn.execute(
                    """
                    SELECT e.task_id, e.payload FROM task_events e
                    JOIN tasks t ON e.task_id = t.id
                    WHERE e.event_type = ? AND t.user_id = ?
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
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "focus_session", payload_json, encode_dt(now)),
            )
        return focus_result(task_id, payload) if actual_seconds is not None else None

    def get_focus_sessions(self, *, user_id: str = DEFAULT_USER) -> list[dict]:
        """获取用户所有专注记录（最近30天）"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT e.task_id, e.payload, e.created_at
                FROM task_events e
                INNER JOIN tasks t ON e.task_id = t.id
                WHERE e.event_type = ? AND t.user_id = ?
                AND e.created_at >= date('now', '-30 days')
                ORDER BY e.created_at DESC
                """,
                ("focus_session", user_id),
            ).fetchall()
        import json
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
        """Read owner-scoped status/focus events in one SQLite snapshot."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            completed = conn.execute(
                """
                SELECT e.id AS event_id, e.task_id, e.payload AS status_value,
                       e.created_at AS completed_at, t.title,
                       t.estimated_minutes AS estimated_minutes_reference
                FROM task_events e JOIN tasks t ON e.task_id = t.id
                WHERE t.user_id = ? AND e.event_type = 'status_changed'
                ORDER BY e.id
                """,
                (user_id,),
            ).fetchall()
            focus = conn.execute(
                """
                SELECT e.task_id, e.payload, e.created_at
                FROM task_events e JOIN tasks t ON e.task_id = t.id
                WHERE t.user_id = ? AND e.event_type = 'focus_session'
                """,
                (user_id,),
            ).fetchall()
        return {
            "status_events": [dict(row) for row in completed],
            "focus_sessions": [dict(row) for row in focus],
        }


# ── 工具函数 ──────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def encode_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def decode_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _next_recurrence_due(from_date: datetime | None, recurrence: str) -> datetime | None:
    if from_date is None:
        return None
    if recurrence == "daily":
        return from_date + timedelta(days=1)
    if recurrence == "weekly":
        return from_date + timedelta(days=7)
    if recurrence == "monthly":
        month = from_date.month + 1
        year = from_date.year
        if month > 12:
            month = 1
            year += 1
        try:
            return from_date.replace(year=year, month=month)
        except ValueError:
            return from_date + timedelta(days=30)
    return None


def _coerce_priority(value: "Priority | str | None", *, default: Priority = Priority.MEDIUM) -> Priority:
    """把字符串优先级转成枚举。

    以前直接写 priority.value，传字符串会在日志行里抛 AttributeError，
    错误信息完全看不出是调用方传错了类型。
    """
    if value is None:
        return default
    if isinstance(value, Priority):
        return value
    try:
        return Priority(str(value).strip().lower())
    except ValueError:
        raise ValueError(f"无效优先级：{value!r}（可选 low/medium/high）") from None


def _serialize_tags(tags: list[str] | None) -> str | None:
    """将标签列表序列化为逗号分隔的字符串（去重排序）。"""
    if not tags:
        return None
    unique_tags = sorted(list({tag.strip() for tag in tags if tag.strip()}))
    return ",".join(unique_tags) if unique_tags else None


def _deserialize_tags(tags_str: str | None) -> list[str] | None:
    """将逗号分隔的字符串反序列化为标签列表。"""
    if not tags_str:
        return None
    return [tag.strip() for tag in tags_str.split(",") if tag.strip()]


def row_to_task(row: sqlite3.Row | dict[str, Any]) -> Task:
    """将数据库行转换为 Task 对象（兼容 sqlite3.Row 和 dict）。"""
    def get_val(key: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key]
        except (KeyError, IndexError):
            return default

    def has_key(key: str) -> bool:
        if isinstance(row, dict):
            return key in row
        try:
            row[key]
            return True
        except (KeyError, IndexError):
            return False

    return Task(
        id=int(get_val("id")),
        title=str(get_val("title")),
        status=TaskStatus(get_val("status")),
        priority=Priority(get_val("priority")),
        due_at=decode_dt(get_val("due_at")),
        estimated_minutes=get_val("estimated_minutes"),
        notes=get_val("notes"),
        parent_task_id=get_val("parent_task_id"),
        recurrence=get_val("recurrence") if has_key("recurrence") else None,
        user_id=get_val("user_id") if has_key("user_id") else None,
        created_at=decode_dt(get_val("created_at")) or utcnow(),
        updated_at=decode_dt(get_val("updated_at")) or utcnow(),
        tags=_deserialize_tags(get_val("tags")),
    )


def row_to_task_relation(row: sqlite3.Row | dict[str, Any]) -> TaskRelation:
    """将数据库行转换为 TaskRelation 对象。"""
    def get_val(key: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key]
        except (KeyError, IndexError):
            return default

    return TaskRelation(
        id=int(get_val("id")),
        source_task_id=int(get_val("source_task_id")),
        target_task_id=int(get_val("target_task_id")),
        relation_type=TaskRelationType(get_val("relation_type")),
        created_at=decode_dt(get_val("created_at")) or utcnow(),
    )
