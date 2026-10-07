from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer

from momentum_agent.auth import hash_password
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import MomentumHandler, _store_cache


_TRIGGER_NAME = "test_fail_focus_session_insert_once"


def _application_snapshot(database_path):
    """Return every application table's rows, excluding SQLite internals."""
    with closing(sqlite3.connect(database_path)) as connection:
        table_names = [
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        snapshot = {}
        for table_name in table_names:
            quoted_name = '"' + table_name.replace('"', '""') + '"'
            rows = connection.execute(
                f"SELECT * FROM {quoted_name} ORDER BY rowid"
            ).fetchall()
            snapshot[table_name] = tuple(tuple(row) for row in rows)
    return snapshot


def _post_focus_finish(base_url, token, payload):
    request = urllib.request.Request(
        f"{base_url}/api/focus/finish",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Connection": "close",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as response:
        with response:
            return response.code, response.read()


def _set_focus_event_trigger(database_path, task_id):
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(
                f"""
                CREATE TRIGGER {_TRIGGER_NAME}
                BEFORE INSERT ON task_events
                WHEN NEW.event_type = 'focus_session' AND NEW.task_id = {int(task_id)}
                BEGIN
                    SELECT RAISE(ABORT, 'injected focus event insert failure');
                END
                """
            )


def _drop_focus_event_trigger(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}")


def test_focus_finish_http_rolls_back_and_same_session_retry_succeeds(tmp_path):
    database_path = tmp_path / "focus-finish-http-recovery.sqlite3"
    database_url = f"sqlite:///{database_path}"
    schema_cache_key = str(database_path.resolve())
    _store_cache.pop(database_url, None)

    server = None
    server_thread = None
    trigger_installed = False
    try:
        store = SQLiteTaskStore(database_path)
        user_id = f"focus-recovery-{uuid.uuid4().hex}"
        password = "focus-finish-http-recovery-test-password"
        store.register_user(user_id, user_id, hash_password(password))
        token = store.login_user(user_id, password)
        assert token
        task = store.create_task("HTTP 故障恢复隔离任务", user_id=user_id)

        started_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=73)
        ended_at = started_at + timedelta(seconds=73)
        payload = {
            "task_id": task.id,
            "session_id": uuid.uuid4().hex,
            "started_at": started_at.isoformat().replace("+00:00", "Z"),
            "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
            "planned_minutes": 25,
            "actual_seconds": 73,
            "outcome": "stopped",
        }

        class ReadyThreadingHTTPServer(ThreadingHTTPServer):
            def __init__(self, *args, **kwargs):
                self.ready = threading.Event()
                super().__init__(*args, **kwargs)

            def service_actions(self):
                self.ready.set()

        handler_type = type(
            "FocusFinishRecoveryHandler",
            (MomentumHandler,),
            {"database_url": database_url},
        )
        # Force the real handler property to construct/cache its isolated store.
        _store_cache.pop(database_url, None)
        server = ReadyThreadingHTTPServer(("127.0.0.1", 0), handler_type)
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="focus-finish-recovery-http-server",
            daemon=True,
        )
        server_thread.start()
        assert server.ready.wait(timeout=5), "HTTP server did not enter serve_forever"
        base_url = f"http://127.0.0.1:{server.server_port}"

        _set_focus_event_trigger(database_path, task.id)
        trigger_installed = True
        before_failure = _application_snapshot(database_path)
        expected_tables = {
            "users",
            "sessions",
            "tasks",
            "task_events",
            "task_relations",
            "user_memory",
            "task_postpone_idempotency",
            "task_done_idempotency",
            "task_done_occurrences",
        }
        assert expected_tables.issubset(before_failure)

        failure_status, _failure_body = _post_focus_finish(base_url, token, payload)
        assert failure_status == HTTPStatus.INTERNAL_SERVER_ERROR
        assert _application_snapshot(database_path) == before_failure
        assert database_url in _store_cache

        _drop_focus_event_trigger(database_path)
        trigger_installed = False

        success_status, success_body = _post_focus_finish(base_url, token, payload)
        assert success_status == HTTPStatus.OK
        success_payload = json.loads(success_body.decode("utf-8"))
        assert success_payload["task_id"] == task.id
        assert success_payload["session_id"] == payload["session_id"]
        assert success_payload["actual_seconds"] == 73
        assert success_payload["outcome"] == "stopped"

        after_success = _application_snapshot(database_path)
        assert set(after_success) == set(before_failure)
        for table_name in set(before_failure) - {"task_events"}:
            assert after_success[table_name] == before_failure[table_name]
        assert len(after_success["task_events"]) == len(before_failure["task_events"]) + 1

        with closing(sqlite3.connect(database_path)) as connection:
            event_rows = connection.execute(
                """
                SELECT payload FROM task_events
                WHERE task_id = ? AND event_type = 'focus_session'
                """,
                (task.id,),
            ).fetchall()
        event_payloads = [json.loads(row[0]) for row in event_rows]
        matching_events = [
            event for event in event_payloads
            if event.get("session_id") == payload["session_id"]
        ]
        assert len(matching_events) == 1
        assert matching_events[0]["actual_seconds"] == 73
        assert sum(event["actual_seconds"] for event in matching_events) == 73

        stored_sessions = [
            session
            for session in store.get_focus_sessions(user_id=user_id)
            if session["session_id"] == payload["session_id"]
        ]
        assert len(stored_sessions) == 1
        assert stored_sessions[0]["actual_seconds"] == 73

        replay_status, replay_body = _post_focus_finish(base_url, token, payload)
        assert replay_status == HTTPStatus.OK
        assert replay_body == success_body
        assert json.loads(replay_body.decode("utf-8")) == success_payload
        assert _application_snapshot(database_path) == after_success
    finally:
        try:
            if trigger_installed:
                _drop_focus_event_trigger(database_path)
        finally:
            try:
                if server is not None:
                    if server_thread is not None and server_thread.is_alive():
                        server.shutdown()
                        server_thread.join(timeout=5)
                    server.server_close()
                    assert server_thread is None or not server_thread.is_alive(), (
                        "HTTP server thread did not stop cleanly"
                    )
            finally:
                _store_cache.pop(database_url, None)
                SQLiteTaskStore._schema_initialized.discard(schema_cache_key)
