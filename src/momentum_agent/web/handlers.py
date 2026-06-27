"""Web handlers — 从 MomentumHandler 中提取的处理方法。"""
from __future__ import annotations

import asyncio
import json
import time
from http import HTTPStatus
from importlib.resources import files
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from .server import MomentumHandler

_login_attempts: dict[str, tuple[int, float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300


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
    from .utils import extract_task_id
    task_id = extract_task_id(handler, path, "done")
    if task_id is None:
        return
    store = handler.store
    next_task = store.complete_recurring_task(task_id, user_id=user_id)
    if next_task and next_task.recurrence:
        handler.send_json({"message": f"已创建下一期任务 #{next_task.id}：{next_task.title}"})
    elif next_task:
        handler.send_json({"message": f"已完成任务 #{next_task.id}：{next_task.title}"})
    else:
        handler.send_json({"error": "没有找到这个任务。"}, HTTPStatus.NOT_FOUND)


def handle_edit_task(handler: MomentumHandler, path: str, user_id: str) -> None:
    from ..agent_app import edit_task_from_params
    try:
        task_id = int(path.strip("/").split("/")[2])
    except (IndexError, ValueError):
        handler.send_json({"error": "任务 ID 无效。"}, HTTPStatus.BAD_REQUEST)
        return
    payload = handler.read_json()
    message = edit_task_from_params(
        handler.store, task_id,
        title=payload.get("title"), due_at=payload.get("due_at"),
        priority=payload.get("priority"), estimated_minutes=payload.get("estimated_minutes"),
        notes=payload.get("notes"), tags=payload.get("tags"), user_id=user_id,
    )
    handler.send_json({"message": message})


def handle_postpone_task(handler: MomentumHandler, path: str, user_id: str) -> None:
    from ..agent_app import postpone_task_cmd
    from .utils import extract_task_id
    task_id = extract_task_id(handler, path, "postpone")
    if task_id is None:
        return
    payload = handler.read_json()
    days = int(payload.get("days", 3))
    handler.send_json({"message": postpone_task_cmd(handler.store, task_id, days, user_id=user_id)})


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
    attempts, lockout_until = _login_attempts.get(client_ip, (0, 0.0))
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
        new_attempts = attempts + 1
        if new_attempts >= MAX_LOGIN_ATTEMPTS:
            _login_attempts[client_ip] = (new_attempts, now + LOGIN_LOCKOUT_SECONDS)
            handler.send_json(
                {"error": f"登录失败次数过多，请 {LOGIN_LOCKOUT_SECONDS} 秒后再试"},
                HTTPStatus.TOO_MANY_REQUESTS,
            )
        else:
            _login_attempts[client_ip] = (new_attempts, 0.0)
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
    clear_conversation_history(user_id)
    handler.send_json({"message": "对话历史已清除"})

def handle_advice(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import local_advice
    handler.send_json({"advice": local_advice(handler.store, user_id=user_id)})


def handle_review(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import local_review
    handler.send_json({"review": local_review(handler.store, user_id=user_id)})


def handle_provider(handler: MomentumHandler, user_id: str) -> None:
    from ..agent_app import provider_status
    handler.send_json(provider_status(handler.store.get_all_memory(user_id=user_id)))


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
    data = handler.store.export_user_data(user_id=user_id)
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler._last_status = 200
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Disposition", f"attachment; filename=momentum-{user_id}.json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_import(handler: MomentumHandler, user_id: str) -> None:
    payload = handler.read_json()
    data = payload.get("data")
    if not data or not isinstance(data, dict):
        handler.send_json({"error": "请提供 JSON 数据。"}, HTTPStatus.BAD_REQUEST)
        return
    try:
        n = handler.store.import_user_data(data, user_id=user_id)
        handler.send_json({"message": f"已导入 {n} 个任务。"})
    except Exception as exc:
        handler.send_json({"error": f"导入失败：{exc}"}, HTTPStatus.BAD_REQUEST)


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
    store.update_last_heartbeat(user_id=user_id)
    handler.send_json({
        "suggestion": suggestion,
        "should_trigger": should_trigger,
        "config": store.get_heartbeat_config(user_id=user_id),
    })


# ── 天气 & 位置 ──────────────────────────────────────────────────


def handle_get_weather(handler: MomentumHandler, user_id: str, parsed) -> None:
    from ..services import weather as w
    from datetime import datetime
    query = parse_qs(parsed.query)
    city = query.get("city", [None])[0]
    if not city:
        saved_city = handler.store.get_memory("user_location", user_id=user_id)
        city = saved_city or "北京"
    data = w.get_weather(city)
    loc = w.get_location(city)
    handler.send_json({
        "city": data["city"],
        "country": "",
        "temperature": data["temperature"],
        "condition": data["condition"],
        "condition_cn": data["condition_cn"],
        "emoji": data["emoji"],
        "recommendations": data["tips"],
        "latitude": loc["latitude"],
        "longitude": loc["longitude"],
        "updated_at": datetime.now().isoformat(),
    })


def handle_get_location(handler: MomentumHandler, user_id: str, parsed) -> None:
    from ..services import weather as w
    query = parse_qs(parsed.query)
    city = query.get("city", [None])[0]
    if not city:
        saved_city = handler.store.get_memory("user_location", user_id=user_id)
        city = saved_city or "北京"
    info = w.get_location(city)
    handler.send_json({
        "city": info["city"],
        "country": "",
        "latitude": info["latitude"],
        "longitude": info["longitude"],
    })


def handle_get_user_location(handler: MomentumHandler, user_id: str) -> None:
    city = handler.store.get_memory("user_location", user_id=user_id)
    if not city:
        handler.send_json({"city": "北京", "is_default": True})
    else:
        handler.send_json({"city": city, "is_default": False})


def handle_set_user_location(handler: MomentumHandler, user_id: str) -> None:
    payload = handler.read_json()
    city = payload.get("city")
    if not city:
        handler.send_json({"error": "需要提供城市名称"}, HTTPStatus.BAD_REQUEST)
        return
    handler.store.set_memory("user_location", city, user_id=user_id)
    handler.send_json({"message": f"已设置默认位置为：{city}", "city": city})


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

def handle_start_focus(handler: MomentumHandler, user_id: str) -> None:
    """开始一个专注时段"""
    payload = handler.read_json()
    task_id = payload.get("task_id")
    duration_minutes = int(payload.get("duration_minutes", 25))
    if duration_minutes < 1 or duration_minutes > 120:
        handler.send_json({"error": "时长需在 1-120 分钟之间"}, HTTPStatus.BAD_REQUEST)
        return
    from ..web.utils import encode_dt
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    handler.store.record_focus_session(task_id, duration_minutes, user_id=user_id)
    handler.send_json({
        "message": f"专注计时开始，{duration_minutes}分钟后提醒",
        "started_at": now.isoformat(),
        "duration_minutes": duration_minutes,
    })


def handle_get_focus_stats(handler: MomentumHandler, user_id: str) -> None:
    """获取专注统计数据"""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    sessions = handler.store.get_focus_sessions(user_id=user_id)
    recent = [s for s in sessions if s["started_at"] >= week_ago]
    total_minutes = sum(s.get("duration_minutes", 0) for s in recent)
    total_sessions = len(recent)
    handler.send_json({
        "sessions": recent,
        "total_minutes_today": sum(s.get("duration_minutes", 0) for s in sessions if s["started_at"] >= now.replace(hour=0, minute=0, second=0)),
        "total_minutes_week": total_minutes,
        "total_sessions_week": total_sessions,
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
        if t.status == TaskStatus.DONE:
            updated_key = t.updated_at.strftime("%m-%d")
            if updated_key in daily_done:
                daily_done[updated_key] += 1

    # 优先级分布
    priority_counts = {"high": 0, "medium": 0, "low": 0}
    for t in tasks:
        if t.status in (TaskStatus.TODO, TaskStatus.DOING):
            priority_counts[t.priority.value] += 1

    # 完成时段分布（24小时）
    hourly_done = {str(h): 0 for h in range(24)}
    for t in tasks:
        if t.status == TaskStatus.DONE:
            hourly_done[str(t.updated_at.hour)] += 1

    # 每周模式
    weekly_pattern = engine.get_weekly_pattern(user_id)

    # 专注分钟数（最近14天）
    focus_daily: dict[str, int] = {}
    for i in range(13, -1, -1):
        d = now - timedelta(days=i)
        focus_daily[d.strftime("%m-%d")] = 0
    sessions = handler.store.get_focus_sessions(user_id=user_id)
    for s in sessions:
        key = s["started_at"].strftime("%m-%d")
        if key in focus_daily:
            focus_daily[key] += s.get("duration_minutes", 0)

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
