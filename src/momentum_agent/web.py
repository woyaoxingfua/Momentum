from __future__ import annotations

import asyncio
import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .agent_app import (
    create_plan_from_text,
    create_task_from_text,
    drop_task_cmd,
    edit_task_from_params,
    get_user_config_cmd,
    local_advice,
    local_review,
    postpone_task_cmd,
    provider_status,
    reopen_task_cmd,
    run_agent_message,
    run_agent_message_stream,
    set_user_config_cmd,
    start_task_cmd,
)
from .auth import hash_password
from .config import get_current_user
from .logger import get_logger, init_from_env
from .models import Task, TaskStatus
from .storage import DEFAULT_USER, TaskStore

log = get_logger("web")


def _get_user_id(handler: MomentumHandler) -> str | None:
    token = handler.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return None
    store = TaskStore(handler.db_path)
    return store.validate_session(token)


def _require_auth(handler: MomentumHandler):
    user_id = _get_user_id(handler)
    if user_id is None:
        handler.send_json({"error": "请先登录"}, HTTPStatus.UNAUTHORIZED)
        return None
    return user_id


class MomentumHandler(BaseHTTPRequestHandler):
    db_path: Path

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_GET(self) -> None:
        t0 = time.time()
        parsed = urlparse(self.path)
        try:
            # static files — no auth
            if parsed.path in ("/", "/login.html", "/app.css", "/app.js") or parsed.path.startswith("/js/"):
                if parsed.path == "/":
                    self.send_static("index.html", "text/html; charset=utf-8")
                elif parsed.path == "/login.html":
                    self.send_static("login.html", "text/html; charset=utf-8")
                elif parsed.path == "/app.css":
                    self.send_static("app.css", "text/css; charset=utf-8")
                elif parsed.path == "/app.js":
                    self.send_static("app.js", "text/javascript; charset=utf-8")
                else:
                    name = parsed.path.split("/")[-1]
                    self.send_static(f"js/{name}", "text/javascript; charset=utf-8")
                return

            # API endpoints — require auth
            user_id = _require_auth(self)
            if user_id is None:
                return

            if parsed.path == "/api/tasks":
                query = parse_qs(parsed.query)
                if "q" in query:
                    self.handle_search_tasks(query["q"][0], user_id)
                else:
                    status = query.get("status", ["todo"])[0]
                    self.handle_list_tasks(status, user_id)
            elif parsed.path == "/api/export":
                self.handle_export(user_id)
            elif parsed.path == "/api/advice":
                self.send_json({"advice": local_advice(TaskStore(self.db_path), user_id=user_id)})
            elif parsed.path == "/api/review":
                self.send_json({"review": local_review(TaskStore(self.db_path), user_id=user_id)})
            elif parsed.path == "/api/provider":
                self.send_json({"provider": provider_status()})
            elif parsed.path == "/api/config":
                self.send_json({"config": get_user_config_cmd(TaskStore(self.db_path), user_id=user_id)})
            elif parsed.path == "/api/me":
                self.send_json({"user_id": user_id})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        finally:
            log.info("%s %s → %d (%.0fms)", self.command, parsed.path, self._last_status or 200, (time.time() - t0) * 1000)

    def do_POST(self) -> None:
        t0 = time.time()
        parsed = urlparse(self.path)
        try:
            # auth endpoints — no auth required
            if parsed.path in ("/api/register", "/api/login", "/api/logout"):
                if parsed.path == "/api/register":
                    self.handle_register()
                elif parsed.path == "/api/login":
                    self.handle_login()
                else:
                    self.handle_logout()
                return

            # all other endpoints require auth
            user_id = _require_auth(self)
            if user_id is None:
                return

            if parsed.path == "/api/tasks":
                self.handle_create_task(user_id)
            elif parsed.path == "/api/plan":
                self.handle_create_plan(user_id)
            elif parsed.path == "/api/change-password":
                self.handle_change_password(user_id)
            elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/done"):
                self.handle_done_task(parsed.path)
            elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/postpone"):
                self.handle_postpone_task(parsed.path)
            elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/drop"):
                self.handle_drop_task(parsed.path)
            elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/start"):
                self.handle_start_task(parsed.path)
            elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/reopen"):
                self.handle_reopen_task(parsed.path)
            elif parsed.path == "/api/chat":
                self.handle_chat(user_id)
            elif parsed.path == "/api/chat/stream":
                self.handle_chat_stream(user_id)
            elif parsed.path == "/api/config":
                self.handle_set_config(user_id)
            elif parsed.path == "/api/import":
                self.handle_import(user_id)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        finally:
            log.info("%s %s → %d (%.0fms)", self.command, parsed.path, self._last_status or 200, (time.time() - t0) * 1000)

    def do_PUT(self) -> None:
        t0 = time.time()
        parsed = urlparse(self.path)
        user_id = _require_auth(self)
        if user_id is None:
            return
        try:
            if parsed.path.startswith("/api/tasks/") and "/done" not in parsed.path and "/postpone" not in parsed.path and "/drop" not in parsed.path:
                self.handle_edit_task(parsed.path, user_id)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        finally:
            log.info("%s %s → %d (%.0fms)", self.command, parsed.path, self._last_status or 200, (time.time() - t0) * 1000)

    _last_status: int = 200

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        self._last_status = status.value
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, status: HTTPStatus) -> None:
        self._last_status = status.value
        super().send_error(status)

    def handle_list_tasks(self, status: str, user_id: str) -> None:
        chosen = TaskStatus(status) if status in TaskStatus._value2member_map_ else TaskStatus.TODO
        tasks = TaskStore(self.db_path).list_tasks(chosen, user_id=user_id)
        self.send_json({"tasks": [task_to_json(task) for task in tasks]})

    def handle_create_task(self, user_id: str) -> None:
        payload = self.read_json()
        text = str(payload.get("text", "")).strip()
        if not text:
            self.send_json({"error": "任务内容不能为空。"}, HTTPStatus.BAD_REQUEST)
            return
        store = TaskStore(self.db_path)
        message = create_task_from_text(store, text, user_id=user_id)
        self.send_json({"message": message, "tasks": [task_to_json(t) for t in store.list_tasks(user_id=user_id)]})

    def handle_create_plan(self, user_id: str) -> None:
        payload = self.read_json()
        text = str(payload.get("text", "")).strip()
        if not text:
            self.send_json({"error": "任务内容不能为空。"}, HTTPStatus.BAD_REQUEST)
            return
        store = TaskStore(self.db_path)
        message = create_plan_from_text(store, text, user_id=user_id)
        self.send_json({"message": message, "tasks": [task_to_json(t) for t in store.list_tasks(user_id=user_id)]})

    def handle_done_task(self, path: str) -> None:
        task_id = self._extract_task_id(path, "done")
        if task_id is None:
            return
        store = TaskStore(self.db_path)
        next_task = store.complete_recurring_task(task_id)
        if next_task and next_task.recurrence:
            self.send_json({"message": f"已创建下一期任务 #{next_task.id}：{next_task.title}"})
        elif next_task:
            self.send_json({"message": f"已完成任务 #{next_task.id}：{next_task.title}"})
        else:
            self.send_json({"error": "没有找到这个任务。"}, HTTPStatus.NOT_FOUND)

    def handle_edit_task(self, path: str, user_id: str) -> None:
        try:
            task_id = int(path.strip("/").split("/")[2])
        except (IndexError, ValueError):
            self.send_json({"error": "任务 ID 无效。"}, HTTPStatus.BAD_REQUEST)
            return
        payload = self.read_json()
        message = edit_task_from_params(
            TaskStore(self.db_path), task_id,
            title=payload.get("title"), due_at=payload.get("due_at"),
            priority=payload.get("priority"), estimated_minutes=payload.get("estimated_minutes"),
            notes=payload.get("notes"), user_id=user_id,
        )
        self.send_json({"message": message})

    def handle_postpone_task(self, path: str) -> None:
        task_id = self._extract_task_id(path, "postpone")
        if task_id is None:
            return
        payload = self.read_json()
        days = int(payload.get("days", 3))
        self.send_json({"message": postpone_task_cmd(TaskStore(self.db_path), task_id, days)})

    def handle_drop_task(self, path: str) -> None:
        task_id = self._extract_task_id(path, "drop")
        if task_id is None:
            return
        self.send_json({"message": drop_task_cmd(TaskStore(self.db_path), task_id)})

    def handle_start_task(self, path: str) -> None:
        task_id = self._extract_task_id(path, "start")
        if task_id is None:
            return
        self.send_json({"message": start_task_cmd(TaskStore(self.db_path), task_id)})

    def handle_reopen_task(self, path: str) -> None:
        task_id = self._extract_task_id(path, "reopen")
        if task_id is None:
            return
        self.send_json({"message": reopen_task_cmd(TaskStore(self.db_path), task_id)})

    def _extract_task_id(self, path: str, suffix: str) -> int | None:
        parts = path.strip("/").split("/")
        try:
            idx = parts.index(suffix)
            task_id = int(parts[idx - 1])
        except (IndexError, ValueError):
            self.send_json({"error": "任务 ID 无效。"}, HTTPStatus.BAD_REQUEST)
            return None
        return task_id

    def handle_chat(self, user_id: str) -> None:
        payload = self.read_json()
        message = str(payload.get("message", "")).strip()
        if not message:
            self.send_json({"error": "消息不能为空。"}, HTTPStatus.BAD_REQUEST)
            return
        response = asyncio.run(run_agent_message(self.db_path, message, user_id=user_id))
        self.send_json({"message": response})

    def handle_chat_stream(self, user_id: str) -> None:
        payload = self.read_json()
        message = str(payload.get("message", "")).strip()
        if not message:
            self.send_json({"error": "消息不能为空。"}, HTTPStatus.BAD_REQUEST)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        asyncio.run(self._stream_chat(message, user_id))

    async def _stream_chat(self, message: str, user_id: str) -> None:
        try:
            async for chunk in run_agent_message_stream(self.db_path, message, user_id=user_id):
                data = json.dumps({"chunk": chunk}, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except Exception as exc:
            log.error("stream error: %s", exc)
            error_data = json.dumps({"error": str(exc)}, ensure_ascii=False)
            self.wfile.write(f"data: {error_data}\n\n".encode("utf-8"))
            self.wfile.flush()

    def handle_register(self) -> None:
        payload = self.read_json()
        user_id = str(payload.get("user_id", "")).strip()
        display_name = str(payload.get("display_name", "")).strip()
        password = str(payload.get("password", "")).strip()
        if not user_id or not password:
            self.send_json({"error": "用户名和密码不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        if len(password) < 4:
            self.send_json({"error": "密码至少 4 位"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            TaskStore(self.db_path).register_user(user_id, display_name or user_id, hash_password(password))
            self.send_json({"message": "注册成功，请登录"})
        except Exception:
            self.send_json({"error": "用户名已存在"}, HTTPStatus.CONFLICT)

    def handle_login(self) -> None:
        payload = self.read_json()
        user_id = str(payload.get("user_id", "")).strip()
        password = str(payload.get("password", "")).strip()
        if not user_id or not password:
            self.send_json({"error": "用户名和密码不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        token = TaskStore(self.db_path).login_user(user_id, password)
        if not token:
            self.send_json({"error": "用户名或密码错误"}, HTTPStatus.UNAUTHORIZED)
            return
        self.send_json({"token": token, "user_id": user_id})

    def handle_logout(self) -> None:
        token = self.headers.get("Authorization", "").replace("Bearer ", "")
        if token:
            TaskStore(self.db_path).logout_user(token)
        self.send_json({"message": "已登出"})

    def handle_change_password(self, user_id: str) -> None:
        payload = self.read_json()
        old_pw = str(payload.get("old_password", "")).strip()
        new_pw = str(payload.get("new_password", "")).strip()
        if not old_pw or not new_pw:
            self.send_json({"error": "请提供旧密码和新密码"}, HTTPStatus.BAD_REQUEST)
            return
        if len(new_pw) < 4:
            self.send_json({"error": "新密码至少 4 位"}, HTTPStatus.BAD_REQUEST)
            return
        ok = TaskStore(self.db_path).change_password(user_id, old_pw, new_pw)
        if not ok:
            self.send_json({"error": "旧密码错误"}, HTTPStatus.FORBIDDEN)
            return
        self.send_json({"message": "密码已修改"})

    def handle_set_config(self, user_id: str) -> None:
        payload = self.read_json()
        key = str(payload.get("key", "")).strip()
        value = str(payload.get("value", "")).strip()
        if not key:
            self.send_json({"error": "配置键不能为空。"}, HTTPStatus.BAD_REQUEST)
            return
        message = set_user_config_cmd(TaskStore(self.db_path), key, value, user_id=user_id)
        self.send_json({"message": message})

    def handle_search_tasks(self, query: str, user_id: str) -> None:
        results = TaskStore(self.db_path).search_tasks(query, user_id=user_id)
        self.send_json({"tasks": [task_to_json(t) for t in results]})

    def handle_export(self, user_id: str) -> None:
        data = TaskStore(self.db_path).export_user_data(user_id=user_id)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._last_status = 200
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename=momentum-{user_id}.json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_import(self, user_id: str) -> None:
        payload = self.read_json()
        data = payload.get("data")
        if not data or not isinstance(data, dict):
            self.send_json({"error": "请提供 JSON 数据。"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            n = TaskStore(self.db_path).import_user_data(data, user_id=user_id)
            self.send_json({"message": f"已导入 {n} 个任务。"})
        except Exception as exc:
            self.send_json({"error": f"导入失败：{exc}"}, HTTPStatus.BAD_REQUEST)

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        result = value if isinstance(value, dict) else {}
        self._cached_body = result
        return result

    def send_static(self, filename: str, content_type: str) -> None:
        static_file = files("momentum_agent").joinpath("static", filename)
        body = static_file.read_bytes()
        self._last_status = 200
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.debug("http: " + format % args)


def task_to_json(task: Task) -> dict[str, object]:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status.value,
        "priority": task.priority.value,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "estimated_minutes": task.estimated_minutes,
        "notes": task.notes,
        "parent_task_id": task.parent_task_id,
        "recurrence": task.recurrence,
        "user_id": task.user_id,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def run_server(db_path: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    init_from_env()
    handler = type("ConfiguredMomentumHandler", (MomentumHandler,), {"db_path": db_path})
    server = ThreadingHTTPServer((host, port), handler)
    log.info("server listening at http://%s:%s", host, port)
    print(f"Momentum Task Agent running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("server stopped by user")
        print("\nServer stopped.")
    finally:
        server.server_close()
