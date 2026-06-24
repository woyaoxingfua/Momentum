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

    def do_GET(self) -> None:
        from . import handlers
        t0 = time.time()
        parsed = urlparse(self.path)
        with request_context():
            try:
                path = parsed.path

                # 静态文件 — 无需认证
                STATIC_MAP = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/login.html": ("login.html", "text/html; charset=utf-8"),
                    "/app.css": ("app.css", "text/css; charset=utf-8"),
                    "/manifest.json": ("manifest.json", "application/manifest+json; charset=utf-8"),
                    "/icon.svg": ("icon.svg", "image/svg+xml"),
                }
                if path in STATIC_MAP:
                    name, ct = STATIC_MAP[path]
                    handlers.send_static(self, name, ct)
                    return
                if path.startswith("/js/"):
                    handlers.send_static(self, f"js/{path.split('/')[-1]}", "text/javascript; charset=utf-8")
                    return

                # API — 需要认证
                user_id = _require_auth(self)
                if user_id is None:
                    return

                query = parse_qs(parsed.query)

                if path == "/api/tasks":
                    if "q" in query:
                        handlers.handle_search_tasks(self, query["q"][0], user_id)
                    elif "tag" in query:
                        handlers.handle_get_tasks_by_tag(self, query["tag"][0], user_id)
                    else:
                        handlers.handle_list_tasks(self, query.get("status", ["todo"])[0], user_id)
                elif path == "/api/tags":
                    handlers.handle_get_all_tags(self, user_id)
                elif path == "/api/heartbeat/config":
                    handlers.handle_get_heartbeat_config(self, user_id)
                elif path == "/api/heartbeat/suggestion":
                    handlers.handle_get_heartbeat_suggestion(self, user_id)
                elif path == "/api/user/location":
                    handlers.handle_get_user_location(self, user_id)
                elif path == "/api/weather":
                    handlers.handle_get_weather(self, user_id, parsed)
                elif path == "/api/location":
                    handlers.handle_get_location(self, user_id, parsed)
                elif path.startswith("/api/tasks/") and path.endswith("/subtasks"):
                    handlers.handle_get_subtasks(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/with-subtasks"):
                    handlers.handle_get_task_with_subtasks(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/dependencies"):
                    handlers.handle_get_dependencies(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/dependents"):
                    handlers.handle_get_dependents(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/relations"):
                    handlers.handle_get_task_relations(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/is-blocked"):
                    handlers.handle_is_task_blocked(self, path, user_id)
                elif path == "/api/export":
                    handlers.handle_export(self, user_id)
                elif path == "/api/advice":
                    handlers.handle_advice(self, user_id)
                elif path == "/api/review":
                    handlers.handle_review(self, user_id)
                elif path == "/api/provider":
                    handlers.handle_provider(self)
                elif path == "/api/config":
                    handlers.handle_get_config(self, user_id)
                elif path == "/api/me":
                    self.send_json({"user_id": user_id})
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
                if path == "/api/register":
                    handlers.handle_register(self)
                    return
                if path == "/api/login":
                    handlers.handle_login(self)
                    return
                if path == "/api/logout":
                    handlers.handle_logout(self)
                    return

                # 其他端点 — 需要认证
                user_id = _require_auth(self)
                if user_id is None:
                    return

                if path == "/api/tasks":
                    handlers.handle_create_task(self, user_id)
                elif path == "/api/plan":
                    handlers.handle_create_plan(self, user_id)
                elif path == "/api/change-password":
                    handlers.handle_change_password(self, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/done"):
                    handlers.handle_done_task(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/postpone"):
                    handlers.handle_postpone_task(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/drop"):
                    handlers.handle_drop_task(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/start"):
                    handlers.handle_start_task(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/reopen"):
                    handlers.handle_reopen_task(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/subtasks"):
                    handlers.handle_create_subtask(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/bulk-subtasks"):
                    handlers.handle_bulk_create_subtasks(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/dependencies"):
                    handlers.handle_add_dependency(self, path, user_id)
                elif path.startswith("/api/tasks/") and path.endswith("/relations"):
                    handlers.handle_add_task_relation(self, path, user_id)
                elif path == "/api/batch/update-status":
                    handlers.handle_batch_update_status(self, user_id)
                elif path == "/api/batch/add-tags":
                    handlers.handle_batch_add_tags(self, user_id)
                elif path == "/api/heartbeat/config":
                    handlers.handle_set_heartbeat_config(self, user_id)
                elif path == "/api/user/location":
                    handlers.handle_set_user_location(self, user_id)
                elif path == "/api/chat":
                    handlers.handle_chat(self, user_id)
                elif path == "/api/chat/stream":
                    handlers.handle_chat_stream(self, user_id)
                elif path == "/api/chat/clear":
                    handlers.handle_chat_clear(self, user_id)
                elif path == "/api/config":
                    handlers.handle_set_config(self, user_id)
                elif path == "/api/import":
                    handlers.handle_import(self, user_id)
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
                if parsed.path.startswith("/api/tasks/") and "/done" not in parsed.path and "/postpone" not in parsed.path and "/drop" not in parsed.path:
                    handlers.handle_edit_task(self, parsed.path, user_id)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            finally:
                log_api_request("PUT", parsed.path, self._last_status or 200, (time.time() - t0) * 1000)


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
