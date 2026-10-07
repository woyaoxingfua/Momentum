"""Versioned, user-scoped backup import/export helpers.

Only task-owned events are exportable: ``task_events`` has no ``user_id``
column, so events without a task cannot be attributed safely.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..models import Priority, TaskRelationType, TaskStatus


UNATTRIBUTED_EVENTS_REASON = (
    "task_events 没有 user_id，无法安全确定未归属事件数量；count 为 null 表示数量未知（不代表为零），"
    "此类事件不会导出或导入。"
)
EXCLUDED_CREDENTIAL_MEMORY_REASON = (
    "user_memory 中键名标识为凭据的项目不会导出或导入，以避免备份携带 API key、口令或令牌。"
)


class BackupError(ValueError):
    """Base class for safely rejected backup operations."""


class BackupValidationError(BackupError):
    """The backup structure or one of its fields is invalid."""


class UnsupportedBackupVersionError(BackupValidationError):
    """The backup version is not supported."""


class BackupNotEmptyError(BackupError):
    """A v2 restore was attempted into a non-empty user data domain."""


class BackupIntegrityError(BackupError):
    """Existing database rows cannot be represented without losing ownership."""


_SENSITIVE_KEY_PARTS = (
    "apikey",
    "password",
    "passwd",
    "secret",
    "credential",
    "authorization",
    "token",
    "privatekey",
    "accesskey",
)


def _is_credential_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    tokens = set(re.findall(r"[a-z0-9]+", key.casefold()))
    return "key" in tokens or any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _time_value(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise BackupValidationError(f"字段 {field} 必须是 ISO 时间字符串" if not nullable else f"字段 {field} 必须是 ISO 时间字符串或 null")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupValidationError(f"字段 {field} 不是有效的 ISO 时间") from exc
    return value


def _integer(value: object, field: str, *, minimum: int | None = None, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupValidationError(f"字段 {field} 必须是整数" if not nullable else f"字段 {field} 必须是整数或 null")
    if minimum is not None and value < minimum:
        raise BackupValidationError(f"字段 {field} 不能小于 {minimum}")
    return value


def _string(value: object, field: str, *, nullable: bool = False, max_length: int | None = None) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise BackupValidationError(f"字段 {field} 必须是字符串" if not nullable else f"字段 {field} 必须是字符串或 null")
    if max_length is not None and len(value) > max_length:
        raise BackupValidationError(f"字段 {field} 超过最大长度 {max_length}")
    return value


def _require_fields(record: object, required: set[str], allowed: set[str], field: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise BackupValidationError(f"{field} 必须是对象")
    missing = required - record.keys()
    unknown = record.keys() - allowed
    if missing:
        raise BackupValidationError(f"{field} 缺少字段：{', '.join(sorted(missing))}")
    if unknown:
        raise BackupValidationError(f"{field} 包含未知字段：{', '.join(sorted(unknown))}")
    return record


def _validate_v1(data: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = data.get("tasks", [])
    memory = data.get("memory", {})
    if not isinstance(tasks, list):
        raise BackupValidationError("v1 的 tasks 必须是数组")
    if not isinstance(memory, dict):
        raise BackupValidationError("v1 的 memory 必须是对象")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in memory.items()):
        raise BackupValidationError("v1 的 memory 键和值必须是字符串")

    validated: list[dict[str, Any]] = []
    for index, item in enumerate(tasks):
        if not isinstance(item, dict):
            raise BackupValidationError(f"v1 的 tasks[{index}] 必须是对象")
        title = _string(item.get("title"), f"tasks[{index}].title")
        priority = item.get("priority", Priority.MEDIUM.value)
        if not isinstance(priority, str) or priority not in Priority._value2member_map_:
            raise BackupValidationError(f"tasks[{index}].priority 无效")
        due_at = item.get("due_at")
        if due_at is not None:
            due_at = _time_value(due_at, f"tasks[{index}].due_at")
        estimated = _integer(item.get("estimated_minutes"), f"tasks[{index}].estimated_minutes", minimum=0, nullable=True)
        notes = _string(item.get("notes"), f"tasks[{index}].notes", nullable=True)
        recurrence = _string(item.get("recurrence"), f"tasks[{index}].recurrence", nullable=True)
        validated.append({
            "title": title,
            "priority": priority,
            "due_at": due_at,
            "estimated_minutes": estimated,
            "notes": notes,
            "recurrence": recurrence,
        })
    return validated


_V2_TOP_LEVEL = {
    "version", "exported_at", "user_id", "tasks", "relations", "events",
    "memory", "memory_updated_at", "excluded_events", "excluded_memory",
}
_TASK_FIELDS = {
    "id", "title", "status", "priority", "due_at", "estimated_minutes",
    "notes", "parent_task_id", "recurrence", "created_at", "updated_at", "tags",
}
_RELATION_FIELDS = {"source_task_id", "target_task_id", "relation_type", "created_at"}
_EVENT_FIELDS = {"task_id", "event_type", "payload", "created_at"}


def _validate_exclusion(
    value: object,
    field: str,
    reason: str,
    *,
    count_must_be_null: bool = False,
) -> dict[str, Any]:
    record = _require_fields(value, {"count", "reason"}, {"count", "reason"}, field)
    if count_must_be_null:
        if record["count"] is not None:
            raise BackupValidationError(f"{field}.count 必须为 null，因为数量未知")
        count = None
    else:
        count = _integer(record["count"], f"{field}.count", minimum=0)
    if record["reason"] != reason:
        raise BackupValidationError(f"{field}.reason 不受支持")
    return {"count": count, "reason": reason}


def _validate_v2(data: dict[str, Any]) -> dict[str, Any]:
    unknown = data.keys() - _V2_TOP_LEVEL
    missing = _V2_TOP_LEVEL - data.keys()
    if missing:
        raise BackupValidationError(f"v2 备份缺少字段：{', '.join(sorted(missing))}")
    if unknown:
        raise BackupValidationError(f"v2 备份包含未知字段：{', '.join(sorted(unknown))}")
    _time_value(data["exported_at"], "exported_at")
    source_user = data["user_id"]
    if source_user is not None and (not isinstance(source_user, str) or len(source_user) > 64):
        raise BackupValidationError("user_id 元数据无效")

    tasks_raw = data["tasks"]
    relations_raw = data["relations"]
    events_raw = data["events"]
    memory_raw = data["memory"]
    memory_times_raw = data["memory_updated_at"]
    if not isinstance(tasks_raw, list) or not isinstance(relations_raw, list) or not isinstance(events_raw, list):
        raise BackupValidationError("tasks、relations 和 events 必须是数组")
    if not isinstance(memory_raw, dict) or not isinstance(memory_times_raw, dict):
        raise BackupValidationError("memory 和 memory_updated_at 必须是对象")

    tasks: list[dict[str, Any]] = []
    task_ids: set[int] = set()
    for index, raw in enumerate(tasks_raw):
        item = _require_fields(raw, _TASK_FIELDS, _TASK_FIELDS, f"tasks[{index}]")
        source_id = _integer(item["id"], f"tasks[{index}].id", minimum=1)
        if source_id in task_ids:
            raise BackupValidationError("v2 tasks 中存在重复 ID")
        task_ids.add(source_id)
        title = _string(item["title"], f"tasks[{index}].title")
        status = _string(item["status"], f"tasks[{index}].status")
        priority = _string(item["priority"], f"tasks[{index}].priority")
        if status not in TaskStatus._value2member_map_:
            raise BackupValidationError(f"tasks[{index}].status 无效")
        if priority not in Priority._value2member_map_:
            raise BackupValidationError(f"tasks[{index}].priority 无效")
        due_at = _time_value(item["due_at"], f"tasks[{index}].due_at", nullable=True)
        estimated = _integer(item["estimated_minutes"], f"tasks[{index}].estimated_minutes", minimum=0, nullable=True)
        notes = _string(item["notes"], f"tasks[{index}].notes", nullable=True)
        parent = _integer(item["parent_task_id"], f"tasks[{index}].parent_task_id", minimum=1, nullable=True)
        recurrence = _string(item["recurrence"], f"tasks[{index}].recurrence", nullable=True)
        created_at = _time_value(item["created_at"], f"tasks[{index}].created_at")
        updated_at = _time_value(item["updated_at"], f"tasks[{index}].updated_at")
        tags = item["tags"]
        if tags is not None:
            if not isinstance(tags, list) or not tags:
                raise BackupValidationError(f"tasks[{index}].tags 必须是 null 或非空字符串数组")
            if any(not isinstance(tag, str) or not tag or tag != tag.strip() or "," in tag for tag in tags):
                raise BackupValidationError(f"tasks[{index}].tags 包含无法安全保存的标签")
            if tags != sorted(set(tags)):
                raise BackupValidationError(f"tasks[{index}].tags 必须去重并按字典序排列")
        tasks.append({
            "id": source_id,
            "title": title,
            "status": status,
            "priority": priority,
            "due_at": due_at,
            "estimated_minutes": estimated,
            "notes": notes,
            "parent_task_id": parent,
            "recurrence": recurrence,
            "created_at": created_at,
            "updated_at": updated_at,
            "tags": tags,
        })

    for item in tasks:
        parent = item["parent_task_id"]
        if parent is not None and (parent not in task_ids or parent == item["id"]):
            raise BackupValidationError("parent_task_id 必须指向备份内的其他任务")
    parents = {item["id"]: item["parent_task_id"] for item in tasks}
    for task_id in parents:
        visited: set[int] = set()
        cursor = task_id
        while cursor is not None:
            if cursor in visited:
                raise BackupValidationError("任务层级存在循环引用")
            visited.add(cursor)
            cursor = parents.get(cursor)

    relations: list[dict[str, Any]] = []
    relation_keys: set[tuple[int, int, str]] = set()
    for index, raw in enumerate(relations_raw):
        item = _require_fields(raw, _RELATION_FIELDS, _RELATION_FIELDS, f"relations[{index}]")
        source_id = _integer(item["source_task_id"], f"relations[{index}].source_task_id", minimum=1)
        target_id = _integer(item["target_task_id"], f"relations[{index}].target_task_id", minimum=1)
        relation_type = _string(item["relation_type"], f"relations[{index}].relation_type")
        created_at = _time_value(item["created_at"], f"relations[{index}].created_at")
        if source_id not in task_ids or target_id not in task_ids:
            raise BackupValidationError("关系两端必须属于备份内的任务")
        if relation_type not in TaskRelationType._value2member_map_:
            raise BackupValidationError(f"relations[{index}].relation_type 无效")
        key = (source_id, target_id, relation_type)
        if key in relation_keys:
            raise BackupValidationError("relations 中存在重复关系")
        relation_keys.add(key)
        relations.append({
            "source_task_id": source_id,
            "target_task_id": target_id,
            "relation_type": relation_type,
            "created_at": created_at,
        })

    events: list[dict[str, Any]] = []
    for index, raw in enumerate(events_raw):
        item = _require_fields(raw, _EVENT_FIELDS, _EVENT_FIELDS, f"events[{index}]")
        task_id = _integer(item["task_id"], f"events[{index}].task_id", minimum=1)
        event_type = _string(item["event_type"], f"events[{index}].event_type", max_length=32)
        payload = _string(item["payload"], f"events[{index}].payload", nullable=True)
        created_at = _time_value(item["created_at"], f"events[{index}].created_at")
        if task_id not in task_ids:
            raise BackupValidationError("每个 v2 事件都必须关联备份内的任务")
        if not event_type:
            raise BackupValidationError(f"events[{index}].event_type 不能为空")
        events.append({
            "task_id": task_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": created_at,
        })

    memory: dict[str, str] = {}
    for key, value in memory_raw.items():
        if not isinstance(key, str) or not key or len(key) > 128 or not isinstance(value, str):
            raise BackupValidationError("memory 的键和值必须是有效字符串")
        if _is_credential_key(key):
            raise BackupValidationError("v2 memory 不允许包含凭据类键")
        memory[key] = value
    if set(memory_times_raw) != set(memory):
        raise BackupValidationError("memory_updated_at 必须与 memory 键集合完全一致")
    memory_times: dict[str, str] = {}
    for key, value in memory_times_raw.items():
        memory_times[key] = _time_value(value, f"memory_updated_at.{key}")

    return {
        "tasks": tasks,
        "relations": relations,
        "events": events,
        "memory": memory,
        "memory_updated_at": memory_times,
        "excluded_events": _validate_exclusion(
            data["excluded_events"],
            "excluded_events",
            UNATTRIBUTED_EVENTS_REASON,
            count_must_be_null=True,
        ),
        "excluded_memory": _validate_exclusion(data["excluded_memory"], "excluded_memory", EXCLUDED_CREDENTIAL_MEMORY_REASON),
    }


def _execute(store: Any, conn: Any, sql: str, params: tuple[Any, ...], dialect: str) -> Any:
    if dialect == "mysql":
        return store._execute(conn, sql.replace("?", "%s"), params)
    return conn.execute(sql, params)


def _begin(conn: Any, dialect: str) -> None:
    if dialect == "mysql":
        conn.begin()
    else:
        conn.execute("BEGIN IMMEDIATE")


def _lock_mysql_user_domain(store: Any, conn: Any, user_id: str) -> None:
    """Serialize restores and protect empty user-scoped key ranges in InnoDB."""
    _execute(store, conn, "SELECT id FROM users WHERE id = ? FOR UPDATE", (user_id,), "mysql").fetchall()
    _execute(store, conn, "SELECT id FROM tasks WHERE user_id = ? FOR UPDATE", (user_id,), "mysql").fetchall()
    _execute(store, conn, "SELECT id FROM task_relations WHERE user_id = ? FOR UPDATE", (user_id,), "mysql").fetchall()
    _execute(store, conn, "SELECT user_id FROM user_memory WHERE user_id = ? FOR UPDATE", (user_id,), "mysql").fetchall()


def _count(store: Any, conn: Any, table: str, where: str, params: tuple[Any, ...], dialect: str) -> int:
    row = _execute(store, conn, f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params, dialect).fetchone()
    return int(row["n"])


def _encode_tags(tags: list[str] | None) -> str | None:
    return ",".join(tags) if tags else None


def _write_memory(store: Any, conn: Any, key: str, value: str, updated_at: str, user_id: str, dialect: str) -> None:
    column = "`key`" if dialect == "mysql" else "key"
    if dialect == "mysql":
        _execute(
            store, conn,
            f"INSERT INTO user_memory (user_id, {column}, value, updated_at) VALUES (?, ?, ?, ?) "
            f"ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = VALUES(updated_at)",
            (user_id, key, value, updated_at), dialect,
        )
    else:
        _execute(
            store, conn,
            f"INSERT INTO user_memory (user_id, {column}, value, updated_at) VALUES (?, ?, ?, ?) "
            f"ON CONFLICT(user_id, {column}) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (user_id, key, value, updated_at), dialect,
        )


def _write_v1(store: Any, conn: Any, tasks: list[dict[str, Any]], memory: dict[str, str], user_id: str, dialect: str) -> int:
    from .sqlite import utcnow

    imported = 0
    for item in tasks:
        now = utcnow().isoformat()
        cursor = _execute(
            store, conn,
            "INSERT INTO tasks (title, status, priority, due_at, estimated_minutes, notes, parent_task_id, recurrence, user_id, created_at, updated_at, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["title"], TaskStatus.TODO.value, item["priority"], item["due_at"], item["estimated_minutes"], item["notes"], None, item["recurrence"], user_id, now, now, None),
            dialect,
        )
        task_id = int(conn.insert_id()) if dialect == "mysql" else int(cursor.lastrowid)
        _execute(
            store, conn,
            "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "created", None, now), dialect,
        )
        imported += 1
    for key, value in memory.items():
        from .sqlite import utcnow
        _write_memory(store, conn, key, value, utcnow().isoformat(), user_id, dialect)
    return imported


def _write_v2(store: Any, conn: Any, backup: dict[str, Any], user_id: str, dialect: str) -> dict[str, Any]:
    id_map: dict[int, int] = {}
    for item in backup["tasks"]:
        cursor = _execute(
            store, conn,
            "INSERT INTO tasks (title, status, priority, due_at, estimated_minutes, notes, parent_task_id, recurrence, user_id, created_at, updated_at, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["title"], item["status"], item["priority"], item["due_at"], item["estimated_minutes"], item["notes"], None, item["recurrence"], user_id, item["created_at"], item["updated_at"], _encode_tags(item["tags"])),
            dialect,
        )
        new_id = int(conn.insert_id()) if dialect == "mysql" else int(cursor.lastrowid)
        id_map[item["id"]] = new_id
    for item in backup["tasks"]:
        parent_id = item["parent_task_id"]
        if parent_id is not None:
            _execute(
                store, conn,
                "UPDATE tasks SET parent_task_id = ? WHERE id = ? AND user_id = ?",
                (id_map[parent_id], id_map[item["id"]], user_id), dialect,
            )
    for item in backup["relations"]:
        _execute(
            store, conn,
            "INSERT INTO task_relations (source_task_id, target_task_id, relation_type, user_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (id_map[item["source_task_id"]], id_map[item["target_task_id"]], item["relation_type"], user_id, item["created_at"]),
            dialect,
        )
    for item in backup["events"]:
        _insert_v2_event(store, conn, item, id_map, dialect)
    for key, value in backup["memory"].items():
        _write_memory(store, conn, key, value, backup["memory_updated_at"][key], user_id, dialect)
    return {
        "imported_tasks": len(backup["tasks"]),
        "id_map": id_map,
        "excluded_events": backup["excluded_events"],
        "excluded_memory": backup["excluded_memory"],
    }


def _insert_v2_event(store: Any, conn: Any, item: dict[str, Any], id_map: dict[int, int], dialect: str) -> None:
    _execute(
        store, conn,
        "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
        (id_map[item["task_id"]], item["event_type"], item["payload"], item["created_at"]), dialect,
    )


def _run_import(store: Any, data: object, user_id: str, dialect: str, *, v2_only: bool) -> tuple[str, int | dict[str, Any]]:
    with store._connect() as conn:
        try:
            _begin(conn, dialect)
            if not isinstance(data, dict):
                raise BackupValidationError("备份必须是 JSON 对象")
            version = data.get("version", "1.0")
            if version == "1.0":
                if v2_only:
                    raise BackupValidationError("恢复接口只接受 v2 备份")
                tasks = _validate_v1(data)
                memory = data.get("memory", {})
                safe_memory = {
                    key: value for key, value in memory.items()
                    if not _is_credential_key(key)
                }
                imported_tasks = _write_v1(store, conn, tasks, safe_memory, user_id, dialect)
                result: int | dict[str, Any] = {
                    "imported_tasks": imported_tasks,
                    "excluded_memory": {
                        "count": len(memory) - len(safe_memory),
                        "reason": EXCLUDED_CREDENTIAL_MEMORY_REASON,
                    },
                }
                return "1.0", result
            if version != "2.0":
                raise UnsupportedBackupVersionError(f"不支持的备份版本：{version}")
            backup = _validate_v2(data)
            if dialect == "mysql":
                _lock_mysql_user_domain(store, conn, user_id)
            own_tasks = _count(store, conn, "tasks", "user_id = ?", (user_id,), dialect)
            own_relations = _count(store, conn, "task_relations", "user_id = ?", (user_id,), dialect)
            own_memory = _count(store, conn, "user_memory", "user_id = ?", (user_id,), dialect)
            if own_tasks or own_relations or own_memory:
                raise BackupNotEmptyError("当前认证用户的数据域非空；v2 恢复仅允许写入空数据域。")
            result = _write_v2(store, conn, backup, user_id, dialect)
            return "2.0", result
        except BaseException:
            conn.rollback()
            raise


def import_user_data(store: Any, data: object, user_id: str, dialect: str) -> int:
    """Import v1 by merge or v2 by empty-domain restore; return task count."""
    version, result = _run_import(store, data, user_id, dialect, v2_only=False)
    if isinstance(result, dict):
        return int(result["imported_tasks"])
    return int(result)


def import_user_data_with_summary(store: Any, data: object, user_id: str, dialect: str) -> dict[str, Any]:
    """Import a v1 package and return task count plus a credential exclusion summary."""
    if not isinstance(data, dict) or data.get("version", "1.0") != "1.0":
        raise BackupValidationError("v1 导入接口只接受 v1 备份")
    version, result = _run_import(store, data, user_id, dialect, v2_only=False)
    if version != "1.0" or not isinstance(result, dict):
        raise BackupValidationError("v1 导入接口只接受 v1 备份")
    return result


def restore_user_data(store: Any, data: object, user_id: str, dialect: str) -> dict[str, Any]:
    """Atomically restore a v2 package and return its ID map and exclusions."""
    version, result = _run_import(store, data, user_id, dialect, v2_only=True)
    if version != "2.0" or not isinstance(result, dict):
        raise BackupValidationError("恢复接口只接受 v2 备份")
    return result


def export_user_data(store: Any, user_id: str, dialect: str) -> dict[str, Any]:
    """Export a consistent v2 snapshot of a user's safe-to-attribute data."""
    from .sqlite import utcnow

    with store._connect() as conn:
        try:
            if dialect == "mysql":
                conn.begin()
            else:
                conn.execute("BEGIN")
            task_rows = _execute(store, conn, "SELECT * FROM tasks WHERE user_id = ? ORDER BY id", (user_id,), dialect).fetchall()
            task_ids = {int(row["id"]) for row in task_rows}
            tasks: list[dict[str, Any]] = []
            for row in task_rows:
                parent_id = row["parent_task_id"]
                if parent_id is not None and int(parent_id) not in task_ids:
                    raise BackupIntegrityError("任务层级指向其他用户或备份外任务，无法安全完整导出。")
                raw_tags = row["tags"]
                tags = [part.strip() for part in raw_tags.split(",") if part.strip()] if raw_tags else None
                tasks.append({
                    "id": int(row["id"]),
                    "title": row["title"],
                    "status": row["status"],
                    "priority": row["priority"],
                    "due_at": row["due_at"],
                    "estimated_minutes": row["estimated_minutes"],
                    "notes": row["notes"],
                    "parent_task_id": int(parent_id) if parent_id is not None else None,
                    "recurrence": row["recurrence"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "tags": tags,
                })

            relation_rows = _execute(store, conn, "SELECT * FROM task_relations WHERE user_id = ? ORDER BY id", (user_id,), dialect).fetchall()
            relations: list[dict[str, Any]] = []
            for row in relation_rows:
                source_id = int(row["source_task_id"])
                target_id = int(row["target_task_id"])
                if source_id not in task_ids or target_id not in task_ids:
                    raise BackupIntegrityError("任务关系指向其他用户或备份外任务，无法安全完整导出。")
                relations.append({
                    "source_task_id": source_id,
                    "target_task_id": target_id,
                    "relation_type": row["relation_type"],
                    "created_at": row["created_at"],
                })

            event_rows = _execute(
                store, conn,
                "SELECT e.id, e.task_id, e.event_type, e.payload, e.created_at FROM task_events e "
                "JOIN tasks t ON t.id = e.task_id WHERE t.user_id = ? ORDER BY e.id",
                (user_id,), dialect,
            ).fetchall()
            events = [{
                "task_id": int(row["task_id"]),
                "event_type": row["event_type"],
                "payload": row["payload"],
                "created_at": row["created_at"],
            } for row in event_rows]
            key_column = "`key`" if dialect == "mysql" else "key"
            memory_rows = _execute(
                store, conn,
                f"SELECT {key_column}, value, updated_at FROM user_memory WHERE user_id = ? ORDER BY {key_column}",
                (user_id,), dialect,
            ).fetchall()
            memory: dict[str, str] = {}
            memory_updated_at: dict[str, str] = {}
            excluded_memory_count = 0
            for row in memory_rows:
                key = row["key"]
                if _is_credential_key(key):
                    excluded_memory_count += 1
                    continue
                memory[key] = row["value"]
                memory_updated_at[key] = row["updated_at"]

            result = {
                "version": "2.0",
                "exported_at": utcnow().isoformat(),
                "user_id": user_id,
                "tasks": tasks,
                "relations": relations,
                "events": events,
                "memory": memory,
                "memory_updated_at": memory_updated_at,
                "excluded_events": {"count": None, "reason": UNATTRIBUTED_EVENTS_REASON},
                "excluded_memory": {"count": excluded_memory_count, "reason": EXCLUDED_CREDENTIAL_MEMORY_REASON},
            }
            _validate_v2(result)
            return result
        except BaseException:
            conn.rollback()
            raise
