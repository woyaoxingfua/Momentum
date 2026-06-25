"""Web 服务器 — 轻量级 HTTP handler，路由分发到 handlers 模块。"""
from __future__ import annotations

import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..logger import get_logger, init_from_env, request_context, log_api_request

log = get_logger("web")


def _get_user_id(handler: MomentumHandler) -> str | None:
    token = handler.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return None
    from ..storage import TaskStore
    return TaskStore(handler.db_path).validate_session(token)


def _require_auth(handler: MomentumHandler):
    user_id = _get_user_id(handler)
    if user_id is None:
        handler.send_json({"error": "请先登录"}, HTTPStatus.UNAUTHORIZED)
        return None
    return user_id


class MomentumHandler(BaseHTTPRequestHandler):
    db_path: Path
    _last_status: int = 200
    protocol_version = "HTTP/1.1"  # SSE 流式输出需要 HTTP/1.1

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

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

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def log_message(self, format: str, *args: object) -> None:
        log.debug("http: " + format % args)

    # ── GET ──────────────────────────────────────────────────────

    # ── 静态文件路由表 ─────────────────────────────────────────────
    STATIC_MAP = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/login.html": ("login.html", "text/html; charset=utf-8"),
        "/app.css": ("app.css", "text/css; charset=utf-8"),
        "/manifest.json": ("manifest.json", "application/manifest+json; charset=utf-8"),
        "/icon.svg": ("icon.svg", "image/svg+xml"),
    }

    def _send_static_or_none(self, path: str) -> bool:
        from . import handlers
        if path in self.STATIC_MAP:
            name, ct = self.STATIC_MAP[path]
            handlers.send_static(self, name, ct)
            return True
        if path.startswith("/js/"):
            handlers.send_static(self, f"js/{path.split('/')[-1]}", "text/javascript; charset=utf-8")
            return True
        return False

    def do_GET(self) -> None:
        from . import handlers
        t0 = time.time()
        parsed = urlparse(self.path)
        with request_context():
            try:
                path = parsed.path

                # 静态文件 — 无需认证
                if self._send_static_or_none(path):
                    return

                # API — 需要认证
                user_id = _require_auth(self)
                if user_id is None:
                    return

                query = parse_qs(parsed.query)

                # 精确匹配路由表
                exact_routes = {
                    "/api/tags": handlers.handle_get_all_tags,
                    "/api/heartbeat/config": handlers.handle_get_heartbeat_config,
                    "/api/heartbeat/suggestion": handlers.handle_get_heartbeat_suggestion,
                    "/api/user/location": handlers.handle_get_user_location,
                    "/api/export": handlers.handle_export,
                    "/api/advice": handlers.handle_advice,
                    "/api/review": handlers.handle_review,
                    "/api/provider": handlers.handle_provider,
                    "/api/config": handlers.handle_get_config,
                }

                # 前缀匹配路由表 (suffix -> handler)
                prefix_routes = {
                    "/subtasks": handlers.handle_get_subtasks,
                    "/with-subtasks": handlers.handle_get_task_with_subtasks,
                    "/dependencies": handlers.handle_get_dependencies,
                    "/dependents": handlers.handle_get_dependents,
                    "/relations": handlers.handle_get_task_relations,
                    "/is-blocked": handlers.handle_is_task_blocked,
                }

                if path == "/api/tasks":
                    if "q" in query:
                        handlers.handle_search_tasks(self, query["q"][0], user_id)
                    elif "tag" in query:
                        handlers.handle_get_tasks_by_tag(self, query["tag"][0], user_id)
                    else:
                        handlers.handle_list_tasks(self, query.get("status", ["todo"])[0], user_id)
                elif path == "/api/weather":
                    handlers.handle_get_weather(self, user_id, parsed)
                elif path == "/api/location":
                    handlers.handle_get_location(self, user_id, parsed)
                elif path == "/api/me":
                    self.send_json({"user_id": user_id})
                elif path in exact_routes:
                    exact_routes[path](self, user_id)
                elif path.startswith("/api/tasks/"):
                    suffix = path.rsplit("/", 1)[-1]
                    if suffix in prefix_routes:
                        prefix_routes[suffix](self, path, user_id)
                    else:
                        self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            finally:
                log_api_request("GET", parsed.path, self._last_status or 200, (time.time() - t0) * 1000)

    # ── POST ──────────────────────────────────────────────────────

    def do_POST(self) -> None:
        from . import handlers
        t0 = time.time()
        parsed = urlparse(self.path)
        with request_context():
            try:
                path = parsed.path

                # 认证端点 — 无需认证
                public_routes = {
                    "/api/register": handlers.handle_register,
                    "/api/login": handlers.handle_login,
                    "/api/logout": handlers.handle_logout,
                }
                if path in public_routes:
                    public_routes[path](self)
                    return

                # 其他端点 — 需要认证
                user_id = _require_auth(self)
                if user_id is None:
                    return

                exact_routes = {
                    "/api/tasks": handlers.handle_create_task,
                    "/api/plan": handlers.handle_create_plan,
                    "/api/change-password": handlers.handle_change_password,
                    "/api/batch/update-status": handlers.handle_batch_update_status,
                    "/api/batch/add-tags": handlers.handle_batch_add_tags,
                    "/api/heartbeat/config": handlers.handle_set_heartbeat_config,
                    "/api/user/location": handlers.handle_set_user_location,
                    "/api/chat": handlers.handle_chat,
                    "/api/chat/stream": handlers.handle_chat_stream,
                    "/api/chat/clear": handlers.handle_chat_clear,
                    "/api/config": handlers.handle_set_config,
                    "/api/import": handlers.handle_import,
                }

                prefix_routes = {
                    "/done": handlers.handle_done_task,
                    "/postpone": handlers.handle_postpone_task,
                    "/drop": handlers.handle_drop_task,
                    "/start": handlers.handle_start_task,
                    "/reopen": handlers.handle_reopen_task,
                    "/subtasks": handlers.handle_create_subtask,
                    "/bulk-subtasks": handlers.handle_bulk_create_subtasks,
                    "/dependencies": handlers.handle_add_dependency,
                    "/relations": handlers.handle_add_task_relation,
                }

                if path in exact_routes:
                    exact_routes[path](self, user_id)
                elif path.startswith("/api/tasks/"):
                    suffix = path.rsplit("/", 1)[-1]
                    if suffix in prefix_routes:
                        prefix_routes[suffix](self, path, user_id)
                    else:
                        self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            finally:
                log_api_request("POST", parsed.path, self._last_status or 200, (time.time() - t0) * 1000)

    # ── PUT ──────────────────────────────────────────────────────

    def do_PUT(self) -> None:
        from . import handlers
        t0 = time.time()
        parsed = urlparse(self.path)
        user_id = _require_auth(self)
        if user_id is None:
            return
        with request_context():
            try:
                path = parsed.path
                # PUT /api/tasks/<id> 编辑任务；需排除 POST 专用的 action 后缀
                if (
                    path.startswith("/api/tasks/")
                    and path.rsplit("/", 1)[-1] not in {"done", "postpone", "drop", "start", "reopen", "subtasks", "bulk-subtasks", "dependencies", "relations"}
                ):
                    handlers.handle_edit_task(self, path, user_id)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            finally:
                log_api_request("PUT", parsed.path, self._last_status or 200, (time.time() - t0) * 1000)


def run_server(db_path: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    init_from_env()
    from ..storage import TaskStore
    TaskStore.ensure_schema(db_path)
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
