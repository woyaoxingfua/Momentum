from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from .logger import get_logger, log_db_query, log_security_event
from .models import Priority, Task, TaskStatus, TaskRelation, TaskRelationType

log = get_logger("storage")

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
    created_at TEXT NOT NULL
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
"""

DEFAULT_USER = "default"


class TaskStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        log.info("store opened: %s", self.db_path)

    def _init_schema(self) -> None:
        log.debug("initializing schema")
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            self._ensure_default_user(conn)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        except sqlite3.Error as e:
            log.error("database error: %s", e)
            conn.rollback()
            raise
        finally:
            conn.commit()
            conn.close()

    def _ensure_default_user(self, conn: sqlite3.Connection) -> None:
        from .auth import hash_password
        existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing == 0:
            conn.execute(
                "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (DEFAULT_USER, "默认用户", hash_password("momentum"), encode_dt(utcnow())),
            )
            log.info("created default user (password: momentum)")

    def _migrate(self, conn: sqlite3.Connection) -> None:
        # 迁移 users 表：添加 password_hash 列（兼容旧数据库）
        from .auth import hash_password
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

    # ── auth ───────────────────────────────────────────────────────

    def register_user(self, user_id: str, display_name: str, password_hash: str) -> None:
        from .auth import utcnow as auth_now
        log.info("register user=%r", user_id)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, display_name, password_hash, encode_dt(auth_now())),
            )

    def login_user(self, user_id: str, password: str) -> str | None:
        from .auth import generate_token, utcnow as auth_now, verify_password
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
            conn.execute(
                "INSERT OR REPLACE INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                (token, user_id, encode_dt(auth_now())),
            )
        log.info("login user=%r", user_id)
        return token

    def validate_session(self, token: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE token = ?", (token,)
            ).fetchone()
        return row["user_id"] if row else None

    def logout_user(self, token: str) -> None:
        log.info("logout token=...%s", token[-8:])
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        from .auth import verify_password, hash_password
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

    def _get_task(self, task_id: int) -> Task | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
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
                old = conn.execute(
                    "SELECT parent_task_id FROM tasks WHERE id = ? AND user_id = ?",
                    (task_id, user_id),
                ).fetchone()
            else:
                old = conn.execute("SELECT parent_task_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if old:
                old_parent_id = old["parent_task_id"]
            if user_id is not None and old is None:
                log.warning("update_status: task #%d not owned by %r", task_id, user_id)
                return None
            if user_id is not None:
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (status.value, encode_dt(now), task_id, user_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (status.value, encode_dt(now), task_id),
                )
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "status_changed", status.value, encode_dt(now)),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            subtasks = conn.execute(
                "SELECT id FROM tasks WHERE parent_task_id = ?", (task_id,)
            ).fetchall()
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
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE parent_task_id = ? AND status != ? AND user_id = ?",
                    (TaskStatus.DONE.value, encode_dt(now), parent_task_id, TaskStatus.DONE.value, user_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE parent_task_id = ? AND status != ?",
                    (TaskStatus.DONE.value, encode_dt(now), parent_task_id, TaskStatus.DONE.value),
                )
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
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
            return self._get_task(task_id)
        sets.append("updated_at = ?")
        params.append(encode_dt(now))
        params.append(task_id)
        params.append(user_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND user_id = ?", params)
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "updated", None, encode_dt(now)),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
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
            children = conn.execute(
                "SELECT * FROM tasks WHERE parent_task_id = ?", (parent_id,)
            ).fetchall()
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
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (status.value, encode_dt(utcnow()), task_id, user_id),
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
