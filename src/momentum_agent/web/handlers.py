"""Web handlers — 从 MomentumHandler 中提取的处理方法。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from http import HTTPStatus
from importlib.resources import files
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from .server import MomentumHandler

_login_attempts: dict[str, tuple[int, float, float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300
MAX_TRACKED_LOGIN_CLIENTS = 4096


def _prune_login_attempts(now: float) -> None:
    """清理登录失败记录。

    以前这个字典只增不减：任何一次失败登录都会留下一个 IP 条目，
    长期运行就是一个无界增长的内存泄漏（也是可被放大的攻击面）。
    这里先清掉「锁定已过期且长时间没有活动」的条目；仍然超过上限时按最久未活动裁剪。
    """
    for client_ip, (_attempts, lockout_until, seen) in list(_login_attempts.items()):
        if lockout_until <= now and now - seen > LOGIN_LOCKOUT_SECONDS:
            _login_attempts.pop(client_ip, None)
    overflow = len(_login_attempts) - MAX_TRACKED_LOGIN_CLIENTS
    if overflow > 0:
        oldest = sorted(_login_attempts.items(), key=lambda item: item[1][2])[:overflow]
        for client_ip, _value in oldest:
            _login_attempts.pop(client_ip, None)


def _login_attempt_state(client_ip: str, now: float) -> tuple[int, float]:
    raw = _login_attempts.get(client_ip)
    if raw is None:
        return 0, 0.0
    if len(raw) == 3:
        attempts, lockout_until, _seen = raw
        return attempts, lockout_until
    attempts, lockout_until = raw  # 兼容旧的两元组记录
    return attempts, lockout_until


def _record_login_failure(client_ip: str, attempts: int, now: float) -> None:
    next_attempts = attempts + 1
    lockout_until = now + LOGIN_LOCKOUT_SECONDS if next_attempts >= MAX_LOGIN_ATTEMPTS else 0.0
    _login_attempts[client_ip] = (next_attempts, lockout_until, now)
    _prune_login_attempts(now)


# ── 静态文件 ──────────────────────────────────────────────────────

def send_static(handler: MomentumHandler, filename: str, content_type: str) -> None:
    static_file = files("momentum_agent").joinpath("static", filename)
    body = static_file.read_bytes()
    handler._last_status = 200
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ── 任务 CRUD ──────────────────────────────────────────────────────

def handle_list_tasks(handler: MomentumHandler, status: str, user_id: str, *, sort: str = "default") -> None:
    from ..models import TaskStatus
    from ..context import build_user_context, ranked_tasks
    chosen = TaskStatus(status) if status in TaskStatus._value2member_map_ else TaskStatus.TODO
    tasks = handler.store.list_tasks(chosen, user_id=user_id)
    if sort == "score" and tasks:
        # 读取用户工作配置作为排序上下文
        prefs = handler.store.get_all_memory(user_id=user_id)
        daily_capacity = int(prefs.get("daily_capacity_minutes", "") or 45)
        work_start = prefs.get("working_hours_start") or "09:00"
        work_end = prefs.get("working_hours_end") or "18:00"
        context = build_user_context(tasks, daily_capacity_minutes=daily_capacity, working_hours_start=work_start, working_hours_end=work_end)
        tasks = ranked_tasks(tasks, context)
    from .utils import task_to_json
    handler.send_json({"tasks": [task_to_json(t) for t in tasks]})


def handle_create_task(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import create_task_from_text
    from .utils import task_to_json
    payload = handler.read_json()
    text = str(payload.get("text", "")).strip()
    images = payload.get("images", [])
    if not text and not images:
        handler.send_json({"error": "任务内容不能为空。"}, HTTPStatus.BAD_REQUEST)
        return
    store = handler.store
    message = create_task_from_text(store, text, user_id=user_id, images=images if images else None)
    handler.send_json({"message": message, "tasks": [task_to_json(t) for t in store.list_tasks(user_id=user_id)]})


def handle_create_plan(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import create_plan_from_text
    from .utils import task_to_json
    payload = handler.read_json()
    text = str(payload.get("text", "")).strip()
    if not text:
        handler.send_json({"error": "任务内容不能为空。"}, HTTPStatus.BAD_REQUEST)
        return
    store = handler.store
    message = create_plan_from_text(store, text, user_id=user_id)
    handler.send_json({"message": message, "tasks": [task_to_json(t) for t in store.list_tasks(user_id=user_id)]})


def handle_done_task(handler: MomentumHandler, path: str, user_id: str) -> None:
    import uuid
    from .utils import extract_task_id

    supplied_key = handler.headers.get("Idempotency-Key")
    if not supplied_key:
        handler.send_json({"error": "idempotency_key_required"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        parsed_key = uuid.UUID(supplied_key)
    except (AttributeError, TypeError, ValueError):
        parsed_key = None
    if parsed_key is None or parsed_key.version != 4 or str(parsed_key) != supplied_key.lower():
        handler.send_json({"error": "idempotency_key_invalid"}, HTTPStatus.BAD_REQUEST)
        return

    task_id = extract_task_id(handler, path, "done")
    if task_id is None:
        return
    canonical_request = json.dumps(
        {"method": "POST", "task_id": task_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hashlib.sha256(canonical_request).hexdigest()
    status, response = handler.store.complete_task_idempotent(
        task_id,
        user_id=user_id,
        idempotency_key=str(parsed_key),
        request_fingerprint=fingerprint,
    )
    handler.send_json(response, HTTPStatus(status))


def handle_edit_task(handler: MomentumHandler, path: str, user_id: str) -> None:
    from ..agent_app import edit_task_from_params
    try:
        task_id = int(path.strip("/").split("/")[2])
    except (IndexError, ValueError):
        handler.send_json({"error": "任务 ID 无效。"}, HTTPStatus.BAD_REQUEST)
        return
    if handler.store.get_task_for_user(task_id, user_id) is None:
        handler.send_json({"error": "没有找到这个任务。"}, HTTPStatus.NOT_FOUND)
        return
    payload = handler.read_json()
    message = edit_task_from_params(
        handler.store, task_id,
        title=payload.get("title"), due_at=payload.get("due_at"),
        priority=payload.get("priority"), estimated_minutes=payload.get("estimated_minutes"),
        notes=payload.get("notes"), tags=payload.get("tags"), user_id=user_id,
    )
    if message.startswith("没有找到任务 #"):
        handler.send_json({"error": "没有找到这个任务。"}, HTTPStatus.NOT_FOUND)
        return
    handler.send_json({"message": message})


def handle_postpone_task(handler: MomentumHandler, path: str, user_id: str) -> None:
    import uuid
    from .utils import extract_task_id

    supplied_key = handler.headers.get("Idempotency-Key")
    if not supplied_key:
        handler.send_json({"error": "idempotency_key_required"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        parsed_key = uuid.UUID(supplied_key)
    except (AttributeError, TypeError, ValueError):
        parsed_key = None
    if parsed_key is None or parsed_key.version != 4 or str(parsed_key) != supplied_key.lower():
        handler.send_json({"error": "idempotency_key_invalid"}, HTTPStatus.BAD_REQUEST)
        return

    task_id = extract_task_id(handler, path, "postpone")
    if task_id is None:
        return
    payload = handler.read_json()
    days = payload.get("days", 3)
    canonical_request = json.dumps(
        {"method": "POST", "task_id": task_id, "days": days},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hashlib.sha256(canonical_request).hexdigest()
    status, response = handler.store.postpone_task_idempotent(
        task_id,
        days,
        user_id=user_id,
        idempotency_key=str(parsed_key),
        request_fingerprint=fingerprint,
    )
    handler.send_json(response, HTTPStatus(status))


def handle_drop_task(handler: MomentumHandler, path: str, user_id: str) -> None:
    from ..agent_app import drop_task_cmd
    from .utils import extract_task_id
    task_id = extract_task_id(handler, path, "drop")
    if task_id is None:
        return
    handler.send_json({"message": drop_task_cmd(handler.store, task_id, user_id=user_id)})


def handle_start_task(handler: MomentumHandler, path: str, user_id: str) -> None:
    from ..agent_app import start_task_cmd
    from .utils import extract_task_id
    task_id = extract_task_id(handler, path, "start")
    if task_id is None:
        return
    handler.send_json({"message": start_task_cmd(handler.store, task_id, user_id=user_id)})


def handle_reopen_task(handler: MomentumHandler, path: str, user_id: str) -> None:
    from ..agent_app import reopen_task_cmd
    from .utils import extract_task_id
    task_id = extract_task_id(handler, path, "reopen")
    if task_id is None:
        return
    handler.send_json({"message": reopen_task_cmd(handler.store, task_id, user_id=user_id)})


def handle_search_tasks(handler: MomentumHandler, query: str, user_id: str) -> None:
    from .utils import task_to_json
    results = handler.store.search_tasks(query, user_id=user_id)
    handler.send_json({"tasks": [task_to_json(t) for t in results]})


# ── 历史常完成任务 ──────────────────────────────────────────────

def handle_frequent_tasks(handler: MomentumHandler, path: str, user_id: str) -> None:
    """GET /api/tasks/frequent — 近 180 天真实完成事件聚合出的「常完成任务」。"""
    from ..frequent import DEFAULT_HISTORY_DAYS, MIN_DISTINCT_TASKS, aggregate_frequent_tasks, history_since
    rows = handler.store.list_completed_task_history(user_id=user_id, since=history_since())
    tasks = aggregate_frequent_tasks(rows)
    handler.send_json({
        "tasks": [item.to_dict() for item in tasks],
        "window_days": DEFAULT_HISTORY_DAYS,
        "min_distinct_tasks": MIN_DISTINCT_TASKS,
    })


def handle_recreate_frequent_task(handler: MomentumHandler, user_id: str) -> None:
    """POST /api/frequent/recreate — 「再来一个」：只复制标题、优先级、估时、标签。"""
    from ..frequent import find_frequent_task, history_since
    from ..models import Priority
    from .utils import task_to_json
    payload = handler.read_json()
    key = str(payload.get("key", "")).strip()
    if not key:
        handler.send_json({"error": "缺少要重现的任务标识。"}, HTTPStatus.BAD_REQUEST)
        return
    rows = handler.store.list_completed_task_history(user_id=user_id, since=history_since())
    match = find_frequent_task(rows, key)
    if match is None:
        handler.send_json({"error": "没有找到对应的历史常完成任务。"}, HTTPStatus.NOT_FOUND)
        return
    priority = Priority(match.priority) if match.priority in Priority._value2member_map_ else Priority.MEDIUM
    task = handler.store.create_task(
        match.title,
        priority=priority,
        estimated_minutes=match.estimated_minutes,
        tags=match.tags,
        user_id=user_id,
    )
    handler.send_json({
        "message": f"已创建任务 #{task.id}：{task.title}",
        "task": task_to_json(task),
    })


# ── 认证 ──────────────────────────────────────────────────────────

def handle_register(handler: MomentumHandler) -> None:
    from ..auth import hash_password
    payload = handler.read_json()
    user_id = str(payload.get("user_id", "")).strip()
    display_name = str(payload.get("display_name", "")).strip()
    password = str(payload.get("password", "")).strip()
    if not user_id or not password:
        handler.send_json({"error": "用户名和密码不能为空"}, HTTPStatus.BAD_REQUEST)
        return
    if len(user_id) < 2 or len(user_id) > 64:
        handler.send_json({"error": "用户名长度需在 2-64 位之间"}, HTTPStatus.BAD_REQUEST)
        return
    if len(password) < 8:
        handler.send_json({"error": "密码至少 8 位"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        handler.store.register_user(user_id, display_name or user_id, hash_password(password))
        handler.send_json({"message": "注册成功，请登录"})
    except Exception:
        handler.send_json({"error": "用户名已存在"}, HTTPStatus.CONFLICT)


def handle_login(handler: MomentumHandler) -> None:
    client_ip = handler.client_address[0] if handler.client_address else "unknown"
    now = time.time()
    attempts, lockout_until = _login_attempt_state(client_ip, now)
    if now < lockout_until:
        remaining = int(lockout_until - now)
        handler.send_json(
            {"error": f"登录失败次数过多，请 {remaining} 秒后再试"},
            HTTPStatus.TOO_MANY_REQUESTS,
        )
        return

    payload = handler.read_json()
    user_id = str(payload.get("user_id", "")).strip()
    password = str(payload.get("password", "")).strip()
    if not user_id or not password:
        handler.send_json({"error": "用户名和密码不能为空"}, HTTPStatus.BAD_REQUEST)
        return
    token = handler.store.login_user(user_id, password)
    if not token:
        _record_login_failure(client_ip, attempts, now)
        _, lockout_until = _login_attempt_state(client_ip, now)
        if lockout_until > now:
            handler.send_json(
                {"error": f"登录失败次数过多，请 {LOGIN_LOCKOUT_SECONDS} 秒后再试"},
                HTTPStatus.TOO_MANY_REQUESTS,
            )
        else:
            handler.send_json({"error": "用户名或密码错误"}, HTTPStatus.UNAUTHORIZED)
        return
    _login_attempts.pop(client_ip, None)
    handler.send_json({"token": token, "user_id": user_id})


def handle_logout(handler: MomentumHandler) -> None:
    token = handler.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        handler.store.logout_user(token)
    handler.send_json({"message": "已登出"})


def handle_change_password(handler: MomentumHandler, user_id: str) -> None:
    payload = handler.read_json()
    old_pw = str(payload.get("old_password", "")).strip()
    new_pw = str(payload.get("new_password", "")).strip()
    if not old_pw or not new_pw:
        handler.send_json({"error": "请提供旧密码和新密码"}, HTTPStatus.BAD_REQUEST)
        return
    if len(new_pw) < 8:
        handler.send_json({"error": "新密码至少 8 位"}, HTTPStatus.BAD_REQUEST)
        return
    ok = handler.store.change_password(user_id, old_pw, new_pw)
    if not ok:
        handler.send_json({"error": "旧密码错误"}, HTTPStatus.FORBIDDEN)
        return
    handler.send_json({"message": "密码已修改"})


# ── 配置 ──────────────────────────────────────────────────────────

def handle_get_config(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import get_user_config_cmd
    handler.send_json({"config": get_user_config_cmd(handler.store, user_id=user_id)})


def handle_set_config(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import set_user_config_cmd
    payload = handler.read_json()
    key = str(payload.get("key", "")).strip()
    value = str(payload.get("value", "")).strip()
    if not key:
        handler.send_json({"error": "配置键不能为空。"}, HTTPStatus.BAD_REQUEST)
        return
    message = set_user_config_cmd(handler.store, key, value, user_id=user_id)
    handler.send_json({"message": message})


# ── Agent 对话 ──────────────────────────────────────────────────────

def handle_chat(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import run_agent_message
    payload = handler.read_json()
    message = str(payload.get("message", "")).strip()
    if not message:
        handler.send_json({"error": "消息不能为空。"}, HTTPStatus.BAD_REQUEST)
        return
    response = asyncio.run(run_agent_message(handler.database_url, message, user_id=user_id))
    handler.send_json({"message": response})


def handle_chat_stream(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import run_agent_message_stream
    from ..logger import get_logger
    log = get_logger("web")
    payload = handler.read_json()
    message = str(payload.get("message", "")).strip()
    if not message:
        handler.send_json({"error": "消息不能为空。"}, HTTPStatus.BAD_REQUEST)
        return
    log.info("chat_stream start: user=%r msg=%r", user_id, message[:80])
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    handler.close_connection = True  # 确保流结束后关闭连接

    async def _stream():
        try:
            async for event in run_agent_message_stream(handler.database_url, message, user_id=user_id):
                # event 是 dict，直接序列化为 SSE
                data = json.dumps(event, ensure_ascii=False)
                handler.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                handler.wfile.flush()
            log.info("chat_stream done: user=%r", user_id)
        except Exception as exc:
            log.error("stream error: %s", exc, exc_info=True)
            error_data = json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)
            handler.wfile.write(f"data: {error_data}\n\n".encode("utf-8"))
            handler.wfile.flush()

    asyncio.run(_stream())


# ── 建议 & 复盘 ──────────────────────────────────────────────────

def handle_chat_clear(handler: MomentumHandler, user_id: str) -> None:
    """清除用户的对话历史"""
    from ..agent_app import clear_conversation_history
    clear_conversation_history(user_id, store=handler.store)
    handler.send_json({"message": "对话历史已清除"})

def handle_advice(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import local_advice
    from ..agent_app import _read_preferences
    from ..context import build_user_context, ranked_tasks
    from ..models import TaskStatus

    advice = local_advice(handler.store, user_id=user_id)
    tasks = handler.store.list_tasks(status=None, user_id=user_id)
    open_tasks = [task for task in tasks if task.status in (TaskStatus.TODO, TaskStatus.DOING)]
    suggestion = None
    if open_tasks:
        context = build_user_context(open_tasks, **_read_preferences(handler.store, user_id=user_id))
        task = ranked_tasks(open_tasks, context)[0]
        suggestion = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "estimated_minutes": task.estimated_minutes,
        }
    handler.send_json({"advice": advice, "suggestion": suggestion})


def handle_review(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import local_review
    from datetime import date, datetime, time, timedelta, timezone
    import json
    import re
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from ..storage.focus_utils import parse_timestamp

    query = parse_qs(urlsplit(getattr(handler, "path", "/api/review")).query, keep_blank_values=True)
    zone_values = query.get("timeZone", [])
    date_values = query.get("localDate", [])
    if len(zone_values) != 1 or len(date_values) != 1:
        handler.send_json({"error": "请提供唯一的 timeZone 和 localDate 参数"}, HTTPStatus.BAD_REQUEST)
        return
    zone_key, local_date_raw = zone_values[0], date_values[0]
    if not zone_key or len(zone_key) > 128 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_date_raw):
        handler.send_json({"error": "时区或日期格式无效"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        zone = ZoneInfo(zone_key)
        local_date = date.fromisoformat(local_date_raw)
        if local_date.isoformat() != local_date_raw:
            raise ValueError("non-canonical date")
        next_date = local_date + timedelta(days=1)
        day_start = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(timezone.utc)
        day_end = datetime.combine(next_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    except (ValueError, ZoneInfoNotFoundError, OverflowError):
        handler.send_json({"error": "时区或日期无效"}, HTTPStatus.BAD_REQUEST)
        return

    snapshot = handler.store.get_review_data(user_id=user_id)
    completed_events = []
    last_status_by_task = {}
    status_events = sorted(snapshot["status_events"], key=lambda event: event.get("event_id", 0))
    for event in status_events:
        task_id = event["task_id"]
        status_value = event.get("status_value")
        if status_value not in {"todo", "doing", "done", "dropped"}:
            continue
        previous_status = last_status_by_task.get(task_id)
        last_status_by_task[task_id] = status_value
        if status_value != "done" or previous_status == "done":
            continue
        completed_at = parse_timestamp(event.get("completed_at"))
        if completed_at is None or not day_start <= completed_at < day_end:
            continue
        completed_events.append({
            "task_id": task_id,
            "completed_at": completed_at.isoformat(),
            "title": event["title"],
            "estimated_minutes_reference": event.get("estimated_minutes_reference"),
            "_sort_at": completed_at,
        })
    completed_events.sort(key=lambda item: (item["_sort_at"], item["task_id"]))
    for event in completed_events:
        del event["_sort_at"]

    total_actual_seconds = 0
    focus_session_count = 0
    for session in snapshot["focus_sessions"]:
        try:
            payload = json.loads(session.get("payload") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        raw_seconds = payload.get("actual_seconds")
        if isinstance(raw_seconds, bool) or not isinstance(raw_seconds, (int, str)):
            continue
        try:
            if isinstance(raw_seconds, str) and not re.fullmatch(r"\d+", raw_seconds):
                continue
            actual_seconds = int(raw_seconds)
        except (TypeError, ValueError):
            continue
        if actual_seconds < 0:
            continue
        ended_at = parse_timestamp(payload.get("ended_at"))
        if ended_at is None or not day_start <= ended_at < day_end:
            continue
        total_actual_seconds += actual_seconds
        focus_session_count += 1

    handler.send_json({
        "review": local_review(handler.store, user_id=user_id),
        "timeZone": zone_key,
        "localDate": local_date_raw,
        "dayStartUtc": day_start.isoformat(),
        "dayEndUtcExclusive": day_end.isoformat(),
        "completed_count": len(completed_events),
        "completed_events": completed_events,
        "today_focus_actual_seconds": total_actual_seconds,
        "today_focus_session_count": focus_session_count,
        "focus_attribution_note": "专注时长按 ended_at 所在本地日整段归属，不跨日拆分；只统计带 ended_at 和 actual_seconds 的会话，不用预估时长补值。",
    })


def handle_provider(handler: MomentumHandler, user_id: str) -> None:
    try:
        from ..agent_app import provider_status
        handler.send_json(provider_status(handler.store.get_all_memory(user_id=user_id)))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        handler.send_json({"error": str(exc), "provider": "unknown", "configured": False})


def handle_provider_models(handler: MomentumHandler, user_id: str) -> None:
    """List available models from the configured provider (Ollama only for now)."""
    from ..agent_app import load_provider_config

    config = load_provider_config(handler.store.get_all_memory(user_id=user_id))
    if not config.is_ollama:
        handler.send_json({"models": []})
        return

    base_url = (config.base_url or "").removesuffix("/v1")
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            f"{base_url}/api/tags",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        handler.send_json({"models": models})
    except Exception as exc:
        handler.send_json({"error": f"无法获取 Ollama 模型列表：{exc}", "models": []})


# ── 导出导入 ──────────────────────────────────────────────────────

def handle_export(handler: MomentumHandler, user_id: str) -> None:
    from .server import MAX_BACKUP_SIZE_BYTES

    data = handler.store.export_user_data(user_id=user_id)
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_BACKUP_SIZE_BYTES:
        handler.close_connection = True
        max_mib = MAX_BACKUP_SIZE_BYTES // 1024 // 1024
        handler.send_json(
            {"error": f"导出备份过大，序列化后最大为 {max_mib} MiB ({MAX_BACKUP_SIZE_BYTES:,} bytes)"},
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        )
        return
    handler._last_status = 200
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Disposition", f"attachment; filename=momentum-{user_id}.json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_import(handler: MomentumHandler, user_id: str) -> None:
    from ..storage.backup import BackupError, BackupNotEmptyError
    from .server import MAX_BACKUP_SIZE_BYTES

    payload = handler.read_json(max_body_size=MAX_BACKUP_SIZE_BYTES)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data:
        handler.send_json({"error": "请提供 JSON 数据。"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        if data.get("version") == "2.0":
            result = handler.store.restore_user_data(data, user_id=user_id)
            handler.send_json({
                "message": f"已恢复 {result['imported_tasks']} 个任务。",
                "excluded_events": result["excluded_events"],
                "excluded_memory": result["excluded_memory"],
            })
            return
        result = handler.store.import_user_data_with_summary(data, user_id=user_id)
        handler.send_json({
            "message": f"已导入 {result['imported_tasks']} 个任务。",
            "excluded_memory": result["excluded_memory"],
        })
    except BackupNotEmptyError as exc:
        handler.send_json({"error": f"恢复失败：{exc}"}, HTTPStatus.CONFLICT)
    except BackupError as exc:
        handler.send_json({"error": f"导入失败：{exc}"}, HTTPStatus.BAD_REQUEST)
    except Exception:
        handler.send_json({"error": "导入失败：备份未写入，请稍后重试。"}, HTTPStatus.INTERNAL_SERVER_ERROR)


# ── 标签 ──────────────────────────────────────────────────────────

def handle_get_all_tags(handler: MomentumHandler, user_id: str) -> None:
    tags = handler.store.get_all_tags(user_id=user_id)
    handler.send_json({"tags": tags})


def handle_get_tasks_by_tag(handler: MomentumHandler, tag: str, user_id: str) -> None:
    from .utils import task_to_json
    tasks = handler.store.get_tasks_by_tag(tag, user_id=user_id)
    handler.send_json({"tasks": [task_to_json(t) for t in tasks]})


# ── 批量操作 ──────────────────────────────────────────────────────

def handle_batch_update_status(handler: MomentumHandler, user_id: str) -> None:
    from ..models import TaskStatus
    payload = handler.read_json()
    task_ids = payload.get("task_ids", [])
    status_str = payload.get("status")
    if not task_ids or not isinstance(task_ids, list) or not status_str:
        handler.send_json({"error": "请提供 task_ids 数组和 status。"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        status = TaskStatus(status_str)
    except ValueError:
        handler.send_json({"error": "无效的 status。"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        updated = handler.store.batch_update_status(
            [int(tid) for tid in task_ids], status, user_id=user_id
        )
        handler.send_json({"message": f"已更新 {updated} 个任务。"})
    except Exception as exc:
        handler.send_json({"error": f"批量更新失败：{exc}"}, HTTPStatus.BAD_REQUEST)


def handle_batch_add_tags(handler: MomentumHandler, user_id: str) -> None:
    payload = handler.read_json()
    task_ids = payload.get("task_ids", [])
    tags = payload.get("tags", [])
    if not task_ids or not isinstance(task_ids, list) or not tags or not isinstance(tags, list):
        handler.send_json({"error": "请提供 task_ids 和 tags 数组。"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        updated = handler.store.batch_add_tags(
            [int(tid) for tid in task_ids], tags, user_id=user_id
        )
        handler.send_json({"message": f"已更新 {updated} 个任务。"})
    except Exception as exc:
        handler.send_json({"error": f"批量添加标签失败：{exc}"}, HTTPStatus.BAD_REQUEST)


# ── 心跳 ──────────────────────────────────────────────────────────

def handle_get_heartbeat_config(handler: MomentumHandler, user_id: str) -> None:
    config = handler.store.get_heartbeat_config(user_id=user_id)
    handler.send_json({"config": config})


def handle_set_heartbeat_config(handler: MomentumHandler, user_id: str) -> None:
    payload = handler.read_json()
    store = handler.store
    config = store.set_heartbeat_config(
        enabled=payload.get("enabled"),
        start_hour=payload.get("start_hour"),
        end_hour=payload.get("end_hour"),
        interval_hours=payload.get("interval_hours"),
        user_id=user_id,
    )
    status = "已启用" if config["enabled"] else "已禁用"
    handler.send_json({"status": status, "config": config})


def handle_get_heartbeat_suggestion(handler: MomentumHandler, user_id: str) -> None:
    from ..context import build_user_context, heartbeat_suggestion
    store = handler.store
    tasks = store.list_tasks(status=None, user_id=user_id)
    ctx = build_user_context(tasks)
    suggestion = heartbeat_suggestion(tasks, ctx)
    should_trigger = store.should_trigger_heartbeat(user_id=user_id)
    if should_trigger:
        # 只有真正投递出建议才推进计时器。前端每 60 秒就会问一次，
        # 若无条件更新 last_heartbeat_at，间隔永远攒不满，心跳只会出现一次。
        store.update_last_heartbeat(user_id=user_id)
    handler.send_json({
        "suggestion": suggestion,
        "should_trigger": should_trigger,
        "config": store.get_heartbeat_config(user_id=user_id),
    })


# ── 天气 & 位置 ──────────────────────────────────────────────────


def handle_get_weather(handler: MomentumHandler, user_id: str, parsed) -> None:
    from ..services import weather as w
    query = parse_qs(parsed.query)
    city = query.get("city", [None])[0]
    if not city:
        saved_city = handler.store.get_memory("user_location", user_id=user_id)
        city = saved_city or "北京"
        country = handler.store.get_memory("user_location_country", user_id=user_id)
        if country and city not in w.CITIES and city.casefold() not in w.ALIASES:
            city = f"{city}, {country}"
    try:
        data = w.get_weather(city)
    except w.CityNotFoundError as exc:
        handler.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        return
    except w.WeatherServiceError as exc:
        handler.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        return
    handler.send_json({
        "city": data["city"],
        "country": data["country"],
        "temperature": data["temperature"],
        "humidity": data["humidity"],
        "condition": data["condition"],
        "condition_cn": data["condition_cn"],
        "emoji": data["emoji"],
        "recommendations": data["tips"],
        "latitude": data["latitude"],
        "longitude": data["longitude"],
        "weather_code": data["weather_code"],
        "source": data["source"],
        "updated_at": data["updated_at"],
    })


def handle_get_location(handler: MomentumHandler, user_id: str, parsed) -> None:
    from ..services import weather as w
    query = parse_qs(parsed.query)
    city = query.get("city", [None])[0]
    if not city:
        saved_city = handler.store.get_memory("user_location", user_id=user_id)
        city = saved_city or "北京"
        country = handler.store.get_memory("user_location_country", user_id=user_id)
        if country and city not in w.CITIES and city.casefold() not in w.ALIASES:
            city = f"{city}, {country}"
    try:
        info = w.get_location(city)
    except w.CityNotFoundError as exc:
        handler.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        return
    except w.WeatherServiceError as exc:
        handler.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        return
    handler.send_json({
        "city": info["city"],
        "country": info["country"],
        "latitude": info["latitude"],
        "longitude": info["longitude"],
    })


def handle_get_user_location(handler: MomentumHandler, user_id: str) -> None:
    city = handler.store.get_memory("user_location", user_id=user_id)
    country = handler.store.get_memory("user_location_country", user_id=user_id) or ""
    if not city:
        handler.send_json({"city": "北京", "country": "", "is_default": True})
    else:
        handler.send_json({"city": city, "country": country, "is_default": False})


def handle_set_user_location(handler: MomentumHandler, user_id: str) -> None:
    payload = handler.read_json()
    city = str(payload.get("city") or "").strip()
    country = str(payload.get("country") or "").strip()
    if not city:
        handler.send_json({"error": "需要提供城市名称"}, HTTPStatus.BAD_REQUEST)
        return
    if len(city) > 160 or len(country) > 100:
        handler.send_json({"error": "城市名称或国家名称过长"}, HTTPStatus.BAD_REQUEST)
        return
    coordinates = {}
    for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180)):
        value = payload.get(key)
        if value is not None:
            try:
                number = float(value)
                if not low <= number <= high:
                    raise ValueError
                coordinates[key] = str(number)
            except (TypeError, ValueError):
                handler.send_json({"error": "城市坐标无效"}, HTTPStatus.BAD_REQUEST)
                return
    handler.store.set_memory("user_location", city, user_id=user_id)
    handler.store.set_memory("user_location_country", country, user_id=user_id)
    for key, value in coordinates.items():
        handler.store.set_memory(f"user_location_{key}", value, user_id=user_id)
    handler.send_json({"message": f"已设置默认位置为：{city}", "city": city, "country": country})


def handle_search_cities(handler: MomentumHandler, parsed) -> None:
    from ..services import weather as w
    query = parse_qs(parsed.query).get("q", [""])[0].strip()
    if len(query) < 2:
        handler.send_json({"cities": []})
        return
    if len(query) > 100:
        handler.send_json({"error": "城市搜索内容不能超过 100 个字符"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        handler.send_json({"cities": w.search_cities(query)})
    except w.WeatherServiceError as exc:
        handler.send_json({"error": f"城市搜索暂不可用：{exc}"}, HTTPStatus.BAD_GATEWAY)


def _validate_synced_background_url(value: object) -> str:
    from ipaddress import ip_address
    from urllib.parse import urlsplit

    raw = str(value or "").strip()
    if not raw or len(raw) > 2000:
        raise ValueError("公开图片 URL 不能为空且不能超过 2000 个字符")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("背景图片只接受无账号密码的 HTTP(S) URL")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("背景图片 URL 必须指向公开主机")
    try:
        address = ip_address(host)
        if not address.is_global:
            raise ValueError("背景图片 URL 必须指向公开主机")
    except ValueError as exc:
        if "公开主机" in str(exc):
            raise
        # A non-IP DNS host is expected; its image is fetched by the browser, not this server.
    return raw


def handle_get_preferences(handler: MomentumHandler, user_id: str) -> None:
    store = handler.store
    source = store.get_memory("background_source", user_id=user_id)
    url = store.get_memory("background_url", user_id=user_id) or ""
    opacity = store.get_memory("background_opacity", user_id=user_id) or "15"
    theme = store.get_memory("ui_theme", user_id=user_id) or "paper"
    handler.send_json({
        "theme": theme,
        "background": {
            "configured": source is not None,
            "source": source,
            "url": url if source == "remote_url" else "",
            "opacity": opacity,
        },
    })


def handle_set_preferences(handler: MomentumHandler, user_id: str) -> None:
    payload = handler.read_json()
    unknown = set(payload) - {"theme", "background"}
    if unknown:
        handler.send_json({"error": "偏好设置包含不支持的字段"}, HTTPStatus.BAD_REQUEST)
        return
    store = handler.store
    theme = None
    background_values = None
    if "theme" in payload:
        theme = str(payload.get("theme") or "")
        if theme not in {"paper", "ink", "sage", "midnight"}:
            handler.send_json({"error": "不支持的账户主题"}, HTTPStatus.BAD_REQUEST)
            return
    if "background" in payload:
        background = payload.get("background")
        if not isinstance(background, dict) or set(background) - {"source", "url", "opacity"}:
            handler.send_json({"error": "背景设置格式无效"}, HTTPStatus.BAD_REQUEST)
            return
        source = str(background.get("source") or "")
        if source not in {"remote_url", "local", "none"}:
            handler.send_json({"error": "不支持的背景来源"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            opacity = int(background.get("opacity", 15))
            if not 0 <= opacity <= 100:
                raise ValueError
            url = _validate_synced_background_url(background.get("url")) if source == "remote_url" else ""
            if source != "remote_url" and background.get("url"):
                raise ValueError("本地图片内容或 data URL 不会保存到服务器")
        except (TypeError, ValueError) as exc:
            handler.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        background_values = (source, url, str(opacity))
    if theme is not None:
        store.set_memory("ui_theme", theme, user_id=user_id)
    if background_values is not None:
        source, url, opacity = background_values
        store.set_memory("background_source", source, user_id=user_id)
        store.set_memory("background_url", url, user_id=user_id)
        store.set_memory("background_opacity", opacity, user_id=user_id)
    handler.send_json({"message": "偏好已保存", "theme": store.get_memory("ui_theme", user_id=user_id) or "paper"})


# ── 子任务 ──────────────────────────────────────────────────────

def handle_get_subtasks(handler: MomentumHandler, path: str, user_id: str) -> None:
    from .utils import extract_task_id_from_path, task_to_json
    task_id = extract_task_id_from_path(path)
    if task_id < 0:
        handler.send_json({"error": "无效的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    subtasks = handler.store.get_subtasks(task_id, user_id=user_id)
    handler.send_json({"subtasks": [task_to_json(t) for t in subtasks]})


def handle_get_task_with_subtasks(handler: MomentumHandler, path: str, user_id: str) -> None:
    from .utils import extract_task_id_from_path, task_to_json
    task_id = extract_task_id_from_path(path)
    if task_id < 0:
        handler.send_json({"error": "无效的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    task = handler.store.get_task_with_subtasks(task_id, user_id=user_id)
    if not task:
        handler.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
        return
    handler.send_json({
        "task": task_to_json(task),
        "subtasks": [task_to_json(t) for t in task.subtasks or []]
    })


def handle_create_subtask(handler: MomentumHandler, path: str, user_id: str) -> None:
    from ..models import Priority
    from ..parser import parse_task_text
    from .utils import extract_task_id_from_path, task_to_json
    task_id = extract_task_id_from_path(path)
    if task_id < 0:
        handler.send_json({"error": "无效的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    payload = handler.read_json()
    title = payload.get("title")
    if not title:
        handler.send_json({"error": "需要提供任务标题"}, HTTPStatus.BAD_REQUEST)
        return
    due_at_str = payload.get("due_at")
    priority_str = payload.get("priority", "medium")
    priority = Priority(priority_str) if priority_str in Priority._value2member_map_ else Priority.MEDIUM
    parsed = parse_task_text(f"{due_at_str or ''} {title}")
    chosen_priority = priority if priority_str in Priority._value2member_map_ else parsed.priority
    estimated_minutes = payload.get("estimated_minutes") or parsed.estimated_minutes
    task = handler.store.create_subtask(
        task_id, title, due_at=parsed.due_at, priority=chosen_priority,
        estimated_minutes=estimated_minutes, notes=payload.get("notes"),
        tags=payload.get("tags"), user_id=user_id,
    )
    handler.send_json({"message": f"已创建子任务 #{task.id}", "task": task_to_json(task)})


def handle_bulk_create_subtasks(handler: MomentumHandler, path: str, user_id: str) -> None:
    from .utils import extract_task_id_from_path, task_to_json
    task_id = extract_task_id_from_path(path)
    if task_id < 0:
        handler.send_json({"error": "无效的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    payload = handler.read_json()
    subtasks = payload.get("subtasks", [])
    if not subtasks:
        handler.send_json({"error": "需要提供子任务列表"}, HTTPStatus.BAD_REQUEST)
        return
    created = handler.store.bulk_create_subtasks(task_id, subtasks, user_id=user_id)
    handler.send_json({
        "message": f"已创建 {len(created)} 个子任务",
        "tasks": [task_to_json(t) for t in created]
    })


# ── 任务关系 ──────────────────────────────────────────────────────

def handle_get_dependencies(handler: MomentumHandler, path: str, user_id: str) -> None:
    from .utils import extract_task_id_from_path, task_to_json
    task_id = extract_task_id_from_path(path)
    if task_id < 0:
        handler.send_json({"error": "无效的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    deps = handler.store.get_dependencies(task_id, user_id=user_id)
    handler.send_json({"dependencies": [task_to_json(t) for t in deps]})


def handle_get_dependents(handler: MomentumHandler, path: str, user_id: str) -> None:
    from .utils import extract_task_id_from_path, task_to_json
    task_id = extract_task_id_from_path(path)
    if task_id < 0:
        handler.send_json({"error": "无效的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    deps = handler.store.get_dependents(task_id, user_id=user_id)
    handler.send_json({"dependents": [task_to_json(t) for t in deps]})


def handle_get_task_relations(handler: MomentumHandler, path: str, user_id: str) -> None:
    from .utils import extract_task_id_from_path
    task_id = extract_task_id_from_path(path)
    if task_id < 0:
        handler.send_json({"error": "无效的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    relations = handler.store.get_task_relations(task_id, user_id=user_id)
    handler.send_json({"relations": [
        {"id": r.id, "source_task_id": r.source_task_id, "target_task_id": r.target_task_id,
         "relation_type": r.relation_type.value, "created_at": r.created_at.isoformat()}
        for r in relations
    ]})


def handle_add_dependency(handler: MomentumHandler, path: str, user_id: str) -> None:
    from .utils import extract_task_id_from_path
    task_id = extract_task_id_from_path(path)
    if task_id < 0:
        handler.send_json({"error": "无效的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    payload = handler.read_json()
    depends_on = payload.get("depends_on_task_id")
    if not depends_on:
        handler.send_json({"error": "需要提供依赖的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    relation = handler.store.add_dependency(task_id, depends_on, user_id=user_id)
    if not relation:
        handler.send_json({"error": "无法创建依赖关系"}, HTTPStatus.BAD_REQUEST)
        return
    handler.send_json({"message": f"已创建依赖：#{task_id} → #{depends_on}"})


def handle_add_task_relation(handler: MomentumHandler, path: str, user_id: str) -> None:
    from ..models import TaskRelationType
    from .utils import extract_task_id_from_path
    task_id = extract_task_id_from_path(path)
    if task_id < 0:
        handler.send_json({"error": "无效的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    payload = handler.read_json()
    target_id = payload.get("target_task_id")
    rel_type_str = payload.get("relation_type", "relates_to")
    if not target_id:
        handler.send_json({"error": "需要提供目标任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        rel_type = TaskRelationType(rel_type_str)
    except ValueError:
        handler.send_json({"error": "无效的关系类型"}, HTTPStatus.BAD_REQUEST)
        return
    relation = handler.store.add_task_relation(task_id, target_id, rel_type, user_id=user_id)
    if not relation:
        handler.send_json({"error": "无法创建关系"}, HTTPStatus.BAD_REQUEST)
        return
    handler.send_json({"message": f"已创建关系：#{task_id} {rel_type_str} #{target_id}"})


def handle_is_task_blocked(handler: MomentumHandler, path: str, user_id: str) -> None:
    from .utils import extract_task_id_from_path
    task_id = extract_task_id_from_path(path)
    if task_id < 0:
        handler.send_json({"error": "无效的任务ID"}, HTTPStatus.BAD_REQUEST)
        return
    is_blocked = handler.store.is_task_blocked(task_id, user_id=user_id)
    handler.send_json({"is_blocked": is_blocked})


# ── 专注计时 ──────────────────────────────────────────────────────

def _get_focus_task(handler: MomentumHandler, task_id: int, user_id: str):
    """Look up one task by authenticated owner; retain compatibility with test doubles."""
    from ..models import Task

    getter = getattr(handler.store, "get_task_for_user", None)
    if callable(getter):
        task = getter(task_id, user_id)
        if task is None or isinstance(task, Task):
            return task
    return next(
        (task for task in handler.store.list_tasks(status=None, user_id=user_id) if task.id == task_id),
        None,
    )

def handle_start_focus(handler: MomentumHandler, user_id: str) -> None:
    """开始一个专注时段"""
    import re
    import uuid

    payload = handler.read_json()
    try:
        raw_task_id = payload.get("task_id")
        raw_duration = payload.get("duration_minutes", 25)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, str))
            or (isinstance(value, str) and not re.fullmatch(r"\d+", value))
            for value in (raw_task_id, raw_duration)
        ):
            raise ValueError
        task_id = int(raw_task_id)
        duration_minutes = int(raw_duration)
    except (TypeError, ValueError):
        handler.send_json({"error": "请提供有效的任务和专注时长"}, HTTPStatus.BAD_REQUEST)
        return
    if task_id <= 0:
        handler.send_json({"error": "任务 ID 无效"}, HTTPStatus.BAD_REQUEST)
        return
    if duration_minutes < 1 or duration_minutes > 120:
        handler.send_json({"error": "时长需在 1-120 分钟之间"}, HTTPStatus.BAD_REQUEST)
        return

    task = _get_focus_task(handler, task_id, user_id)
    if task is None:
        handler.send_json({"error": "未找到该任务"}, HTTPStatus.NOT_FOUND)
        return
    from ..models import TaskStatus
    task_status = getattr(task, "status", TaskStatus.TODO)
    task_status = getattr(task_status, "value", task_status)
    if task_status not in {TaskStatus.TODO.value, TaskStatus.DOING.value}:
        handler.send_json({"error": "task_not_open"}, HTTPStatus.CONFLICT)
        return

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    handler.send_json({
        "message": f"专注计时开始，{duration_minutes}分钟后提醒",
        "session_id": uuid.uuid4().hex,
        "task_id": task_id,
        "started_at": now.isoformat(),
        "duration_minutes": duration_minutes,
    })


def handle_finish_focus(handler: MomentumHandler, user_id: str) -> None:
    """保存已结束专注段的真实活动时长；暂停时间由客户端单调时钟排除。"""
    import re
    from datetime import datetime, timedelta, timezone

    payload = handler.read_json()
    try:
        raw_task_id = payload.get("task_id")
        raw_planned = payload.get("planned_minutes")
        raw_actual = payload.get("actual_seconds")
        if type(raw_actual) is not int:
            raise ValueError
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, str))
            or (isinstance(value, str) and not re.fullmatch(r"\d+", value))
            for value in (raw_task_id, raw_planned)
        ):
            raise ValueError
        task_id = int(raw_task_id)
        planned_minutes = int(raw_planned)
        actual_seconds = raw_actual
        started_raw = payload.get("started_at")
        if not isinstance(started_raw, str):
            raise ValueError
        started_at = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        ended_raw = payload.get("ended_at")
        if not isinstance(ended_raw, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            ended_raw,
        ):
            raise ValueError
        ended_at = datetime.fromisoformat(ended_raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        handler.send_json({"error": "专注记录参数无效，ended_at 必须是带时区的 RFC3339 时间"}, HTTPStatus.BAD_REQUEST)
        return

    session_id = payload.get("session_id")
    outcome = payload.get("outcome")
    if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", session_id):
        handler.send_json({"error": "专注会话 ID 无效"}, HTTPStatus.BAD_REQUEST)
        return
    if task_id <= 0 or planned_minutes < 1 or planned_minutes > 120:
        handler.send_json({"error": "任务或计划时长无效"}, HTTPStatus.BAD_REQUEST)
        return
    if (
        started_at.tzinfo is None
        or ended_at.tzinfo is None
        or started_at.utcoffset() is None
        or ended_at.utcoffset() is None
        or started_at.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=5)
    ):
        handler.send_json({"error": "专注开始时间无效"}, HTTPStatus.BAD_REQUEST)
        return
    if ended_at.astimezone(timezone.utc) < started_at.astimezone(timezone.utc):
        handler.send_json({"error": "结束时间早于开始时间"}, HTTPStatus.BAD_REQUEST)
        return
    if ended_at.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=5):
        handler.send_json({"error": "结束时间不能过度超前"}, HTTPStatus.BAD_REQUEST)
        return
    if outcome not in {"completed", "stopped"}:
        handler.send_json({"error": "结束方式无效"}, HTTPStatus.BAD_REQUEST)
        return
    if actual_seconds < 0 or actual_seconds > planned_minutes * 60:
        handler.send_json({"error": "实际时长超出计划范围"}, HTTPStatus.BAD_REQUEST)
        return
    if outcome == "completed" and actual_seconds != planned_minutes * 60:
        handler.send_json({"error": "计时未到计划时长，不能标记为完成"}, HTTPStatus.BAD_REQUEST)
        return

    if _get_focus_task(handler, task_id, user_id) is None:
        handler.send_json({"error": "未找到该任务"}, HTTPStatus.NOT_FOUND)
        return

    from ..storage.errors import FocusTaskNotFound, IdempotencyConflict
    try:
        result = handler.store.record_focus_session(
            task_id,
            planned_minutes,
            user_id=user_id,
            actual_seconds=actual_seconds,
            planned_minutes=planned_minutes,
            started_at=started_at,
            ended_at=ended_at,
            outcome=outcome,
            session_id=session_id,
        )
    except IdempotencyConflict:
        handler.send_json({"error": "idempotency_conflict"}, HTTPStatus.CONFLICT)
        return
    except FocusTaskNotFound:
        handler.send_json({"error": "未找到该任务"}, HTTPStatus.NOT_FOUND)
        return
    canonical = result if isinstance(result, dict) else {
        "task_id": task_id,
        "session_id": session_id,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "ended_at": ended_at.astimezone(timezone.utc).isoformat(),
        "planned_minutes": planned_minutes,
        "actual_seconds": actual_seconds,
        "outcome": outcome,
    }
    handler.send_json({"message": "实际专注时长已保存", **canonical})


def handle_get_focus_stats(handler: MomentumHandler, user_id: str) -> None:
    """获取专注统计数据"""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    sessions = handler.store.get_focus_sessions(user_id=user_id)
    recent = [s for s in sessions if s["started_at"] >= week_ago]
    actual_recent = [s for s in recent if s.get("actual_seconds") is not None]
    legacy_recent = [s for s in recent if s.get("actual_seconds") is None]
    local_today_start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today_actual = [s for s in sessions if s.get("actual_seconds") is not None and s["started_at"] >= local_today_start]
    total_minutes = round(sum(s.get("actual_seconds", 0) for s in actual_recent) / 60, 1)
    today_minutes = round(sum(s.get("actual_seconds", 0) for s in today_actual) / 60, 1)
    serializable_recent = []
    for session in recent:
        item = dict(session)
        for field in ("started_at", "ended_at"):
            value = item.get(field)
            if isinstance(value, datetime):
                item[field] = value.isoformat()
        serializable_recent.append(item)
    handler.send_json({
        "sessions": serializable_recent,
        "total_minutes_today": today_minutes,
        "total_minutes_week": total_minutes,
        "total_sessions_week": len(actual_recent),
        "legacy_sessions_week": len(legacy_recent),
    })


def handle_get_upcoming_notifications(handler: MomentumHandler, user_id: str) -> None:
    """返回即将到来的任务提醒（未来 60 分钟内到期）"""
    from datetime import datetime, timedelta, timezone
    from ..models import TaskStatus
    now = datetime.now(timezone.utc)
    soon = now + timedelta(minutes=60)
    all_tasks = handler.store.list_tasks(status=None, user_id=user_id)
    result = []
    for t in all_tasks:
        if t.status not in (TaskStatus.TODO, TaskStatus.DOING) or not t.due_at:
            continue
        if now <= t.due_at <= soon:
            minutes_left = int((t.due_at - now).total_seconds() / 60)
            result.append({
                "id": t.id,
                "title": t.title,
                "due_at": t.due_at.isoformat(),
                "minutes_left": minutes_left,
                "priority": t.priority.value,
            })
    result.sort(key=lambda x: x["minutes_left"])
    handler.send_json({"notifications": result})

def handle_chat_history(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import plain_chat_history

    handler.send_json({"turns": plain_chat_history(user_id, store=handler.store)})


# ── 待确认操作（破坏性操作审批） ─────────────────────────────────

def handle_list_approvals(handler: MomentumHandler, user_id: str) -> None:
    from ..approvals import gated_tools, list_pending

    handler.send_json({
        "approvals": list_pending(handler.store, user_id),
        "gated_tools": list(gated_tools()),
    })


def handle_approve_approval(handler: MomentumHandler, user_id: str) -> None:
    from ..approvals import execute, take_pending

    payload = handler.read_json()
    approval_id = str(payload.get("id", "")).strip()
    if not approval_id:
        handler.send_json({"error": "缺少待确认编号。"}, HTTPStatus.BAD_REQUEST)
        return
    pending = take_pending(handler.store, user_id, approval_id)
    if pending is None:
        handler.send_json({"error": "没有找到这条待确认操作。"}, HTTPStatus.NOT_FOUND)
        return
    message = execute(handler.store, pending, user_id)
    handler.send_json({"message": message, "approval": pending})


def handle_reject_approval(handler: MomentumHandler, user_id: str) -> None:
    from ..approvals import take_pending

    payload = handler.read_json()
    approval_id = str(payload.get("id", "")).strip()
    if not approval_id:
        handler.send_json({"error": "缺少待确认编号。"}, HTTPStatus.BAD_REQUEST)
        return
    pending = take_pending(handler.store, user_id, approval_id)
    if pending is None:
        handler.send_json({"error": "没有找到这条待确认操作。"}, HTTPStatus.NOT_FOUND)
        return
    handler.send_json({"message": f"已取消：{pending.get('summary') or '该操作'}"})



def handle_get_stats(handler: MomentumHandler, user_id: str) -> None:
    """仪表盘统计 API：返回完成趋势、优先级分布、时段热力、专注趋势"""
    from datetime import datetime, timedelta, timezone
    from ..insights import InsightsEngine
    from ..models import TaskStatus

    now = datetime.now(timezone.utc)
    engine = InsightsEngine(handler.store)
    profile = engine.build_profile(user_id)
    tasks = handler.store.list_tasks(status=None, user_id=user_id)

    # 最近 14 天每日创建/完成数
    daily_created: dict[str, int] = {}
    daily_done: dict[str, int] = {}
    for i in range(13, -1, -1):
        d = now - timedelta(days=i)
        key = d.strftime("%m-%d")
        daily_created[key] = 0
        daily_done[key] = 0

    for t in tasks:
        created_key = t.created_at.strftime("%m-%d")
        if created_key in daily_created:
            daily_created[created_key] += 1

    completion_events = engine.get_completion_events(user_id, since=now - timedelta(days=13))
    for event in completion_events:
        completed_at = event["completed_at"].astimezone(timezone.utc)
        completed_key = completed_at.strftime("%m-%d")
        if completed_key in daily_done:
            daily_done[completed_key] += 1

    # 优先级分布
    priority_counts = {"high": 0, "medium": 0, "low": 0}
    for t in tasks:
        if t.status in (TaskStatus.TODO, TaskStatus.DOING):
            priority_counts[t.priority.value] += 1

    # 完成时段分布（24小时）
    hourly_done = {str(h): 0 for h in range(24)}
    for event in completion_events:
        completed_at = event["completed_at"].astimezone(timezone.utc)
        hourly_done[str(completed_at.hour)] += 1

    # 每周模式
    weekly_pattern = engine.get_weekly_pattern(user_id)

    # 专注分钟数（最近14天）
    focus_daily_seconds: dict[str, int] = {}
    for i in range(13, -1, -1):
        d = now - timedelta(days=i)
        focus_daily_seconds[d.strftime("%m-%d")] = 0
    sessions = handler.store.get_focus_sessions(user_id=user_id)
    for s in sessions:
        if s.get("actual_seconds") is None:
            continue
        key = s["started_at"].strftime("%m-%d")
        if key in focus_daily_seconds:
            focus_daily_seconds[key] += int(s["actual_seconds"])
    focus_daily = {key: round(seconds / 60, 1) for key, seconds in focus_daily_seconds.items()}

    handler.send_json({
        "profile": profile.to_dict(),
        "daily": {
            "labels": list(daily_created.keys()),
            "created": list(daily_created.values()),
            "done": list(daily_done.values()),
        },
        "priority": priority_counts,
        "hourly": hourly_done,
        "weekly": weekly_pattern,
        "focus": {
            "labels": list(focus_daily.keys()),
            "minutes": list(focus_daily.values()),
        },
        "generated_at": now.isoformat(),
    })
