from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .logger import get_logger
from .models import Priority, Task, TaskStatus

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
    updated_at TEXT NOT NULL
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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        log.debug("initializing schema")
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            self._ensure_default_user(conn)

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
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "recurrence" not in cols:
            log.info("migration: adding recurrence column")
            conn.execute("ALTER TABLE tasks ADD COLUMN recurrence TEXT")
        if "user_id" not in cols:
            log.info("migration: adding user_id column")
            conn.execute("ALTER TABLE tasks ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default' REFERENCES users(id)")

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
        row = self._connect().execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        token = generate_token()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                (token, user_id, encode_dt(auth_now())),
            )
        log.info("login user=%r", user_id)
        return token

    def validate_session(self, token: str) -> str | None:
        row = self._connect().execute(
            "SELECT user_id FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        return row["user_id"] if row else None

    def logout_user(self, token: str) -> None:
        log.info("logout token=...%s", token[-8:])
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        from .auth import verify_password, hash_password
        row = self._connect().execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row or not verify_password(old_password, row["password_hash"]):
            return False
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(new_password), user_id),
            )
        log.info("password changed for user=%r", user_id)
        return True

    def list_users(self) -> list[dict[str, str]]:
        rows = self._connect().execute("SELECT id, display_name FROM users ORDER BY id").fetchall()
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
        user_id: str = DEFAULT_USER,
    ) -> Task:
        now = utcnow()
        log.info("create_task title=%r user=%r priority=%s", title.strip(), user_id, priority.value)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    title, status, priority, due_at, estimated_minutes, notes,
                    parent_task_id, recurrence, user_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        row = self._connect().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row_to_task(row) if row else None

    def update_status(
        self, task_id: int, status: TaskStatus, *, user_id: str | None = None
    ) -> Task | None:
        now = utcnow()
        log.info("update_status task=%d status=%s user=%r", task_id, status.value, user_id)
        with self._connect() as conn:
            if user_id is not None:
                old = conn.execute(
                    "SELECT parent_task_id FROM tasks WHERE id = ? AND user_id = ?",
                    (task_id, user_id),
                ).fetchone()
            else:
                old = conn.execute("SELECT parent_task_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
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
        task = row_to_task(row) if row else None
        if task and status == TaskStatus.DONE and old and old["parent_task_id"]:
            self._auto_complete_parent(old["parent_task_id"])
        return task

    def update_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        due_at: datetime | None = None,
        priority: Priority | None = None,
        estimated_minutes: int | None = None,
        notes: str | None = None,
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
        if not sets:
            return self._get_task(task_id)
        sets.append("updated_at = ?")
        params.append(encode_dt(now))
        params.append(task_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
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
        children = self._connect().execute(
            "SELECT * FROM tasks WHERE parent_task_id = ?", (parent_id,)
        ).fetchall()
        if children and all(row["status"] == TaskStatus.DONE.value for row in children):
            log.info("auto-completing parent task #%d", parent_id)
            self.update_status(parent_id, TaskStatus.DONE)

    def complete_recurring_task(self, task_id: int) -> Task | None:
        task = self.update_status(task_id, TaskStatus.DONE)
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
        row = self._connect().execute(
            "SELECT value FROM user_memory WHERE user_id = ? AND key = ?", (user_id, key)
        ).fetchone()
        return row["value"] if row else None

    def get_all_memory(self, user_id: str = DEFAULT_USER) -> dict[str, str]:
        rows = self._connect().execute(
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
            if status:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE user_id = ? AND title LIKE ? AND status = ? "
                    "ORDER BY due_at IS NULL, due_at, id",
                    (user_id, like, status.value),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE user_id = ? AND title LIKE ? "
                    "ORDER BY due_at IS NULL, due_at, id",
                    (user_id, like),
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


def row_to_task(row: sqlite3.Row | dict[str, Any]) -> Task:
    return Task(
        id=int(row["id"]),
        title=str(row["title"]),
        status=TaskStatus(row["status"]),
        priority=Priority(row["priority"]),
        due_at=decode_dt(row["due_at"]),
        estimated_minutes=row["estimated_minutes"],
        notes=row["notes"],
        parent_task_id=row["parent_task_id"],
        recurrence=row["recurrence"] if "recurrence" in row.keys() else None,
        user_id=row["user_id"] if "user_id" in row.keys() else None,
        created_at=decode_dt(row["created_at"]) or utcnow(),
        updated_at=decode_dt(row["updated_at"]) or utcnow(),
    )
