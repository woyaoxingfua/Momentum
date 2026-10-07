from __future__ import annotations

import io
import json
from http import HTTPStatus

import pytest

from momentum_agent.auth import hash_password
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.storage.backup import (
    EXCLUDED_CREDENTIAL_MEMORY_REASON,
    UNATTRIBUTED_EVENTS_REASON,
)
from momentum_agent.web.handlers import handle_export, handle_import
from momentum_agent.web.server import MAX_BACKUP_SIZE_BYTES, MAX_REQUEST_BODY


class BackupHandler:
    def __init__(self, store: SQLiteTaskStore, payload: dict | None = None):
        self.store = store
        self._payload = payload or {}
        self._status = HTTPStatus.OK
        self._body = b""
        self._headers: dict[str, str] = {}
        self.wfile = io.BytesIO()
        self.read_limit = None

    def read_json(self, *, max_body_size=MAX_REQUEST_BODY):
        self.read_limit = max_body_size
        return self._payload

    def send_json(self, payload, status=HTTPStatus.OK):
        self._status = status
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers["Content-Type"] = "application/json; charset=utf-8"
        self._headers["Content-Length"] = str(len(self._body))
        self.wfile.write(self._body)

    def send_response(self, status):
        self._status = status

    def send_header(self, key, value):
        self._headers[key] = value

    def end_headers(self):
        pass


def _user(store: SQLiteTaskStore, user_id: str) -> None:
    store.register_user(user_id, user_id, hash_password("backup-handler-password"))


def _package(store: SQLiteTaskStore) -> dict:
    _user(store, "source")
    store.create_task("source-owned", user_id="source")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, payload, created_at) VALUES (NULL, ?, ?, ?)",
            ("legacy", "unattributed", "2026-01-01T00:00:00+00:00"),
        )
    return store.export_user_data(user_id="source")


def test_v2_import_handler_uses_authenticated_user_and_reports_exclusions(tmp_path):
    store = SQLiteTaskStore(tmp_path / "api.sqlite3")
    package = _package(store)
    _user(store, "recipient")
    _user(store, "victim")
    victim_task = store.create_task("victim-private", user_id="victim")
    package["user_id"] = "victim"  # untrusted source metadata must not select the destination
    with store._connect() as conn:
        victim_before = [tuple(row) for row in conn.execute("SELECT * FROM tasks WHERE user_id = ?", ("victim",))]
    handler = BackupHandler(store, {"data": package})

    handle_import(handler, "recipient")

    body = json.loads(handler._body)
    assert handler.read_limit == MAX_BACKUP_SIZE_BYTES
    assert handler._status == HTTPStatus.OK
    assert body["message"] == "已恢复 1 个任务。"
    assert body["excluded_events"] == {"count": None, "reason": UNATTRIBUTED_EVENTS_REASON}
    assert body["excluded_memory"]["count"] == 0
    assert [task.title for task in store.list_tasks(status=None, user_id="recipient")] == ["source-owned"]
    assert store._get_task(victim_task.id).title == "victim-private"
    with store._connect() as conn:
        victim_after = [tuple(row) for row in conn.execute("SELECT * FROM tasks WHERE user_id = ?", ("victim",))]
    assert victim_after == victim_before


def test_v2_nonempty_restore_returns_conflict_and_error_body(tmp_path):
    store = SQLiteTaskStore(tmp_path / "conflict.sqlite3")
    package = _package(store)
    _user(store, "recipient")
    store.create_task("already-there", user_id="recipient")
    handler = BackupHandler(store, {"data": package})

    handle_import(handler, "recipient")

    body = json.loads(handler._body)
    assert handler._status == HTTPStatus.CONFLICT
    assert "空数据域" in body["error"]
    assert len(store.list_tasks(status=None, user_id="recipient")) == 1


@pytest.mark.parametrize(
    "package",
    [
        {"version": "2.0", "tasks": [{"id": 0}]},
        {"version": "99.0", "tasks": []},
    ],
)
def test_invalid_or_unknown_import_returns_bad_request_error_body(tmp_path, package):
    store = SQLiteTaskStore(tmp_path / f"invalid-{package['version']}.sqlite3")
    _user(store, "recipient")
    with store._connect() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM tasks")]
    handler = BackupHandler(store, {"data": package})

    handle_import(handler, "recipient")

    body = json.loads(handler._body)
    assert handler._status == HTTPStatus.BAD_REQUEST
    assert body["error"].startswith("导入失败：")
    with store._connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM tasks")] == before


def test_v1_import_keeps_existing_request_and_response_shape(tmp_path):
    store = SQLiteTaskStore(tmp_path / "v1-api.sqlite3")
    _user(store, "recipient")
    store.create_task("existing", user_id="recipient")
    legacy = {"version": "1.0", "tasks": [{"title": "legacy import"}], "memory": {}}
    handler = BackupHandler(store, {"data": legacy})

    handle_import(handler, "recipient")

    assert handler._status == HTTPStatus.OK
    assert json.loads(handler._body) == {
        "message": "已导入 1 个任务。",
        "excluded_memory": {"count": 0, "reason": EXCLUDED_CREDENTIAL_MEMORY_REASON},
    }
    assert {task.title for task in store.list_tasks(status=None, user_id="recipient")} == {"existing", "legacy import"}


def test_v1_import_filters_credentials_and_returns_only_safe_summary(tmp_path):
    store = SQLiteTaskStore(tmp_path / "v1-credentials.sqlite3")
    _user(store, "recipient")
    sensitive_fields = {
        "API-Key": "value-api-key-marker",
        "database.password": "value-password-marker",
        "refreshToken": "value-token-marker",
        "client_secret": "value-secret-marker",
        "private_key": "value-private-key-marker",
    }
    package = {
        "version": "1.0",
        "tasks": [],
        "memory": {**sensitive_fields, "theme": "sage"},
    }
    handler = BackupHandler(store, {"data": package})

    handle_import(handler, "recipient")

    body_text = handler._body.decode("utf-8")
    body = json.loads(body_text)
    assert handler._status == HTTPStatus.OK
    assert body == {
        "message": "已导入 0 个任务。",
        "excluded_memory": {"count": len(sensitive_fields), "reason": EXCLUDED_CREDENTIAL_MEMORY_REASON},
    }
    assert store.get_memory("theme", user_id="recipient") == "sage"
    for key, value in sensitive_fields.items():
        assert store.get_memory(key, user_id="recipient") is None
        assert key not in body_text
        assert value not in body_text


def test_v1_mid_write_failure_rolls_back_and_does_not_log_or_return_memory_fields(tmp_path, monkeypatch, caplog):
    import momentum_agent.storage.backup as backup_module

    store = SQLiteTaskStore(tmp_path / "v1-redacted-failure.sqlite3")
    _user(store, "recipient")
    sensitive_fields = {
        "API-Key": "value-api-key-marker",
        "database.password": "value-password-marker",
        "refreshToken": "value-token-marker",
    }
    package = {
        "version": "1.0",
        "tasks": [{"title": "must roll back"}],
        "memory": {**sensitive_fields, "ordinary_pref": "safe"},
    }
    with store._connect() as conn:
        before = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("tasks", "task_events", "user_memory")
        }
    original = backup_module._write_memory
    written_keys = []

    def write_then_fail(store_arg, conn, key, value, updated_at, user_id, dialect):
        written_keys.append(key)
        original(store_arg, conn, key, value, updated_at, user_id, dialect)
        raise RuntimeError("injected memory write failure")

    monkeypatch.setattr(backup_module, "_write_memory", write_then_fail)
    handler = BackupHandler(store, {"data": package})

    handle_import(handler, "recipient")

    response_text = handler._body.decode("utf-8")
    assert handler._status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert json.loads(response_text) == {"error": "导入失败：备份未写入，请稍后重试。"}
    assert written_keys == ["ordinary_pref"]
    for key, value in sensitive_fields.items():
        assert key not in response_text and value not in response_text
        assert key not in caplog.text and value not in caplog.text
    with store._connect() as conn:
        after = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("tasks", "task_events", "user_memory")
        }
    assert after == before


def test_export_handler_returns_v2_package_with_unknown_unattributed_count(tmp_path):
    store = SQLiteTaskStore(tmp_path / "export-api.sqlite3")
    package = _package(store)
    handler = BackupHandler(store)

    handle_export(handler, "source")

    body = json.loads(handler.wfile.getvalue())
    assert handler._status == HTTPStatus.OK
    assert handler._headers["Content-Type"] == "application/json; charset=utf-8"
    assert body["version"] == "2.0"
    assert body["user_id"] == package["user_id"] == "source"
    assert body["tasks"] == package["tasks"]
    assert body["excluded_events"]["count"] is None
    assert "不代表为零" in body["excluded_events"]["reason"]


def test_oversized_export_returns_json_error_without_attachment_or_backup_fragment(tmp_path):
    store = SQLiteTaskStore(tmp_path / "oversized-export.sqlite3")
    _user(store, "source")
    store.create_task(
        "oversized",
        notes="x" * (MAX_BACKUP_SIZE_BYTES + 1),
        user_id="source",
    )
    handler = BackupHandler(store)

    handle_export(handler, "source")

    assert handler._status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert handler.close_connection is True
    assert "Content-Disposition" not in handler._headers
    assert handler.wfile.getvalue() == handler._body
    assert b"x" * 1024 not in handler.wfile.getvalue()
    payload = json.loads(handler.wfile.getvalue())
    assert "16 MiB" in payload["error"]


def test_http_import_destination_is_selected_from_bearer_session(tmp_path):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from momentum_agent.web.server import MomentumHandler, _store_cache

    database_path = tmp_path / "authenticated-route.sqlite3"
    store = SQLiteTaskStore(database_path)
    package = _package(store)
    _user(store, "recipient")
    _user(store, "victim")
    victim_task = store.create_task("victim-private", user_id="victim")
    package["user_id"] = "victim"
    token = store.login_user("recipient", "backup-handler-password")
    assert token

    configured_handler = type(
        "TestConfiguredMomentumHandler",
        (MomentumHandler,),
        {"database_url": str(database_path)},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), configured_handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"data": package}).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/import",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == HTTPStatus.OK
        assert payload["excluded_events"]["count"] is None
        assert [task.title for task in store.list_tasks(status=None, user_id="recipient")] == ["source-owned"]
        assert [task.title for task in store.list_tasks(status=None, user_id="victim")] == ["victim-private"]
        assert store._get_task(victim_task.id).title == "victim-private"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        _store_cache.pop(str(database_path), None)


def test_http_round_trips_complete_v2_backup_larger_than_2_mib(tmp_path):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from momentum_agent.web.server import MomentumHandler, _store_cache

    database_path = tmp_path / "large-round-trip.sqlite3"
    database_url = str(database_path)
    store = SQLiteTaskStore(database_path)
    _user(store, "source")
    _user(store, "recipient")
    notes = "history-" + "x" * (MAX_REQUEST_BODY + 4096)
    store.create_task("large history", notes=notes, user_id="source")
    source_token = store.login_user("source", "backup-handler-password")
    recipient_token = store.login_user("recipient", "backup-handler-password")
    assert source_token and recipient_token

    configured_handler = type(
        "LargeBackupRoundTripHandler",
        (MomentumHandler,),
        {"database_url": database_url},
    )
    _store_cache.pop(database_url, None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), configured_handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        export_request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/export",
            headers={"Authorization": f"Bearer {source_token}"},
        )
        with urllib.request.urlopen(export_request, timeout=15) as response:
            exported_body = response.read()
            assert response.status == HTTPStatus.OK
            assert "attachment" in response.headers.get("Content-Disposition", "")
        assert MAX_REQUEST_BODY < len(exported_body) < MAX_BACKUP_SIZE_BYTES
        package = json.loads(exported_body)
        assert package["version"] == "2.0"
        assert package["tasks"][0]["notes"] == notes

        import_body = json.dumps({"data": package}, ensure_ascii=False).encode("utf-8")
        assert MAX_REQUEST_BODY < len(import_body) < MAX_BACKUP_SIZE_BYTES
        import_request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/import",
            data=import_body,
            headers={
                "Authorization": f"Bearer {recipient_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(import_request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
            assert response.status == HTTPStatus.OK
        assert result["message"] == "已恢复 1 个任务。"
        restored = store.list_tasks(status=None, user_id="recipient")
        assert len(restored) == 1
        assert restored[0].title == "large history"
        assert restored[0].notes == notes
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)


def test_http_v1_import_merges_into_bearer_user_and_preserves_other_user(tmp_path):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from momentum_agent.web.server import MomentumHandler, _store_cache

    database_path = tmp_path / "v1-authenticated-route.sqlite3"
    database_url = str(database_path)
    store = SQLiteTaskStore(database_path)
    _user(store, "recipient")
    _user(store, "victim")
    existing_recipient_task = store.create_task("existing recipient task", user_id="recipient")
    victim_task = store.create_task("private victim task", user_id="victim")
    store.set_memory("recipient_pref", "keep", user_id="recipient")
    store.set_memory("victim_pref", "private", user_id="victim")
    existing_recipient_before = store.get_task_for_user(existing_recipient_task.id, "recipient")
    recipient_token = store.login_user("recipient", "backup-handler-password")
    assert recipient_token

    def account_snapshot(user_id):
        with store._connect() as conn:
            table_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            task_ids = [
                row[0]
                for row in conn.execute("SELECT id FROM tasks WHERE user_id = ?", (user_id,))
            ]
            snapshot = {}
            for table in table_names:
                columns = {
                    row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
                if table == "users":
                    rows = conn.execute(
                        f'SELECT * FROM "{table}" WHERE id = ? ORDER BY rowid', (user_id,)
                    ).fetchall()
                elif "user_id" in columns:
                    rows = conn.execute(
                        f'SELECT * FROM "{table}" WHERE user_id = ? ORDER BY rowid', (user_id,)
                    ).fetchall()
                elif table == "task_events" and task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    rows = conn.execute(
                        f'SELECT * FROM "{table}" WHERE task_id IN ({placeholders}) ORDER BY rowid',
                        task_ids,
                    ).fetchall()
                else:
                    continue
                snapshot[table] = tuple(tuple(row) for row in rows)
            return snapshot

    recipient_before = account_snapshot("recipient")
    victim_before = account_snapshot("victim")
    package = {
        "version": "1.0",
        "user_id": "victim",
        "tasks": [{"title": "legacy imported task"}],
        "memory": {},
    }

    configured_handler = type(
        "V1AuthenticatedImportHandler",
        (MomentumHandler,),
        {"database_url": database_url},
    )
    _store_cache.pop(database_url, None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), configured_handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"data": package}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/import",
            data=body,
            headers={
                "Authorization": f"Bearer {recipient_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response_status = response.status
            response_payload = json.loads(response.read().decode("utf-8"))

        assert response_status == HTTPStatus.OK
        assert response_payload == {
            "message": "已导入 1 个任务。",
            "excluded_memory": {"count": 0, "reason": EXCLUDED_CREDENTIAL_MEMORY_REASON},
        }
        recipient_tasks = store.list_tasks(status=None, user_id="recipient")
        assert {task.title for task in recipient_tasks} == {
            "existing recipient task",
            "legacy imported task",
        }
        assert store.get_task_for_user(existing_recipient_task.id, "recipient") == existing_recipient_before
        imported = next(task for task in recipient_tasks if task.title == "legacy imported task")
        assert store.get_task_for_user(imported.id, "recipient") == imported
        assert store.get_task_for_user(imported.id, "victim") is None
        assert store.get_memory("recipient_pref", user_id="recipient") == "keep"
        assert store.get_memory("victim_pref", user_id="victim") == "private"
        assert account_snapshot("victim") == victim_before
        assert store.get_task_for_user(victim_task.id, "victim").title == "private victim task"
        recipient_after = account_snapshot("recipient")
        assert recipient_after["tasks"] != recipient_before["tasks"]
        assert len(recipient_after["tasks"]) == len(recipient_before["tasks"]) + 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)
