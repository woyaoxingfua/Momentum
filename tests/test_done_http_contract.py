from __future__ import annotations

import json
import threading
import uuid
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from momentum_agent.models import TaskStatus
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import MomentumHandler, _store_cache


DUE_AT = datetime(2026, 10, 4, 9, 15, tzinfo=timezone.utc)


@pytest.fixture
def done_http(tmp_path, monkeypatch):
    """Run the real dispatcher and handlers against an isolated SQLite file."""
    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    database_path = tmp_path / "done-http-contract.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    store = SQLiteTaskStore(database_path)

    user_id = f"done-http-{uuid.uuid4().hex}"
    password = f"test-password-{uuid.uuid4().hex}"
    handler_type = type(
        "DoneHttpContractHandler",
        (MomentumHandler,),
        {"database_url": database_url},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    assert isinstance(server, ThreadingHTTPServer)
    assert server.server_address[0] == "127.0.0.1"
    assert server.server_port > 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        register_status, register_raw = _request(
            base_url,
            "POST",
            "/api/register",
            payload={"user_id": user_id, "display_name": user_id, "password": password},
        )
        assert register_status == HTTPStatus.OK
        assert json.loads(register_raw) == {"message": "注册成功，请登录"}

        login_status, login_raw = _request(
            base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        assert login_status == HTTPStatus.OK
        login = json.loads(login_raw)
        assert login["user_id"] == user_id
        assert login["token"]

        yield store, base_url, user_id, login["token"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)
        assert not thread.is_alive(), "ThreadingHTTPServer did not stop cleanly"


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    timeout: float = 5,
) -> tuple[int, bytes]:
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else (b"" if method == "POST" else None)
    )
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key

    request = urllib.request.Request(
        base_url + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as response:
        return response.code, response.read()


def _post_done(
    base_url: str,
    token: str,
    task_id: int,
    key: str | None,
    *,
    timeout: float = 5,
) -> tuple[int, bytes]:
    return _request(
        base_url,
        "POST",
        f"/api/tasks/{task_id}/done",
        token=token,
        idempotency_key=key,
        timeout=timeout,
    )


def _new_recurring_task(store: SQLiteTaskStore, user_id: str, title: str):
    return store.create_task(
        title,
        due_at=DUE_AT,
        recurrence="daily",
        user_id=user_id,
    )


def _user_write_snapshot(store: SQLiteTaskStore, user_id: str) -> tuple[tuple[tuple, ...], ...]:
    """Capture all task, event, occurrence, and done-ledger rows for this user."""
    with store._connect() as conn:
        tasks = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? ORDER BY id", (user_id,)
            ).fetchall()
        )
        events = tuple(
            tuple(row)
            for row in conn.execute(
                """
                SELECT e.* FROM task_events AS e
                JOIN tasks AS t ON t.id = e.task_id
                WHERE t.user_id = ? ORDER BY e.id
                """,
                (user_id,),
            ).fetchall()
        )
        occurrences = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_done_occurrences WHERE user_id = ? ORDER BY source_task_id, source_done_event_id",
                (user_id,),
            ).fetchall()
        )
        ledger = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_done_idempotency WHERE user_id = ? ORDER BY idempotency_key",
                (user_id,),
            ).fetchall()
        )
    return tasks, events, occurrences, ledger


def _exact_json(payload: dict[str, str]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize(
    ("key", "expected_error"),
    [
        (None, "idempotency_key_required"),
        ("not-a-uuid", "idempotency_key_invalid"),
        ("00000000-0000-1000-8000-000000000001", "idempotency_key_invalid"),
    ],
    ids=["missing-key", "malformed-key", "non-v4-key"],
)
def test_done_rejects_missing_or_invalid_key_over_http_without_any_writes(
    done_http, key, expected_error
):
    store, base_url, user_id, token = done_http
    task = _new_recurring_task(store, user_id, "invalid-key daily")
    before = _user_write_snapshot(store, user_id)

    status, raw_body = _post_done(base_url, token, task.id, key)

    assert status == HTTPStatus.BAD_REQUEST
    assert raw_body == _exact_json({"error": expected_error})
    assert store.get_task_for_user(task.id, user_id).status == TaskStatus.TODO
    assert _user_write_snapshot(store, user_id) == before


def test_done_http_replay_returns_exact_status_and_raw_body_and_creates_one_occurrence(
    done_http,
):
    store, base_url, user_id, token = done_http
    title = "HTTP daily review"
    task = _new_recurring_task(store, user_id, title)
    key = str(uuid.uuid4())

    first_status, first_body = _post_done(base_url, token, task.id, key)
    replay_status, replay_body = _post_done(base_url, token, task.id, key)

    assert first_status == HTTPStatus.OK
    assert replay_status == first_status
    assert replay_body == first_body
    with store._connect() as conn:
        user_tasks = conn.execute(
            "SELECT id, status, recurrence FROM tasks WHERE user_id = ? AND title = ? ORDER BY id",
            (user_id, title),
        ).fetchall()
        done_events = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? AND event_type = ? ORDER BY id",
            (task.id, "status_changed"),
        ).fetchall()
        occurrences = conn.execute(
            "SELECT * FROM task_done_occurrences WHERE user_id = ? AND source_task_id = ?",
            (user_id, task.id),
        ).fetchall()
        ledger = conn.execute(
            "SELECT * FROM task_done_idempotency WHERE user_id = ? AND idempotency_key = ?",
            (user_id, key),
        ).fetchall()

    assert store.get_task_for_user(task.id, user_id).status == TaskStatus.DONE
    assert len(user_tasks) == 2
    next_task = next(row for row in user_tasks if row["id"] != task.id)
    assert next_task["status"] == TaskStatus.TODO.value
    assert next_task["recurrence"] == "daily"
    assert len(done_events) == 1
    assert done_events[0]["payload"] == TaskStatus.DONE.value
    assert len(occurrences) == 1
    assert len(ledger) == 1

    actual_response = {"message": f"已创建下一期任务 #{next_task['id']}：{title}"}
    assert first_body == _exact_json(actual_response)
    assert occurrences[0]["source_done_event_id"] == done_events[0]["id"]
    assert occurrences[0]["next_task_id"] == next_task["id"]
    assert occurrences[0]["response_status"] == HTTPStatus.OK
    assert json.loads(occurrences[0]["response_json"]) == actual_response
    assert ledger[0]["response_status"] == HTTPStatus.OK
    assert json.loads(ledger[0]["response_json"]) == actual_response


def _database_table_snapshot(store: SQLiteTaskStore):
    """Return complete row counts and row snapshots for every application table."""
    with store._connect() as conn:
        table_names = tuple(
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        snapshots = {}
        for table_name in table_names:
            quoted_name = table_name.replace('"', '""')
            rows = conn.execute(f'SELECT * FROM "{quoted_name}" ORDER BY rowid').fetchall()
            snapshots[table_name] = tuple(tuple(row) for row in rows)
    counts = {table_name: len(rows) for table_name, rows in snapshots.items()}
    return counts, snapshots


def _assert_handler_database(handler_type, database_url: str, database_path: Path):
    assert handler_type.database_url == database_url
    store = _store_cache[database_url]
    assert isinstance(store, SQLiteTaskStore)
    assert store.db_path.resolve() == database_path.resolve()
    with store._connect() as conn:
        database_list = conn.execute("PRAGMA database_list").fetchall()
    main_database = next(row for row in database_list if row["name"] == "main")
    assert Path(main_database["file"]).resolve() == database_path.resolve()
    return store


def _stop_done_http_server(server, thread, database_url: str, *, thread_started: bool) -> None:
    try:
        if thread_started and thread.is_alive():
            server.shutdown()
    finally:
        try:
            server.server_close()
        finally:
            try:
                if thread_started:
                    thread.join(timeout=5)
            finally:
                _store_cache.pop(database_url, None)
    assert not thread.is_alive(), "ThreadingHTTPServer serve_forever thread did not stop"
    assert server.socket.fileno() == -1, "ThreadingHTTPServer listening socket is still open"
    assert server.socket._closed, "ThreadingHTTPServer listening socket was not closed"
    assert database_url not in _store_cache


def test_done_http_replays_persistent_idempotency_after_server_restart(tmp_path, monkeypatch):
    """Replay a real recurring-task completion over HTTP after reopening the same SQLite file."""
    from momentum_agent import config as config_module

    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    monkeypatch.delenv("MOMENTUM_DATABASE_URL", raising=False)
    for name in (
        "MOMENTUM_API_KEY",
        "OPENAI_API_KEY",
        "MOMENTUM_BASE_URL",
        "OPENAI_BASE_URL",
        "MOMENTUM_MODEL",
        "OPENAI_MODEL",
        "MOMENTUM_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: False)

    database_path = tmp_path / "done-http-restart.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)

    user_id = f"done-http-restart-{uuid.uuid4().hex}"
    password = f"test-password-{uuid.uuid4().hex}"
    title = f"HTTP restart daily {uuid.uuid4().hex}"
    task_text = f"每天 {title}"
    idempotency_key = str(uuid.uuid4())
    assert uuid.UUID(idempotency_key).version == 4

    first_handler_type = type(
        "FirstRestartDoneHttpContractHandler",
        (MomentumHandler,),
        {"database_url": database_url},
    )
    first_server = ThreadingHTTPServer(("127.0.0.1", 0), first_handler_type)
    first_thread = threading.Thread(target=first_server.serve_forever, daemon=True)
    first_thread_started = False

    try:
        first_thread.start()
        first_thread_started = True
        first_base_url = f"http://127.0.0.1:{first_server.server_port}"
        assert first_server.server_address[0] == "127.0.0.1"
        assert first_server.server_port > 0
        assert first_handler_type.database_url == database_url

        register_status, register_body = _request(
            first_base_url,
            "POST",
            "/api/register",
            payload={"user_id": user_id, "display_name": user_id, "password": password},
        )
        assert register_status == HTTPStatus.OK
        assert json.loads(register_body) == {"message": "注册成功，请登录"}

        login_status, login_body = _request(
            first_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        assert login_status == HTTPStatus.OK
        first_token = json.loads(login_body)["token"]
        assert first_token

        me_status, me_body = _request(first_base_url, "GET", "/api/me", token=first_token)
        assert me_status == HTTPStatus.OK
        assert json.loads(me_body) == {"user_id": user_id}

        create_status, create_body = _request(
            first_base_url,
            "POST",
            "/api/tasks",
            token=first_token,
            payload={"text": task_text},
        )
        assert create_status == HTTPStatus.OK
        created = json.loads(create_body)
        source_payload = next(item for item in created["tasks"] if item["title"] == title)
        task_id = source_payload["id"]
        assert source_payload["recurrence"] == "daily"
        assert source_payload["status"] == TaskStatus.TODO.value

        first_store = _assert_handler_database(
            first_handler_type, database_url, database_path
        )
        first_status, first_body = _post_done(
            first_base_url, first_token, task_id, idempotency_key
        )
        assert first_status == HTTPStatus.OK
        assert isinstance(first_body, bytes)
        first_response = json.loads(first_body)

        with first_store._connect() as conn:
            source_task = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            ).fetchone()
            done_events = conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? AND event_type = ? AND payload = ?",
                (task_id, "status_changed", TaskStatus.DONE.value),
            ).fetchall()
            occurrences = conn.execute(
                "SELECT * FROM task_done_occurrences "
                "WHERE user_id = ? AND source_task_id = ?",
                (user_id, task_id),
            ).fetchall()
            ledger_rows = conn.execute(
                "SELECT * FROM task_done_idempotency "
                "WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key),
            ).fetchall()

        assert source_task is not None
        assert source_task["status"] == TaskStatus.DONE.value
        assert source_task["recurrence"] == "daily"
        assert len(done_events) == 1
        assert len(occurrences) == 1
        next_task_id = occurrences[0]["next_task_id"]
        assert next_task_id is not None
        with first_store._connect() as conn:
            next_task = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (next_task_id, user_id),
            ).fetchone()
        assert next_task is not None
        assert next_task["status"] == TaskStatus.TODO.value
        assert next_task["recurrence"] == "daily"
        assert next_task["title"] == title
        assert len(ledger_rows) == 1
        assert ledger_rows[0]["response_status"] == first_status
        assert json.loads(ledger_rows[0]["response_json"]) == first_response
        assert occurrences[0]["source_done_event_id"] == done_events[0]["id"]
        assert occurrences[0]["response_status"] == first_status
        assert json.loads(occurrences[0]["response_json"]) == first_response

        first_counts, first_snapshot = _database_table_snapshot(first_store)
        assert {"users", "sessions", "tasks", "task_events", "task_done_occurrences", "task_done_idempotency"}.issubset(first_snapshot)
        assert first_counts["tasks"] == 2
        assert first_counts["task_events"] == 3
        assert first_counts["task_done_occurrences"] == 1
        assert first_counts["task_done_idempotency"] == 1
        assert first_counts["sessions"] == 1
        assert len(first_snapshot["tasks"]) == first_counts["tasks"]
        assert len(first_snapshot["task_events"]) == first_counts["task_events"]
        assert len(first_snapshot["task_done_occurrences"]) == first_counts["task_done_occurrences"]
        assert len(first_snapshot["task_done_idempotency"]) == first_counts["task_done_idempotency"]
    finally:
        _stop_done_http_server(
            first_server,
            first_thread,
            database_url,
            thread_started=first_thread_started,
        )

    second_handler_type = type(
        "SecondRestartDoneHttpContractHandler",
        (MomentumHandler,),
        {"database_url": database_url},
    )
    second_server = ThreadingHTTPServer(("127.0.0.1", 0), second_handler_type)
    second_thread = threading.Thread(target=second_server.serve_forever, daemon=True)
    second_thread_started = False

    try:
        second_thread.start()
        second_thread_started = True
        second_base_url = f"http://127.0.0.1:{second_server.server_port}"
        assert second_server is not first_server
        assert second_server.RequestHandlerClass is not first_server.RequestHandlerClass
        assert second_server.server_address[0] == "127.0.0.1"
        assert second_server.server_port > 0
        assert second_handler_type.database_url == database_url
        assert database_url not in _store_cache

        second_login_status, second_login_body = _request(
            second_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        assert second_login_status == HTTPStatus.OK
        second_token = json.loads(second_login_body)["token"]
        assert second_token
        assert second_token != first_token

        second_store = _assert_handler_database(
            second_handler_type, database_url, database_path
        )
        assert second_store is not first_store
        me_status, me_body = _request(second_base_url, "GET", "/api/me", token=second_token)
        assert me_status == HTTPStatus.OK
        assert json.loads(me_body) == {"user_id": user_id}

        after_login_counts, after_login_snapshot = _database_table_snapshot(second_store)
        non_session_tables = set(first_snapshot) - {"sessions"}
        assert {
            table_name: first_counts[table_name] for table_name in non_session_tables
        } == {
            table_name: after_login_counts[table_name] for table_name in non_session_tables
        }
        assert {
            table_name: first_snapshot[table_name] for table_name in non_session_tables
        } == {
            table_name: after_login_snapshot[table_name] for table_name in non_session_tables
        }
        assert after_login_counts["sessions"] == first_counts["sessions"] + 1
        assert set(first_snapshot["sessions"]).issubset(set(after_login_snapshot["sessions"]))
        with second_store._connect() as conn:
            user_session_tokens = {
                row["token"]
                for row in conn.execute(
                    "SELECT token FROM sessions WHERE user_id = ?", (user_id,)
                ).fetchall()
            }
        assert user_session_tokens == {first_token, second_token}

        before_replay_counts, before_replay_snapshot = _database_table_snapshot(second_store)
        replay_status, replay_body = _post_done(
            second_base_url, second_token, task_id, idempotency_key
        )
        assert replay_status == first_status
        assert replay_body == first_body

        after_replay_counts, after_replay_snapshot = _database_table_snapshot(second_store)
        assert after_replay_counts == before_replay_counts
        assert after_replay_snapshot == before_replay_snapshot
        assert {
            table_name: after_replay_counts[table_name] for table_name in non_session_tables
        } == {
            table_name: first_counts[table_name] for table_name in non_session_tables
        }
        assert {
            table_name: after_replay_snapshot[table_name] for table_name in non_session_tables
        } == {
            table_name: first_snapshot[table_name] for table_name in non_session_tables
        }
        assert after_replay_counts["sessions"] == first_counts["sessions"] + 1
    finally:
        _stop_done_http_server(
            second_server,
            second_thread,
            database_url,
            thread_started=second_thread_started,
        )


def test_done_http_same_key_for_different_task_conflicts_without_mutating_second_task(
    done_http,
):
    store, base_url, user_id, token = done_http
    first_task = _new_recurring_task(store, user_id, "first HTTP daily")
    second_task = _new_recurring_task(store, user_id, "second HTTP daily")
    key = str(uuid.uuid4())

    first_status, first_body = _post_done(base_url, token, first_task.id, key)
    assert first_status == HTTPStatus.OK
    assert json.loads(first_body)["message"].startswith("已创建下一期任务 #")
    before_conflict = _user_write_snapshot(store, user_id)
    second_before = store.get_task_for_user(second_task.id, user_id)

    conflict_status, conflict_body = _post_done(base_url, token, second_task.id, key)

    assert conflict_status == HTTPStatus.CONFLICT
    assert conflict_body == _exact_json({"error": "idempotency_conflict"})
    assert store.get_task_for_user(second_task.id, user_id) == second_before
    assert second_before.status == TaskStatus.TODO
    assert _user_write_snapshot(store, user_id) == before_conflict
    with store._connect() as conn:
        second_done_events = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND event_type = ? AND payload = ?",
            (second_task.id, "status_changed", TaskStatus.DONE.value),
        ).fetchall()
        total_mappings = conn.execute(
            "SELECT COUNT(*) AS n FROM task_done_occurrences WHERE user_id = ?",
            (user_id,),
        ).fetchone()["n"]
        total_ledger = conn.execute(
            "SELECT COUNT(*) AS n FROM task_done_idempotency WHERE user_id = ?",
            (user_id,),
        ).fetchone()["n"]
        total_tasks = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE user_id = ?",
            (user_id,),
        ).fetchone()["n"]

    assert second_done_events == []
    assert total_mappings == 1
    assert total_ledger == 1
    assert total_tasks == 3


def test_done_http_same_user_same_key_concurrent_different_tasks_conflicts_and_replays_winner(
    done_http, tmp_path
):
    store, base_url, user_id, token = done_http
    database_path = tmp_path / "done-http-contract.sqlite3"
    assert isinstance(store, SQLiteTaskStore)
    assert store.db_path.resolve() == database_path.resolve()
    assert base_url.startswith("http://127.0.0.1:")
    with store._connect() as conn:
        main_database = next(
            row for row in conn.execute("PRAGMA database_list").fetchall()
            if row["name"] == "main"
        )
    assert Path(main_database["file"]).resolve() == database_path.resolve()

    winner_title = f"concurrent winner daily {uuid.uuid4().hex}"
    loser_title = f"concurrent loser daily {uuid.uuid4().hex}"
    winner_task = _new_recurring_task(store, user_id, winner_title)
    loser_task = _new_recurring_task(store, user_id, loser_title)
    tasks_by_id = {winner_task.id: winner_task, loser_task.id: loser_task}
    titles_by_task = {winner_task.id: winner_title, loser_task.id: loser_title}
    assert winner_task.id != loser_task.id
    assert winner_task.status == loser_task.status == TaskStatus.TODO
    assert winner_task.recurrence == loser_task.recurrence == "daily"

    key = str(uuid.uuid4())
    assert uuid.UUID(key).version == 4
    before_race = _user_write_snapshot(store, user_id)
    barrier = threading.Barrier(2)

    def post_after_barrier(task_id: int):
        barrier.wait(timeout=10)
        return task_id, _post_done(base_url, token, task_id, key, timeout=15)

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        futures = [
            pool.submit(post_after_barrier, task_id)
            for task_id in (winner_task.id, loser_task.id)
        ]
        responses = [future.result(timeout=20) for future in futures]
    finally:
        barrier.abort()
        pool.shutdown(wait=True, cancel_futures=True)

    assert len(responses) == 2
    assert {task_id for task_id, _ in responses} == set(tasks_by_id)
    response_by_task = dict(responses)
    assert sorted(status for status, _ in response_by_task.values()) == [
        HTTPStatus.OK,
        HTTPStatus.CONFLICT,
    ]

    winner_task_id = next(
        task_id
        for task_id, (status, _) in response_by_task.items()
        if status == HTTPStatus.OK
    )
    loser_task_id = next(
        task_id
        for task_id, (status, _) in response_by_task.items()
        if status == HTTPStatus.CONFLICT
    )
    winner_status, winner_body = response_by_task[winner_task_id]
    loser_status, loser_body = response_by_task[loser_task_id]
    winner_task = tasks_by_id[winner_task_id]
    loser_task = tasks_by_id[loser_task_id]
    expected_conflict = _exact_json({"error": "idempotency_conflict"})
    assert winner_status == HTTPStatus.OK
    assert loser_status == HTTPStatus.CONFLICT
    assert loser_body == expected_conflict
    assert titles_by_task[winner_task_id].encode("utf-8") in winner_body
    assert titles_by_task[loser_task_id].encode("utf-8") not in winner_body
    assert winner_task.status == TaskStatus.TODO
    assert loser_task.status == TaskStatus.TODO

    winner_events, winner_occurrences, winner_tasks, winner_ledger = _source_done_state(
        store, user_id, winner_task_id, winner_task.title
    )
    assert store.get_task_for_user(winner_task_id, user_id).status == TaskStatus.DONE
    assert len(winner_events) == 1
    assert winner_events[0]["payload"] == TaskStatus.DONE.value
    assert len(winner_occurrences) == 1
    assert len(winner_tasks) == 2
    next_task = next(row for row in winner_tasks if row["id"] != winner_task_id)
    occurrence = winner_occurrences[0]
    assert occurrence["source_task_id"] == winner_task_id
    assert occurrence["source_done_event_id"] == winner_events[0]["id"]
    assert occurrence["next_task_id"] == next_task["id"]
    assert next_task["status"] == TaskStatus.TODO.value
    assert next_task["recurrence"] == "daily"
    assert occurrence["response_status"] == HTTPStatus.OK
    assert occurrence["response_json"].encode("utf-8") == winner_body
    assert len(winner_ledger) == 1
    assert winner_ledger[0]["idempotency_key"] == key
    assert winner_ledger[0]["response_status"] == HTTPStatus.OK
    assert winner_ledger[0]["response_json"].encode("utf-8") == winner_body

    loser_events, loser_occurrences, loser_tasks, loser_ledger = _source_done_state(
        store, user_id, loser_task_id, loser_task.title
    )
    assert store.get_task_for_user(loser_task_id, user_id).status == TaskStatus.TODO
    assert loser_events == []
    assert loser_occurrences == []
    assert len(loser_tasks) == 1
    assert loser_tasks[0]["id"] == loser_task_id
    assert loser_tasks[0]["status"] == TaskStatus.TODO.value
    assert loser_ledger == winner_ledger
    with store._connect() as conn:
        ledger_rows = conn.execute(
            "SELECT user_id, idempotency_key FROM task_done_idempotency"
        ).fetchall()
    assert [tuple(row) for row in ledger_rows] == [(user_id, key)]

    after_race = _user_write_snapshot(store, user_id)
    assert after_race != before_race
    replay_status, replay_body = _post_done(
        base_url, token, winner_task_id, key
    )
    assert (replay_status, replay_body) == (winner_status, winner_body)
    assert _user_write_snapshot(store, user_id) == after_race

    retry_status, retry_body = _post_done(base_url, token, loser_task_id, key)
    assert retry_status == HTTPStatus.CONFLICT
    assert retry_body == expected_conflict
    assert retry_body == loser_body
    assert _user_write_snapshot(store, user_id) == after_race



def _source_done_state(store: SQLiteTaskStore, user_id: str, task_id: int, title: str):
    with store._connect() as conn:
        events = conn.execute(
            """
            SELECT id, payload FROM task_events
            WHERE task_id = ? AND event_type = 'status_changed' AND payload = ?
            ORDER BY id
            """,
            (task_id, TaskStatus.DONE.value),
        ).fetchall()
        occurrences = conn.execute(
            """
            SELECT * FROM task_done_occurrences
            WHERE user_id = ? AND source_task_id = ?
            ORDER BY source_done_event_id
            """,
            (user_id, task_id),
        ).fetchall()
        recurring_tasks = conn.execute(
            """
            SELECT id, status, recurrence FROM tasks
            WHERE user_id = ? AND title = ? ORDER BY id
            """,
            (user_id, title),
        ).fetchall()
        ledger = conn.execute(
            """
            SELECT idempotency_key, response_status, response_json
            FROM task_done_idempotency WHERE user_id = ? ORDER BY idempotency_key
            """,
            (user_id,),
        ).fetchall()
    return events, occurrences, recurring_tasks, ledger


def test_concurrent_done_dispatch_is_atomic_across_reopen_and_exactly_replayable(
    tmp_path, monkeypatch
):
    """Synchronize two authenticated HTTP handlers immediately before real done calls."""
    from momentum_agent.web import handlers as web_handlers

    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    monkeypatch.delenv("MOMENTUM_DATABASE_URL", raising=False)
    database_path = tmp_path / "done-http-concurrency.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    store = SQLiteTaskStore(database_path)

    user_id = f"done-http-concurrent-{uuid.uuid4().hex}"
    password = f"test-password-{uuid.uuid4().hex}"
    gate_lock = threading.Lock()
    gate_state = {
        "barrier": None,
        "round": None,
        "participants": [],
        "releases": [],
    }
    original_done = web_handlers.handle_done_task

    def synchronized_done(handler, path, authenticated_user_id):
        with gate_lock:
            barrier = gate_state["barrier"]
            round_name = gate_state["round"]
            thread_id = threading.get_ident()
        if barrier is None:
            return original_done(handler, path, authenticated_user_id)
        with gate_lock:
            gate_state["participants"].append((round_name, thread_id))
        release_index = barrier.wait(timeout=10)
        with gate_lock:
            gate_state["releases"].append((round_name, thread_id, release_index))
        return original_done(handler, path, authenticated_user_id)

    post_routes = dict(MomentumHandler.POST_PREFIX_ROUTES)
    post_routes["done"] = synchronized_done
    handler_type = type(
        "ConcurrentDoneHttpContractHandler",
        (MomentumHandler,),
        {"database_url": database_url, "POST_PREFIX_ROUTES": post_routes},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        register_status, register_raw = _request(
            base_url,
            "POST",
            "/api/register",
            payload={"user_id": user_id, "display_name": user_id, "password": password},
        )
        assert register_status == HTTPStatus.OK
        assert json.loads(register_raw) == {"message": "注册成功，请登录"}

        login_status, login_raw = _request(
            base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        assert login_status == HTTPStatus.OK
        token = json.loads(login_raw)["token"]
        assert token

        title = "HTTP concurrent daily review"
        task = _new_recurring_task(store, user_id, title)
        replay_response_assertions = 0

        def assert_exact_replay(key, first_response):
            nonlocal replay_response_assertions
            replay_status, replay_body = _post_done(
                base_url, token, task.id, key
            )
            assert replay_status == first_response[0]
            replay_response_assertions += 1
            assert replay_body == first_response[1]
            replay_response_assertions += 1

        def run_concurrent_done(keys, round_name):
            barrier = threading.Barrier(2)
            with gate_lock:
                gate_state["barrier"] = barrier
                gate_state["round"] = round_name
                participant_start = len(gate_state["participants"])
                release_start = len(gate_state["releases"])
            pool = ThreadPoolExecutor(max_workers=2)
            try:
                futures = [
                    pool.submit(
                        _post_done,
                        base_url,
                        token,
                        task.id,
                        key,
                        timeout=15,
                    )
                    for key in keys
                ]
                results = [future.result(timeout=17) for future in futures]
            finally:
                barrier.abort()
                pool.shutdown(wait=True, cancel_futures=True)
                with gate_lock:
                    if gate_state["barrier"] is barrier:
                        gate_state["barrier"] = None
            with gate_lock:
                participants = gate_state["participants"][participant_start:]
                releases = gate_state["releases"][release_start:]
            assert len(participants) == 2
            assert len({thread_id for _, thread_id in participants}) == 2
            assert len(releases) == 2
            assert sorted(index for _, _, index in releases) == [0, 1]
            assert {name for name, _ in participants} == {round_name}
            assert {name for name, _, _ in releases} == {round_name}
            return results

        first_keys = [str(uuid.uuid4()), str(uuid.uuid4())]
        assert all(uuid.UUID(key).version == 4 for key in first_keys)
        first_responses = run_concurrent_done(first_keys, "before-reopen")
        assert all(status == HTTPStatus.OK for status, _ in first_responses)
        first_messages = [json.loads(body)["message"] for _, body in first_responses]
        assert sum(message.startswith("已创建下一期任务 #") for message in first_messages) == 1
        assert first_messages.count("任务已完成。") == 1

        first_events, first_occurrences, first_tasks, first_ledger = _source_done_state(
            store, user_id, task.id, title
        )
        assert store.get_task_for_user(task.id, user_id).status == TaskStatus.DONE
        assert len(first_events) == 1
        assert len(first_occurrences) == 1
        assert len(first_tasks) == 2
        first_next_id = first_occurrences[0]["next_task_id"]
        assert first_next_id is not None
        assert {row["id"] for row in first_tasks} == {task.id, first_next_id}
        assert first_occurrences[0]["source_done_event_id"] == first_events[0]["id"]
        first_ledger_by_key = {row["idempotency_key"]: row for row in first_ledger}
        assert set(first_keys).issubset(first_ledger_by_key)
        for key, response in zip(first_keys, first_responses):
            ledger_row = first_ledger_by_key[key]
            assert ledger_row["response_status"] == response[0]
            assert json.loads(ledger_row["response_json"]) == json.loads(response[1])

        before_first_replays = _user_write_snapshot(store, user_id)
        for key, response in zip(first_keys, first_responses):
            assert_exact_replay(key, response)
        assert _user_write_snapshot(store, user_id) == before_first_replays

        reopen_status, reopen_body = _request(
            base_url,
            "POST",
            f"/api/tasks/{task.id}/reopen",
            token=token,
        )
        assert reopen_status == HTTPStatus.OK
        assert isinstance(json.loads(reopen_body).get("message"), str)
        assert store.get_task_for_user(task.id, user_id).status == TaskStatus.TODO

        before_old_key_replay = _user_write_snapshot(store, user_id)
        assert_exact_replay(first_keys[0], first_responses[0])
        assert _user_write_snapshot(store, user_id) == before_old_key_replay
        assert store.get_task_for_user(task.id, user_id).status == TaskStatus.TODO

        second_keys = [str(uuid.uuid4()), str(uuid.uuid4())]
        assert all(uuid.UUID(key).version == 4 for key in second_keys)
        second_responses = run_concurrent_done(second_keys, "after-reopen")
        assert all(status == HTTPStatus.OK for status, _ in second_responses)
        second_messages = [json.loads(body)["message"] for _, body in second_responses]
        assert sum(message.startswith("已创建下一期任务 #") for message in second_messages) == 1
        assert second_messages.count("任务已完成。") == 1

        events, occurrences, recurring_tasks, ledger = _source_done_state(
            store, user_id, task.id, title
        )
        assert store.get_task_for_user(task.id, user_id).status == TaskStatus.DONE
        assert len(events) == 2
        assert len(occurrences) == 2
        assert len(recurring_tasks) == 3
        next_ids = {row["next_task_id"] for row in occurrences}
        assert None not in next_ids
        assert len(next_ids) == 2
        assert {row["id"] for row in recurring_tasks} == {task.id, *next_ids}
        assert all(row["recurrence"] == "daily" for row in recurring_tasks)
        assert sum(row["status"] == TaskStatus.TODO.value for row in recurring_tasks) == 2
        event_ids = {row["id"] for row in events}
        assert {row["source_done_event_id"] for row in occurrences} == event_ids
        assert all(row["response_status"] == HTTPStatus.OK for row in occurrences)

        all_keys = first_keys + second_keys
        ledger_by_key = {row["idempotency_key"]: row for row in ledger}
        assert set(ledger_by_key) == set(all_keys)
        for key, response in zip(all_keys, first_responses + second_responses):
            ledger_row = ledger_by_key[key]
            assert ledger_row["response_status"] == response[0]
            assert json.loads(ledger_row["response_json"]) == json.loads(response[1])

        before_second_replays = _user_write_snapshot(store, user_id)
        for key, response in zip(second_keys, second_responses):
            assert_exact_replay(key, response)
        assert _user_write_snapshot(store, user_id) == before_second_replays
        assert replay_response_assertions == 10

        with gate_lock:
            participants = list(gate_state["participants"])
            releases = list(gate_state["releases"])
        assert len(participants) == 4
        assert len(releases) == 4
        assert {name for name, _ in participants} == {"before-reopen", "after-reopen"}
        assert {name for name, _, _ in releases} == {"before-reopen", "after-reopen"}
    finally:
        with gate_lock:
            active_barrier = gate_state["barrier"]
        if active_barrier is not None:
            active_barrier.abort()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        _store_cache.pop(database_url, None)
        assert not server_thread.is_alive(), "ThreadingHTTPServer did not stop cleanly"



def test_done_http_idempotency_key_is_isolated_between_users(tmp_path, monkeypatch):
    """The same HTTP idempotency key must be independent for distinct users."""
    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    monkeypatch.delenv("MOMENTUM_DATABASE_URL", raising=False)

    database_path = tmp_path / "done-http-cross-user.sqlite3"
    database_url = str(database_path)
    assert not database_path.exists()
    _store_cache.pop(database_url, None)

    user_a = f"done-http-a-{uuid.uuid4().hex}"
    user_b = f"done-http-b-{uuid.uuid4().hex}"
    password_a = f"test-password-{uuid.uuid4().hex}"
    password_b = f"test-password-{uuid.uuid4().hex}"
    title_a = f"HTTP user A daily {uuid.uuid4().hex}"
    title_b = f"HTTP user B daily {uuid.uuid4().hex}"
    assert title_a != title_b

    handler_type = type(
        "CrossUserDoneHttpContractHandler",
        (MomentumHandler,),
        {"database_url": database_url},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread_started = False

    try:
        server_thread.start()
        thread_started = True
        base_url = f"http://127.0.0.1:{server.server_port}"
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_port > 0
        assert server.RequestHandlerClass is handler_type
        assert handler_type.database_url == database_url

        def register_and_login(user_id: str, password: str) -> str:
            register_status, register_body = _request(
                base_url,
                "POST",
                "/api/register",
                payload={
                    "user_id": user_id,
                    "display_name": user_id,
                    "password": password,
                },
            )
            assert register_status == HTTPStatus.OK
            assert json.loads(register_body) == {"message": "注册成功，请登录"}

            login_status, login_body = _request(
                base_url,
                "POST",
                "/api/login",
                payload={"user_id": user_id, "password": password},
            )
            assert login_status == HTTPStatus.OK
            login = json.loads(login_body)
            assert login["user_id"] == user_id
            assert login["token"]
            return login["token"]

        token_a = register_and_login(user_a, password_a)
        token_b = register_and_login(user_b, password_b)
        assert token_a != token_b

        for user_id, token in ((user_a, token_a), (user_b, token_b)):
            me_status, me_body = _request(base_url, "GET", "/api/me", token=token)
            assert me_status == HTTPStatus.OK
            assert json.loads(me_body) == {"user_id": user_id}

        store = _assert_handler_database(handler_type, database_url, database_path)
        assert _store_cache[database_url] is store

        def create_recurring_task(token: str, title: str) -> int:
            create_status, create_body = _request(
                base_url,
                "POST",
                "/api/tasks",
                token=token,
                payload={"text": f"每天 {title}"},
            )
            assert create_status == HTTPStatus.OK
            created = json.loads(create_body)
            task_payload = next(task for task in created["tasks"] if task["title"] == title)
            assert task_payload["recurrence"] == "daily"
            assert task_payload["status"] == TaskStatus.TODO.value
            return task_payload["id"]

        task_a_id = create_recurring_task(token_a, title_a)
        task_b_id = create_recurring_task(token_b, title_b)
        assert task_a_id != task_b_id
        assert _assert_handler_database(handler_type, database_url, database_path) is store

        with store._connect() as conn:
            task_owners = {
                row["id"]: row["user_id"]
                for row in conn.execute(
                    "SELECT id, user_id FROM tasks WHERE id IN (?, ?)",
                    (task_a_id, task_b_id),
                ).fetchall()
            }
        assert task_owners == {task_a_id: user_a, task_b_id: user_b}

        for token, task_id, title in (
            (token_a, task_a_id, title_a),
            (token_b, task_b_id, title_b),
        ):
            list_status, list_body = _request(
                base_url, "GET", "/api/tasks", token=token
            )
            assert list_status == HTTPStatus.OK
            listed_tasks = json.loads(list_body)["tasks"]
            assert {task["id"] for task in listed_tasks} == {task_id}
            assert {task["title"] for task in listed_tasks} == {title}

        before_a = _user_write_snapshot(store, user_a)
        before_b = _user_write_snapshot(store, user_b)
        shared_key = str(uuid.uuid4())
        assert uuid.UUID(shared_key).version == 4

        status_a, body_a = _post_done(base_url, token_a, task_a_id, shared_key)
        assert status_a == HTTPStatus.OK
        assert title_a.encode("utf-8") in body_a
        assert title_b.encode("utf-8") not in body_a
        after_a = _user_write_snapshot(store, user_a)
        assert after_a != before_a
        assert _user_write_snapshot(store, user_b) == before_b

        status_b, body_b = _post_done(base_url, token_b, task_b_id, shared_key)
        assert status_b == HTTPStatus.OK
        assert body_b != body_a
        assert title_b.encode("utf-8") in body_b
        assert title_a.encode("utf-8") not in body_b
        after_b = _user_write_snapshot(store, user_b)
        assert after_b != before_b
        assert _user_write_snapshot(store, user_a) == after_a

        before_a_replay = _user_write_snapshot(store, user_a)
        before_b_during_a_replay = _user_write_snapshot(store, user_b)
        all_tables_before_a_replay = _database_table_snapshot(store)
        replay_a_status, replay_a_body = _post_done(
            base_url, token_a, task_a_id, shared_key
        )
        assert (replay_a_status, replay_a_body) == (status_a, body_a)
        assert _user_write_snapshot(store, user_a) == before_a_replay
        assert _user_write_snapshot(store, user_b) == before_b_during_a_replay
        assert _database_table_snapshot(store) == all_tables_before_a_replay

        before_b_replay = _user_write_snapshot(store, user_b)
        before_a_during_b_replay = _user_write_snapshot(store, user_a)
        all_tables_before_b_replay = _database_table_snapshot(store)
        replay_b_status, replay_b_body = _post_done(
            base_url, token_b, task_b_id, shared_key
        )
        assert (replay_b_status, replay_b_body) == (status_b, body_b)
        assert _user_write_snapshot(store, user_b) == before_b_replay
        assert _user_write_snapshot(store, user_a) == before_a_during_b_replay
        assert _database_table_snapshot(store) == all_tables_before_b_replay

        completion_rows = []
        for user_id, task_id, title, response_body in (
            (user_a, task_a_id, title_a, body_a),
            (user_b, task_b_id, title_b, body_b),
        ):
            with store._connect() as conn:
                source_task = conn.execute(
                    "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                    (task_id, user_id),
                ).fetchone()
                done_events = conn.execute(
                    "SELECT * FROM task_events "
                    "WHERE task_id = ? AND event_type = ? AND payload = ? ORDER BY id",
                    (task_id, "status_changed", TaskStatus.DONE.value),
                ).fetchall()
                occurrences = conn.execute(
                    "SELECT * FROM task_done_occurrences WHERE user_id = ? ORDER BY source_task_id",
                    (user_id,),
                ).fetchall()
                ledger_rows = conn.execute(
                    "SELECT * FROM task_done_idempotency WHERE user_id = ? ORDER BY idempotency_key",
                    (user_id,),
                ).fetchall()
                user_tasks = conn.execute(
                    "SELECT * FROM tasks WHERE user_id = ? ORDER BY id", (user_id,)
                ).fetchall()

            assert source_task is not None
            assert source_task["status"] == TaskStatus.DONE.value
            assert source_task["recurrence"] == "daily"
            assert len(done_events) == 1
            assert len(occurrences) == 1
            assert len(ledger_rows) == 1
            assert len(user_tasks) == 2

            occurrence = occurrences[0]
            next_task_id = occurrence["next_task_id"]
            assert occurrence["source_task_id"] == task_id
            assert occurrence["source_done_event_id"] == done_events[0]["id"]
            assert next_task_id is not None
            next_tasks = [task for task in user_tasks if task["id"] == next_task_id]
            assert len(next_tasks) == 1
            next_task = next_tasks[0]
            assert next_task["user_id"] == user_id
            assert next_task["title"] == title
            assert next_task["status"] == TaskStatus.TODO.value
            assert next_task["recurrence"] == "daily"
            assert {task["id"] for task in user_tasks} == {task_id, next_task_id}

            expected_response = {
                "message": f"已创建下一期任务 #{next_task_id}：{title}"
            }
            assert json.loads(response_body) == expected_response
            assert occurrence["response_status"] == HTTPStatus.OK
            assert occurrence["response_json"].encode("utf-8") == response_body
            ledger = ledger_rows[0]
            assert ledger["idempotency_key"] == shared_key
            assert ledger["response_status"] == HTTPStatus.OK
            assert ledger["response_json"].encode("utf-8") == response_body
            completion_rows.append((user_id, done_events[0]["id"], next_task_id, ledger))

        assert completion_rows[0][0] != completion_rows[1][0]
        assert completion_rows[0][1] != completion_rows[1][1]
        assert completion_rows[0][2] != completion_rows[1][2]
        assert completion_rows[0][3]["idempotency_key"] == completion_rows[1][3]["idempotency_key"]
        assert completion_rows[0][3]["user_id"] != completion_rows[1][3]["user_id"]
    finally:
        _stop_done_http_server(
            server,
            server_thread,
            database_url,
            thread_started=thread_started,
        )


def test_done_http_cross_user_task_is_hidden_and_does_not_consume_idempotency_key(
    tmp_path, monkeypatch
):
    """A foreign-task attempt is indistinguishable from a missing task and writes nothing."""
    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    monkeypatch.delenv("MOMENTUM_DATABASE_URL", raising=False)

    database_path = tmp_path / "done-http-ownership.sqlite3"
    database_url = str(database_path)
    assert not database_path.exists()
    _store_cache.pop(database_url, None)

    user_a = f"done-http-owner-a-{uuid.uuid4().hex}"
    user_b = f"done-http-owner-b-{uuid.uuid4().hex}"
    password_a = f"test-password-{uuid.uuid4().hex}"
    password_b = f"test-password-{uuid.uuid4().hex}"
    title_a = f"HTTP owner A daily {uuid.uuid4().hex}"
    title_b = f"HTTP owner B daily {uuid.uuid4().hex}"
    assert user_a != user_b
    assert password_a != password_b
    assert title_a != title_b

    handler_type = type(
        "CrossUserOwnershipDoneHttpContractHandler",
        (MomentumHandler,),
        {"database_url": database_url},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread_started = False

    try:
        server_thread.start()
        thread_started = True
        base_url = f"http://127.0.0.1:{server.server_port}"
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_port > 0
        assert server.RequestHandlerClass is handler_type
        assert handler_type.database_url == database_url

        def register_and_login(user_id: str, password: str) -> str:
            register_status, register_body = _request(
                base_url,
                "POST",
                "/api/register",
                payload={
                    "user_id": user_id,
                    "display_name": user_id,
                    "password": password,
                },
            )
            assert register_status == HTTPStatus.OK
            assert json.loads(register_body) == {"message": "注册成功，请登录"}

            login_status, login_body = _request(
                base_url,
                "POST",
                "/api/login",
                payload={"user_id": user_id, "password": password},
            )
            assert login_status == HTTPStatus.OK
            login = json.loads(login_body)
            assert login["user_id"] == user_id
            assert login["token"]
            return login["token"]

        token_a = register_and_login(user_a, password_a)
        token_b = register_and_login(user_b, password_b)
        assert token_a != token_b

        for user_id, token in ((user_a, token_a), (user_b, token_b)):
            me_status, me_body = _request(base_url, "GET", "/api/me", token=token)
            assert me_status == HTTPStatus.OK
            assert json.loads(me_body) == {"user_id": user_id}

        store = _assert_handler_database(handler_type, database_url, database_path)
        assert _store_cache[database_url] is store

        def create_recurring_task(token: str, title: str) -> int:
            create_status, create_body = _request(
                base_url,
                "POST",
                "/api/tasks",
                token=token,
                payload={"text": f"每天 {title}"},
            )
            assert create_status == HTTPStatus.OK
            created = json.loads(create_body)
            task = next(item for item in created["tasks"] if item["title"] == title)
            assert task["recurrence"] == "daily"
            assert task["status"] == TaskStatus.TODO.value
            return task["id"]

        task_a_id = create_recurring_task(token_a, title_a)
        task_b_id = create_recurring_task(token_b, title_b)
        assert task_a_id != task_b_id
        assert _assert_handler_database(handler_type, database_url, database_path) is store

        with store._connect() as conn:
            task_owners = {
                row["id"]: row["user_id"]
                for row in conn.execute(
                    "SELECT id, user_id FROM tasks WHERE id IN (?, ?)",
                    (task_a_id, task_b_id),
                ).fetchall()
            }
            missing_task_id = conn.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 AS id FROM tasks"
            ).fetchone()["id"]
            assert conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (missing_task_id,)
            ).fetchone() is None
        assert task_owners == {task_a_id: user_a, task_b_id: user_b}

        key = str(uuid.uuid4())
        assert uuid.UUID(key).version == 4
        before_a = _user_write_snapshot(store, user_a)
        before_b = _user_write_snapshot(store, user_b)
        before_database = _database_table_snapshot(store)

        foreign_status, foreign_body = _post_done(base_url, token_a, task_b_id, key)
        expected_not_found = _exact_json({"error": "没有找到这个任务。"})
        assert foreign_status == HTTPStatus.NOT_FOUND
        assert foreign_body == expected_not_found
        assert _user_write_snapshot(store, user_a) == before_a
        assert _user_write_snapshot(store, user_b) == before_b
        assert _database_table_snapshot(store) == before_database

        missing_status, missing_body = _post_done(
            base_url, token_a, missing_task_id, key
        )
        assert (missing_status, missing_body) == (foreign_status, foreign_body)
        assert (missing_status, missing_body) == (HTTPStatus.NOT_FOUND, expected_not_found)
        assert _user_write_snapshot(store, user_a) == before_a
        assert _user_write_snapshot(store, user_b) == before_b
        assert _database_table_snapshot(store) == before_database
        with store._connect() as conn:
            assert conn.execute(
                "SELECT * FROM task_done_idempotency WHERE idempotency_key = ?",
                (key,),
            ).fetchall() == []

        status_b, body_b = _post_done(base_url, token_b, task_b_id, key)
        assert status_b == HTTPStatus.OK
        after_b = _user_write_snapshot(store, user_b)
        assert _user_write_snapshot(store, user_a) == before_a
        assert len(after_b[0]) == len(before_b[0]) + 1
        assert len(after_b[1]) == len(before_b[1]) + 2
        assert len(after_b[2]) == len(before_b[2]) + 1
        assert len(after_b[3]) == len(before_b[3]) + 1

        with store._connect() as conn:
            source_b = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (task_b_id, user_b),
            ).fetchone()
            done_events_b = conn.execute(
                "SELECT * FROM task_events "
                "WHERE task_id = ? AND event_type = ? AND payload = ? ORDER BY id",
                (task_b_id, "status_changed", TaskStatus.DONE.value),
            ).fetchall()
            occurrences_b = conn.execute(
                "SELECT * FROM task_done_occurrences WHERE user_id = ?",
                (user_b,),
            ).fetchall()
            ledger_b = conn.execute(
                "SELECT * FROM task_done_idempotency WHERE user_id = ?",
                (user_b,),
            ).fetchall()
            tasks_b = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? ORDER BY id", (user_b,)
            ).fetchall()

        assert source_b is not None
        assert source_b["status"] == TaskStatus.DONE.value
        assert source_b["recurrence"] == "daily"
        assert len(done_events_b) == 1
        assert len(occurrences_b) == 1
        assert len(ledger_b) == 1
        assert ledger_b[0]["idempotency_key"] == key
        assert ledger_b[0]["response_status"] == HTTPStatus.OK
        occurrence_b = occurrences_b[0]
        assert occurrence_b["source_task_id"] == task_b_id
        assert occurrence_b["source_done_event_id"] == done_events_b[0]["id"]
        next_b_id = occurrence_b["next_task_id"]
        assert next_b_id is not None
        next_b = next(task for task in tasks_b if task["id"] == next_b_id)
        assert {task["id"] for task in tasks_b} == {task_b_id, next_b_id}
        assert next_b["user_id"] == user_b
        assert next_b["title"] == title_b
        assert next_b["status"] == TaskStatus.TODO.value
        assert next_b["recurrence"] == "daily"
        expected_body_b = _exact_json(
            {"message": f"已创建下一期任务 #{next_b_id}：{title_b}"}
        )
        assert body_b == expected_body_b
        assert title_a.encode("utf-8") not in body_b
        assert occurrence_b["response_status"] == HTTPStatus.OK
        assert occurrence_b["response_json"].encode("utf-8") == body_b
        assert ledger_b[0]["response_json"].encode("utf-8") == body_b
        database_after_b = _database_table_snapshot(store)
        assert database_after_b != before_database

        status_a, body_a = _post_done(base_url, token_a, task_a_id, key)
        assert status_a == HTTPStatus.OK
        after_a = _user_write_snapshot(store, user_a)
        assert _user_write_snapshot(store, user_b) == after_b
        assert len(after_a[0]) == len(before_a[0]) + 1
        assert len(after_a[1]) == len(before_a[1]) + 2
        assert len(after_a[2]) == len(before_a[2]) + 1
        assert len(after_a[3]) == len(before_a[3]) + 1

        with store._connect() as conn:
            source_a = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (task_a_id, user_a),
            ).fetchone()
            done_events_a = conn.execute(
                "SELECT * FROM task_events "
                "WHERE task_id = ? AND event_type = ? AND payload = ? ORDER BY id",
                (task_a_id, "status_changed", TaskStatus.DONE.value),
            ).fetchall()
            occurrences_a = conn.execute(
                "SELECT * FROM task_done_occurrences WHERE user_id = ?",
                (user_a,),
            ).fetchall()
            ledger_a = conn.execute(
                "SELECT * FROM task_done_idempotency WHERE user_id = ?",
                (user_a,),
            ).fetchall()
            tasks_a = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? ORDER BY id", (user_a,)
            ).fetchall()
            shared_key_rows = conn.execute(
                "SELECT * FROM task_done_idempotency WHERE idempotency_key = ?",
                (key,),
            ).fetchall()

        assert source_a is not None
        assert source_a["status"] == TaskStatus.DONE.value
        assert source_a["recurrence"] == "daily"
        assert len(done_events_a) == 1
        assert len(occurrences_a) == 1
        assert len(ledger_a) == 1
        assert ledger_a[0]["idempotency_key"] == key
        assert ledger_a[0]["response_status"] == HTTPStatus.OK
        occurrence_a = occurrences_a[0]
        assert occurrence_a["source_task_id"] == task_a_id
        assert occurrence_a["source_done_event_id"] == done_events_a[0]["id"]
        next_a_id = occurrence_a["next_task_id"]
        assert next_a_id is not None
        next_a = next(task for task in tasks_a if task["id"] == next_a_id)
        assert {task["id"] for task in tasks_a} == {task_a_id, next_a_id}
        assert next_a["user_id"] == user_a
        assert next_a["title"] == title_a
        assert next_a["status"] == TaskStatus.TODO.value
        assert next_a["recurrence"] == "daily"
        expected_body_a = _exact_json(
            {"message": f"已创建下一期任务 #{next_a_id}：{title_a}"}
        )
        assert body_a == expected_body_a
        assert title_b.encode("utf-8") not in body_a
        assert occurrence_a["response_status"] == HTTPStatus.OK
        assert occurrence_a["response_json"].encode("utf-8") == body_a
        assert ledger_a[0]["response_json"].encode("utf-8") == body_a
        assert len(shared_key_rows) == 2
        assert {row["user_id"] for row in shared_key_rows} == {user_a, user_b}
        assert {row["idempotency_key"] for row in shared_key_rows} == {key}

        database_after_a = _database_table_snapshot(store)
        assert database_after_a != database_after_b
        before_replays_a = _user_write_snapshot(store, user_a)
        before_replays_b = _user_write_snapshot(store, user_b)
        replay_b = _post_done(base_url, token_b, task_b_id, key)
        assert replay_b == (status_b, body_b)
        assert _user_write_snapshot(store, user_a) == before_replays_a
        assert _user_write_snapshot(store, user_b) == before_replays_b
        assert _database_table_snapshot(store) == database_after_a
        replay_a = _post_done(base_url, token_a, task_a_id, key)
        assert replay_a == (status_a, body_a)
        assert _user_write_snapshot(store, user_a) == before_replays_a
        assert _user_write_snapshot(store, user_b) == before_replays_b
        assert _database_table_snapshot(store) == database_after_a
    finally:
        _stop_done_http_server(
            server,
            server_thread,
            database_url,
            thread_started=thread_started,
        )



def test_done_http_same_task_same_key_concurrent_requests_replay_one_result(
    tmp_path, monkeypatch
):
    """Two real HTTP handlers racing on one task/key must persist one result."""
    from momentum_agent.web import handlers as web_handlers

    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    monkeypatch.delenv("MOMENTUM_DATABASE_URL", raising=False)
    database_path = tmp_path / "done-http-same-key-race.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)
    store = SQLiteTaskStore(database_path)

    user_id = f"done-http-same-key-{uuid.uuid4().hex}"
    password = f"test-password-{uuid.uuid4().hex}"
    title = f"HTTP same-key daily {uuid.uuid4().hex}"
    gate_lock = threading.Lock()
    gate_state = {"barrier": None, "participants": [], "releases": []}
    original_done = web_handlers.handle_done_task

    def synchronized_done(handler, path, authenticated_user_id):
        with gate_lock:
            barrier = gate_state["barrier"]
            participant = (threading.get_ident(), path, authenticated_user_id)
        if barrier is not None:
            with gate_lock:
                gate_state["participants"].append(participant)
            release_index = barrier.wait(timeout=10)
            with gate_lock:
                gate_state["releases"].append((participant[0], release_index))
        return original_done(handler, path, authenticated_user_id)

    post_routes = dict(MomentumHandler.POST_PREFIX_ROUTES)
    post_routes["done"] = synchronized_done
    handler_type = type(
        "SameKeyRaceDoneHttpContractHandler",
        (MomentumHandler,),
        {"database_url": database_url, "POST_PREFIX_ROUTES": post_routes},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread_started = False

    try:
        server_thread.start()
        thread_started = True
        base_url = f"http://127.0.0.1:{server.server_port}"
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_port > 0
        assert server.RequestHandlerClass is handler_type

        register_status, register_body = _request(
            base_url,
            "POST",
            "/api/register",
            payload={"user_id": user_id, "display_name": user_id, "password": password},
        )
        assert register_status == HTTPStatus.OK
        assert json.loads(register_body) == {"message": "注册成功，请登录"}
        login_status, login_body = _request(
            base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        assert login_status == HTTPStatus.OK
        token = json.loads(login_body)["token"]
        assert token
        me_status, me_body = _request(base_url, "GET", "/api/me", token=token)
        assert me_status == HTTPStatus.OK
        assert json.loads(me_body) == {"user_id": user_id}

        create_status, create_body = _request(
            base_url,
            "POST",
            "/api/tasks",
            token=token,
            payload={"text": f"每天 {title}"},
        )
        assert create_status == HTTPStatus.OK
        created = json.loads(create_body)
        source_task = next(task for task in created["tasks"] if task["title"] == title)
        task_id = source_task["id"]
        assert source_task["status"] == TaskStatus.TODO.value
        assert source_task["recurrence"] == "daily"
        store = _assert_handler_database(handler_type, database_url, database_path)

        key = str(uuid.uuid4())
        assert uuid.UUID(key).version == 4
        before_race = _user_write_snapshot(store, user_id)
        client_barrier = threading.Barrier(2)
        dispatch_barrier = threading.Barrier(2)
        with gate_lock:
            gate_state["barrier"] = dispatch_barrier
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            def post_after_client_barrier():
                client_barrier.wait(timeout=10)
                return _post_done(base_url, token, task_id, key, timeout=15)

            futures = [pool.submit(post_after_client_barrier) for _ in range(2)]
            responses = [future.result(timeout=20) for future in futures]
        finally:
            client_barrier.abort()
            dispatch_barrier.abort()
            with gate_lock:
                if gate_state["barrier"] is dispatch_barrier:
                    gate_state["barrier"] = None
            pool.shutdown(wait=True, cancel_futures=True)

        with gate_lock:
            participants = list(gate_state["participants"])
            releases = list(gate_state["releases"])
        assert len(participants) == 2
        assert {participant[0] for participant in participants}.__len__() == 2
        assert {participant[1] for participant in participants} == {
            f"/api/tasks/{task_id}/done"
        }
        assert {participant[2] for participant in participants} == {user_id}
        assert len(releases) == 2
        assert sorted(index for _, index in releases) == [0, 1]
        assert sorted(status for status, _ in responses) == [HTTPStatus.OK] * 2
        assert responses[0] == responses[1]
        first_status, first_body = responses[0]

        events, occurrences, recurring_tasks, ledger = _source_done_state(
            store, user_id, task_id, title
        )
        assert store.get_task_for_user(task_id, user_id).status == TaskStatus.DONE
        assert len(events) == 1
        assert events[0]["payload"] == TaskStatus.DONE.value
        assert len(occurrences) == 1
        assert len(recurring_tasks) == 2
        next_task = next(row for row in recurring_tasks if row["id"] != task_id)
        assert next_task["status"] == TaskStatus.TODO.value
        assert next_task["recurrence"] == "daily"
        occurrence = occurrences[0]
        assert occurrence["user_id"] == user_id
        assert occurrence["source_task_id"] == task_id
        assert occurrence["source_done_event_id"] == events[0]["id"]
        assert occurrence["next_task_id"] == next_task["id"]
        assert occurrence["response_status"] == HTTPStatus.OK
        expected_body = _exact_json(
            {"message": f"已创建下一期任务 #{next_task['id']}：{title}"}
        )
        assert first_body == expected_body
        assert occurrence["response_json"].encode("utf-8") == first_body
        assert len(ledger) == 1
        assert ledger[0]["idempotency_key"] == key
        assert ledger[0]["response_status"] == HTTPStatus.OK
        assert ledger[0]["response_json"].encode("utf-8") == first_body
        after_race = _user_write_snapshot(store, user_id)
        assert after_race != before_race

        for _ in range(2):
            before_replay = _user_write_snapshot(store, user_id)
            replay_status, replay_body = _post_done(base_url, token, task_id, key)
            assert (replay_status, replay_body) == (first_status, first_body)
            assert _user_write_snapshot(store, user_id) == before_replay
        assert _user_write_snapshot(store, user_id) == after_race
    finally:
        _stop_done_http_server(
            server,
            server_thread,
            database_url,
            thread_started=thread_started,
        )


def test_done_http_same_key_concurrent_users_have_isolated_results_and_replays(
    tmp_path, monkeypatch
):
    """Concurrent users may independently use the same key over real HTTP."""
    from momentum_agent.web import handlers as web_handlers

    monkeypatch.delenv("MOMENTUM_TEST_MYSQL_URL", raising=False)
    monkeypatch.delenv("MOMENTUM_DATABASE_URL", raising=False)
    database_path = tmp_path / "done-http-cross-user-race.sqlite3"
    database_url = str(database_path)
    _store_cache.pop(database_url, None)

    user_a = f"done-http-race-a-{uuid.uuid4().hex}"
    user_b = f"done-http-race-b-{uuid.uuid4().hex}"
    password_a = f"test-password-{uuid.uuid4().hex}"
    password_b = f"test-password-{uuid.uuid4().hex}"
    title_a = f"HTTP race user A daily {uuid.uuid4().hex}"
    title_b = f"HTTP race user B daily {uuid.uuid4().hex}"
    gate_lock = threading.Lock()
    gate_state = {"barrier": None, "participants": [], "releases": []}
    original_done = web_handlers.handle_done_task

    def synchronized_done(handler, path, authenticated_user_id):
        with gate_lock:
            barrier = gate_state["barrier"]
            participant = (threading.get_ident(), path, authenticated_user_id)
        if barrier is not None:
            with gate_lock:
                gate_state["participants"].append(participant)
            release_index = barrier.wait(timeout=10)
            with gate_lock:
                gate_state["releases"].append((participant[0], release_index))
        return original_done(handler, path, authenticated_user_id)

    post_routes = dict(MomentumHandler.POST_PREFIX_ROUTES)
    post_routes["done"] = synchronized_done
    handler_type = type(
        "CrossUserRaceDoneHttpContractHandler",
        (MomentumHandler,),
        {"database_url": database_url, "POST_PREFIX_ROUTES": post_routes},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread_started = False

    try:
        server_thread.start()
        thread_started = True
        base_url = f"http://127.0.0.1:{server.server_port}"
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_port > 0
        assert server.RequestHandlerClass is handler_type

        def register_and_login(user_id: str, password: str) -> str:
            register_status, register_body = _request(
                base_url,
                "POST",
                "/api/register",
                payload={"user_id": user_id, "display_name": user_id, "password": password},
            )
            assert register_status == HTTPStatus.OK
            assert json.loads(register_body) == {"message": "注册成功，请登录"}
            login_status, login_body = _request(
                base_url,
                "POST",
                "/api/login",
                payload={"user_id": user_id, "password": password},
            )
            assert login_status == HTTPStatus.OK
            login = json.loads(login_body)
            assert login["user_id"] == user_id
            assert login["token"]
            return login["token"]

        token_a = register_and_login(user_a, password_a)
        token_b = register_and_login(user_b, password_b)
        assert token_a != token_b
        for user_id, token in ((user_a, token_a), (user_b, token_b)):
            me_status, me_body = _request(base_url, "GET", "/api/me", token=token)
            assert me_status == HTTPStatus.OK
            assert json.loads(me_body) == {"user_id": user_id}

        store = _assert_handler_database(handler_type, database_url, database_path)

        def create_recurring_task(token: str, title: str) -> int:
            create_status, create_body = _request(
                base_url,
                "POST",
                "/api/tasks",
                token=token,
                payload={"text": f"每天 {title}"},
            )
            assert create_status == HTTPStatus.OK
            created = json.loads(create_body)
            task = next(item for item in created["tasks"] if item["title"] == title)
            assert task["status"] == TaskStatus.TODO.value
            assert task["recurrence"] == "daily"
            return task["id"]

        task_a_id = create_recurring_task(token_a, title_a)
        task_b_id = create_recurring_task(token_b, title_b)
        assert task_a_id != task_b_id
        with store._connect() as conn:
            initial_owners = {
                row["id"]: row["user_id"]
                for row in conn.execute(
                    "SELECT id, user_id FROM tasks WHERE id IN (?, ?)",
                    (task_a_id, task_b_id),
                ).fetchall()
            }
        assert initial_owners == {task_a_id: user_a, task_b_id: user_b}

        before_a = _user_write_snapshot(store, user_a)
        before_b = _user_write_snapshot(store, user_b)
        key = str(uuid.uuid4())
        assert uuid.UUID(key).version == 4
        client_barrier = threading.Barrier(2)
        dispatch_barrier = threading.Barrier(2)
        with gate_lock:
            gate_state["barrier"] = dispatch_barrier
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            def post_after_client_barrier(user_id: str, token: str, task_id: int):
                client_barrier.wait(timeout=10)
                response = _post_done(base_url, token, task_id, key, timeout=15)
                return user_id, task_id, response

            futures = [
                pool.submit(post_after_client_barrier, user_a, token_a, task_a_id),
                pool.submit(post_after_client_barrier, user_b, token_b, task_b_id),
            ]
            concurrent_results = [future.result(timeout=20) for future in futures]
        finally:
            client_barrier.abort()
            dispatch_barrier.abort()
            with gate_lock:
                if gate_state["barrier"] is dispatch_barrier:
                    gate_state["barrier"] = None
            pool.shutdown(wait=True, cancel_futures=True)

        with gate_lock:
            participants = list(gate_state["participants"])
            releases = list(gate_state["releases"])
        assert len(participants) == 2
        assert len({participant[0] for participant in participants}) == 2
        assert {(path, owner) for _, path, owner in participants} == {
            (f"/api/tasks/{task_a_id}/done", user_a),
            (f"/api/tasks/{task_b_id}/done", user_b),
        }
        assert len(releases) == 2
        assert sorted(index for _, index in releases) == [0, 1]

        response_by_user = {
            user_id: (task_id, response)
            for user_id, task_id, response in concurrent_results
        }
        task_id_a, response_a = response_by_user[user_a]
        task_id_b, response_b = response_by_user[user_b]
        assert task_id_a == task_a_id
        assert task_id_b == task_b_id
        status_a, body_a = response_a
        status_b, body_b = response_b
        assert status_a == status_b == HTTPStatus.OK
        assert body_a != body_b
        assert title_a.encode("utf-8") in body_a
        assert title_b.encode("utf-8") not in body_a
        assert title_b.encode("utf-8") in body_b
        assert title_a.encode("utf-8") not in body_b

        completion_details = {}
        for user_id, task_id, title, response in (
            (user_a, task_a_id, title_a, response_a),
            (user_b, task_b_id, title_b, response_b),
        ):
            status, raw_body = response
            events, occurrences, recurring_tasks, ledger = _source_done_state(
                store, user_id, task_id, title
            )
            assert store.get_task_for_user(task_id, user_id).status == TaskStatus.DONE
            assert len(events) == 1
            assert events[0]["payload"] == TaskStatus.DONE.value
            assert len(occurrences) == 1
            assert len(recurring_tasks) == 2
            source = next(row for row in recurring_tasks if row["id"] == task_id)
            next_task = next(row for row in recurring_tasks if row["id"] != task_id)
            assert source["status"] == TaskStatus.DONE.value
            assert source["recurrence"] == "daily"
            assert next_task["status"] == TaskStatus.TODO.value
            assert next_task["recurrence"] == "daily"
            occurrence = occurrences[0]
            assert occurrence["user_id"] == user_id
            assert occurrence["source_task_id"] == task_id
            assert occurrence["source_done_event_id"] == events[0]["id"]
            assert occurrence["next_task_id"] == next_task["id"]
            assert occurrence["response_status"] == status == HTTPStatus.OK
            expected_body = _exact_json(
                {"message": f"已创建下一期任务 #{next_task['id']}：{title}"}
            )
            assert raw_body == expected_body
            assert occurrence["response_json"].encode("utf-8") == raw_body
            assert len(ledger) == 1
            assert ledger[0]["idempotency_key"] == key
            assert ledger[0]["response_status"] == status
            assert ledger[0]["response_json"].encode("utf-8") == raw_body
            completion_details[user_id] = (task_id, next_task["id"], ledger[0])

        with store._connect() as conn:
            all_key_rows = conn.execute(
                "SELECT user_id, idempotency_key FROM task_done_idempotency "
                "WHERE idempotency_key = ? ORDER BY user_id",
                (key,),
            ).fetchall()
            ownership_rows = conn.execute(
                "SELECT id, user_id, title FROM tasks WHERE id IN (?, ?, ?, ?) ORDER BY id",
                (
                    task_a_id,
                    completion_details[user_a][1],
                    task_b_id,
                    completion_details[user_b][1],
                ),
            ).fetchall()
        assert [tuple(row) for row in all_key_rows] == [
            (user_a, key),
            (user_b, key),
        ]
        assert {row["id"]: row["user_id"] for row in ownership_rows} == {
            task_a_id: user_a,
            completion_details[user_a][1]: user_a,
            task_b_id: user_b,
            completion_details[user_b][1]: user_b,
        }
        assert {row["id"]: row["title"] for row in ownership_rows} == {
            task_a_id: title_a,
            completion_details[user_a][1]: title_a,
            task_b_id: title_b,
            completion_details[user_b][1]: title_b,
        }
        assert completion_details[user_a][2]["idempotency_key"] == key
        assert completion_details[user_b][2]["idempotency_key"] == key

        after_a = _user_write_snapshot(store, user_a)
        after_b = _user_write_snapshot(store, user_b)
        assert after_a != before_a
        assert after_b != before_b
        database_after_race = _database_table_snapshot(store)

        before_replays_a = _user_write_snapshot(store, user_a)
        before_replays_b = _user_write_snapshot(store, user_b)
        replay_a = _post_done(base_url, token_a, task_a_id, key)
        assert replay_a == response_a
        assert _user_write_snapshot(store, user_a) == before_replays_a
        assert _user_write_snapshot(store, user_b) == before_replays_b
        assert _database_table_snapshot(store) == database_after_race

        replay_b = _post_done(base_url, token_b, task_b_id, key)
        assert replay_b == response_b
        assert _user_write_snapshot(store, user_a) == before_replays_a
        assert _user_write_snapshot(store, user_b) == before_replays_b
        assert _database_table_snapshot(store) == database_after_race
    finally:
        _stop_done_http_server(
            server,
            server_thread,
            database_url,
            thread_started=thread_started,
        )



def test_done_http_last_recurring_child_completes_parent_without_parent_next(done_http):
    """The real HTTP done route creates a next only for the explicit child target."""
    store, base_url, user_id, token = done_http
    suffix = uuid.uuid4().hex
    parent_title = f"HTTP cascade daily parent {suffix}"
    child_title = f"HTTP cascade weekly child {suffix}"
    parent = _new_recurring_task(store, user_id, parent_title)
    child = store.create_task(
        child_title,
        due_at=DUE_AT,
        recurrence="weekly",
        parent_task_id=parent.id,
        user_id=user_id,
    )
    assert store.get_task_for_user(parent.id, user_id).status == TaskStatus.TODO
    assert store.get_task_for_user(child.id, user_id).status == TaskStatus.TODO
    with store._connect() as conn:
        initial_children = conn.execute(
            "SELECT id, status FROM tasks WHERE user_id = ? AND parent_task_id = ? ORDER BY id",
            (user_id, parent.id),
        ).fetchall()
    assert [(row["id"], row["status"]) for row in initial_children] == [
        (child.id, TaskStatus.TODO.value)
    ]

    key = str(uuid.uuid4())
    assert uuid.UUID(key).version == 4
    status, response_body = _post_done(base_url, token, child.id, key)

    assert status == HTTPStatus.OK
    with store._connect() as conn:
        tasks = conn.execute(
            "SELECT id, title, status, recurrence, parent_task_id "
            "FROM tasks WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
        parent_done_events = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id = ? AND event_type = ? ORDER BY id",
            (parent.id, "status_changed"),
        ).fetchall()
        parent_subtasks_completed = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND event_type = ? ORDER BY id",
            (parent.id, "subtasks_completed"),
        ).fetchall()
        child_done_events = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id = ? AND event_type = ? ORDER BY id",
            (child.id, "status_changed"),
        ).fetchall()
        occurrences = conn.execute(
            "SELECT * FROM task_done_occurrences WHERE user_id = ? "
            "ORDER BY source_task_id, source_done_event_id",
            (user_id,),
        ).fetchall()
        ledger = conn.execute(
            "SELECT * FROM task_done_idempotency WHERE user_id = ? ORDER BY idempotency_key",
            (user_id,),
        ).fetchall()

    parent_tasks = [row for row in tasks if row["title"] == parent_title]
    child_tasks = [row for row in tasks if row["title"] == child_title]
    assert len(tasks) == 3
    assert len(parent_tasks) == 1
    assert parent_tasks[0]["id"] == parent.id
    assert parent_tasks[0]["status"] == TaskStatus.DONE.value
    assert parent_tasks[0]["recurrence"] == "daily"
    assert len(child_tasks) == 2
    completed_child = next(row for row in child_tasks if row["id"] == child.id)
    next_children = [row for row in child_tasks if row["id"] != child.id]
    assert len(next_children) == 1
    next_child = next_children[0]
    assert completed_child["status"] == TaskStatus.DONE.value
    assert completed_child["recurrence"] == "weekly"
    assert completed_child["parent_task_id"] == parent.id
    assert next_child["status"] == TaskStatus.TODO.value
    assert next_child["recurrence"] == "weekly"

    assert store.get_task_for_user(parent.id, user_id).status == TaskStatus.DONE
    assert store.get_task_for_user(child.id, user_id).status == TaskStatus.DONE
    assert len(parent_done_events) == 1
    assert parent_done_events[0]["payload"] == TaskStatus.DONE.value
    assert parent_subtasks_completed == []
    assert len(child_done_events) == 1
    assert child_done_events[0]["payload"] == TaskStatus.DONE.value

    expected_response = {
        "message": f"已创建下一期任务 #{next_child['id']}：{child_title}"
    }
    assert response_body == _exact_json(expected_response)
    assert json.loads(response_body) == expected_response
    assert parent_title not in expected_response["message"]

    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert occurrence["user_id"] == user_id
    assert occurrence["source_task_id"] == child.id
    assert occurrence["source_done_event_id"] == child_done_events[0]["id"]
    assert occurrence["next_task_id"] == next_child["id"]
    assert occurrence["response_status"] == status == HTTPStatus.OK
    assert occurrence["response_json"].encode("utf-8") == response_body

    assert len(ledger) == 1
    assert ledger[0]["idempotency_key"] == key
    assert ledger[0]["response_status"] == status
    assert ledger[0]["response_json"].encode("utf-8") == response_body
    assert child_title in ledger[0]["response_json"]
    assert str(next_child["id"]) in ledger[0]["response_json"]

    after_first_request = _user_write_snapshot(store, user_id)
    replay_status, replay_body = _post_done(base_url, token, child.id, key)
    assert replay_status == status
    assert replay_body == response_body
    assert _user_write_snapshot(store, user_id) == after_first_request



def test_done_http_rolls_back_ledger_insert_failure_and_retries_same_key(
    done_http, tmp_path
):
    import hashlib

    store, base_url, user_id, token = done_http
    title = f"HTTP rollback daily {uuid.uuid4().hex}"
    task = _new_recurring_task(store, user_id, title)
    key = str(uuid.uuid4())
    assert uuid.UUID(key).version == 4
    assert task.status == TaskStatus.TODO
    assert task.recurrence == "daily"
    assert task.due_at is not None

    database_path = store.db_path.resolve()
    assert database_path.parent == tmp_path.resolve()
    with store._connect() as conn:
        main_database = next(
            row for row in conn.execute("PRAGMA database_list").fetchall()
            if row["name"] == "main"
        )
    assert Path(main_database["file"]).resolve() == database_path

    before = _user_write_snapshot(store, user_id)
    canonical_request = json.dumps(
        {"method": "POST", "task_id": task.id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request_fingerprint = hashlib.sha256(canonical_request).hexdigest()
    trigger_name = "test_fail_done_ledger_insert_for_request"
    # The ledger has no task_id column; its canonical fingerprint binds this trigger
    # to this task, while the user and UUID key further scope it to this request.
    with store._connect() as conn:
        conn.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON task_done_idempotency
            WHEN NEW.user_id = '{user_id}'
             AND NEW.idempotency_key = '{key}'
             AND NEW.request_fingerprint = '{request_fingerprint}'
            BEGIN
                SELECT RAISE(ABORT, 'test-only ledger insert failure');
            END
            """
        )

    try:
        failed_status, failed_body = _post_done(base_url, token, task.id, key)

        assert failed_status == HTTPStatus.INTERNAL_SERVER_ERROR
        failure_payload = json.loads(failed_body)
        assert isinstance(failure_payload, dict)
        assert isinstance(failure_payload.get("error"), str)
        assert failure_payload["error"].startswith("服务器错误：")
        assert token not in failure_payload["error"]
        assert _user_write_snapshot(store, user_id) == before
        assert store.get_task_for_user(task.id, user_id).status == TaskStatus.TODO

        failed_events, failed_occurrences, failed_tasks, failed_ledger = _source_done_state(
            store, user_id, task.id, title
        )
        assert failed_events == []
        assert failed_occurrences == []
        assert len(failed_tasks) == 1
        assert failed_tasks[0]["id"] == task.id
        assert failed_tasks[0]["status"] == TaskStatus.TODO.value
        assert failed_tasks[0]["recurrence"] == "daily"
        assert failed_ledger == []
    finally:
        with store._connect() as conn:
            conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')

    retry_status, retry_body = _post_done(base_url, token, task.id, key)
    assert retry_status == HTTPStatus.OK

    events, occurrences, recurring_tasks, ledger = _source_done_state(
        store, user_id, task.id, title
    )
    assert store.get_task_for_user(task.id, user_id).status == TaskStatus.DONE
    assert len(events) == 1
    assert events[0]["payload"] == TaskStatus.DONE.value
    assert len(occurrences) == 1
    assert len(recurring_tasks) == 2
    source_task = next(row for row in recurring_tasks if row["id"] == task.id)
    next_task = next(row for row in recurring_tasks if row["id"] != task.id)
    assert source_task["status"] == TaskStatus.DONE.value
    assert next_task["status"] == TaskStatus.TODO.value
    assert next_task["recurrence"] == "daily"
    expected_response = {
        "message": f"已创建下一期任务 #{next_task['id']}：{title}"
    }
    assert retry_body == _exact_json(expected_response)
    assert json.loads(retry_body) == expected_response

    occurrence = occurrences[0]
    assert occurrence["next_task_id"] == next_task["id"]
    assert occurrence["response_status"] == HTTPStatus.OK
    assert occurrence["response_json"].encode("utf-8") == retry_body
    assert len(ledger) == 1
    assert ledger[0]["idempotency_key"] == key
    assert ledger[0]["response_status"] == HTTPStatus.OK
    assert ledger[0]["response_json"].encode("utf-8") == retry_body

    after_retry = _user_write_snapshot(store, user_id)
    replay_status, replay_body = _post_done(base_url, token, task.id, key)
    assert replay_status == retry_status
    assert replay_body == retry_body
    assert _user_write_snapshot(store, user_id) == after_retry



def test_done_http_rolls_back_next_task_insert_failure_and_retries_same_key(
    done_http, tmp_path
):
    store, base_url, user_id, token = done_http
    title = f"HTTP next insert rollback daily {uuid.uuid4().hex}"
    source = _new_recurring_task(store, user_id, title)
    key = str(uuid.uuid4())
    assert uuid.UUID(key).version == 4
    assert source.status == TaskStatus.TODO
    assert source.recurrence == "daily"
    assert base_url.startswith("http://127.0.0.1:")

    database_path = store.db_path.resolve()
    assert database_path.parent == tmp_path.resolve()
    with store._connect() as conn:
        main_database = next(
            row for row in conn.execute("PRAGMA database_list").fetchall()
            if row["name"] == "main"
        )
    assert Path(main_database["file"]).resolve() == database_path

    before = _user_write_snapshot(store, user_id)
    initial_events, initial_occurrences, initial_tasks, initial_ledger = _source_done_state(
        store, user_id, source.id, title
    )
    assert initial_events == []
    assert initial_occurrences == []
    assert len(initial_tasks) == 1
    assert initial_tasks[0]["id"] == source.id
    assert initial_tasks[0]["status"] == TaskStatus.TODO.value
    assert initial_tasks[0]["recurrence"] == "daily"
    assert initial_ledger == []

    trigger_name = "test_fail_done_next_task_insert_for_request"
    trigger_user_id = user_id.replace("'", "''")
    trigger_title = title.replace("'", "''")
    try:
        with store._connect() as conn:
            conn.execute(
                f"""
                CREATE TRIGGER \"{trigger_name}\"
                BEFORE INSERT ON tasks
                WHEN NEW.user_id = '{trigger_user_id}'
                 AND NEW.title = '{trigger_title}'
                 AND NEW.status = 'todo'
                 AND NEW.recurrence = 'daily'
                BEGIN
                    SELECT RAISE(ABORT, 'test-only next-task insert failure');
                END
                """
            )

        failed_status, failed_body = _post_done(base_url, token, source.id, key)
        assert failed_status == HTTPStatus.INTERNAL_SERVER_ERROR
        failure_payload = json.loads(failed_body)
        assert isinstance(failure_payload, dict)
        assert isinstance(failure_payload.get("error"), str)
        assert failure_payload["error"].startswith("服务器错误：")
        assert token not in failure_payload["error"]
        assert store.get_task_for_user(source.id, user_id).status == TaskStatus.TODO
        assert _user_write_snapshot(store, user_id) == before

        failed_events, failed_occurrences, failed_tasks, failed_ledger = _source_done_state(
            store, user_id, source.id, title
        )
        assert failed_events == []
        assert failed_occurrences == []
        assert len(failed_tasks) == 1
        assert failed_tasks[0]["id"] == source.id
        assert failed_tasks[0]["status"] == TaskStatus.TODO.value
        assert failed_tasks[0]["recurrence"] == "daily"
        assert failed_ledger == []
    finally:
        with store._connect() as conn:
            conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')

    retry_status, retry_body = _post_done(base_url, token, source.id, key)
    assert retry_status == HTTPStatus.OK
    events, occurrences, recurring_tasks, ledger = _source_done_state(
        store, user_id, source.id, title
    )
    assert store.get_task_for_user(source.id, user_id).status == TaskStatus.DONE
    assert len(events) == 1
    assert events[0]["payload"] == TaskStatus.DONE.value
    assert len(occurrences) == 1
    assert len(recurring_tasks) == 2
    source_row = next(row for row in recurring_tasks if row["id"] == source.id)
    next_task = next(row for row in recurring_tasks if row["id"] != source.id)
    assert source_row["status"] == TaskStatus.DONE.value
    assert source_row["recurrence"] == "daily"
    assert next_task["status"] == TaskStatus.TODO.value
    assert next_task["recurrence"] == "daily"
    assert len(ledger) == 1
    assert ledger[0]["idempotency_key"] == key

    expected_response = {
        "message": f"已创建下一期任务 #{next_task['id']}：{title}"
    }
    assert retry_body == _exact_json(expected_response)
    assert json.loads(retry_body) == expected_response
    occurrence = occurrences[0]
    assert occurrence["user_id"] == user_id
    assert occurrence["source_task_id"] == source.id
    assert occurrence["source_done_event_id"] == events[0]["id"]
    assert occurrence["next_task_id"] == next_task["id"]
    assert occurrence["response_status"] == HTTPStatus.OK
    assert occurrence["response_json"].encode("utf-8") == retry_body
    assert ledger[0]["response_status"] == HTTPStatus.OK
    assert ledger[0]["response_json"].encode("utf-8") == retry_body
    assert occurrence["response_json"].encode("utf-8") == ledger[0][
        "response_json"
    ].encode("utf-8")

    after_retry = _user_write_snapshot(store, user_id)
    replay_status, replay_body = _post_done(base_url, token, source.id, key)
    assert replay_status == retry_status
    assert replay_body == retry_body
    assert _user_write_snapshot(store, user_id) == after_retry


def test_done_http_rolls_back_occurrence_mapping_insert_failure_and_retries_same_key(
    done_http, tmp_path
):
    store, base_url, user_id, token = done_http
    title = f"HTTP mapping insert rollback daily {uuid.uuid4().hex}"
    source = _new_recurring_task(store, user_id, title)
    key = str(uuid.uuid4())
    assert uuid.UUID(key).version == 4
    assert source.status == TaskStatus.TODO
    assert source.recurrence == "daily"
    assert source.due_at is not None
    assert base_url.startswith("http://127.0.0.1:")
    database_path = store.db_path.resolve()
    assert database_path.parent == tmp_path.resolve()
    with store._connect() as conn:
        main_database = next(
            row for row in conn.execute("PRAGMA database_list").fetchall()
            if row["name"] == "main"
        )
    assert Path(main_database["file"]).resolve() == database_path

    before = _user_write_snapshot(store, user_id)
    initial_events, initial_occurrences, initial_tasks, initial_ledger = _source_done_state(
        store, user_id, source.id, title
    )
    assert initial_events == []
    assert initial_occurrences == []
    assert len(initial_tasks) == 1
    assert initial_tasks[0]["id"] == source.id
    assert initial_tasks[0]["status"] == TaskStatus.TODO.value
    assert initial_tasks[0]["recurrence"] == "daily"
    assert initial_ledger == []

    trigger_name = "test_fail_done_occurrence_mapping_insert_for_request"
    trigger_user_id = user_id.replace("'", "''")
    try:
        with store._connect() as conn:
            conn.execute(
                f"""
                CREATE TRIGGER \"{trigger_name}\"
                BEFORE INSERT ON task_done_occurrences
                WHEN NEW.user_id = '{trigger_user_id}'
                 AND NEW.source_task_id = {source.id}
                BEGIN
                    SELECT RAISE(ABORT, 'test-only mapping insert failure');
                END
                """
            )

        failed_status, failed_body = _post_done(
            base_url, token, source.id, key
        )
        assert failed_status == HTTPStatus.INTERNAL_SERVER_ERROR
        failure_payload = json.loads(failed_body)
        assert isinstance(failure_payload, dict)
        assert isinstance(failure_payload.get("error"), str)
        assert failure_payload["error"].startswith("服务器错误：")
        assert token not in failure_payload["error"]
        assert store.get_task_for_user(source.id, user_id).status == TaskStatus.TODO
        assert _user_write_snapshot(store, user_id) == before

        failed_events, failed_occurrences, failed_tasks, failed_ledger = _source_done_state(
            store, user_id, source.id, title
        )
        assert failed_events == []
        assert failed_occurrences == []
        assert len(failed_tasks) == 1
        assert failed_tasks[0]["id"] == source.id
        assert failed_tasks[0]["status"] == TaskStatus.TODO.value
        assert failed_tasks[0]["recurrence"] == "daily"
        assert failed_ledger == []
    finally:
        with store._connect() as conn:
            conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')

    retry_status, retry_body = _post_done(base_url, token, source.id, key)
    assert retry_status == HTTPStatus.OK
    events, occurrences, recurring_tasks, ledger = _source_done_state(
        store, user_id, source.id, title
    )
    assert store.get_task_for_user(source.id, user_id).status == TaskStatus.DONE
    assert len(events) == 1
    assert events[0]["payload"] == TaskStatus.DONE.value
    assert len(occurrences) == 1
    assert len(recurring_tasks) == 2
    source_row = next(row for row in recurring_tasks if row["id"] == source.id)
    next_task = next(row for row in recurring_tasks if row["id"] != source.id)
    assert source_row["status"] == TaskStatus.DONE.value
    assert source_row["recurrence"] == "daily"
    assert next_task["status"] == TaskStatus.TODO.value
    assert next_task["recurrence"] == "daily"
    assert len(ledger) == 1
    assert ledger[0]["idempotency_key"] == key

    expected_response = {
        "message": f"已创建下一期任务 #{next_task['id']}：{title}"
    }
    assert retry_body == _exact_json(expected_response)
    assert json.loads(retry_body) == expected_response
    occurrence = occurrences[0]
    assert occurrence["user_id"] == user_id
    assert occurrence["source_task_id"] == source.id
    assert occurrence["source_done_event_id"] == events[0]["id"]
    assert occurrence["next_task_id"] == next_task["id"]
    assert occurrence["response_status"] == retry_status == HTTPStatus.OK
    assert occurrence["response_json"].encode("utf-8") == retry_body
    assert ledger[0]["response_status"] == retry_status
    assert ledger[0]["response_json"].encode("utf-8") == retry_body

    after_retry = _user_write_snapshot(store, user_id)
    replay_status, replay_body = _post_done(base_url, token, source.id, key)
    assert replay_status == retry_status
    assert replay_body == retry_body
    assert _user_write_snapshot(store, user_id) == after_retry



def test_done_http_rolls_back_source_done_event_insert_failure_and_retries_same_key(
    done_http, tmp_path
):
    store, base_url, user_id, token = done_http
    title = f"HTTP event insert rollback daily {uuid.uuid4().hex}"
    source = _new_recurring_task(store, user_id, title)
    key = str(uuid.uuid4())
    assert uuid.UUID(key).version == 4
    assert source.status == TaskStatus.TODO
    assert source.recurrence == "daily"
    assert source.due_at is not None
    assert base_url.startswith("http://127.0.0.1:")
    database_path = store.db_path.resolve()
    assert database_path.parent == tmp_path.resolve()
    with store._connect() as conn:
        main_database = next(
            row
            for row in conn.execute("PRAGMA database_list").fetchall()
            if row["name"] == "main"
        )
    assert Path(main_database["file"]).resolve() == database_path

    before = _user_write_snapshot(store, user_id)
    initial_events, initial_occurrences, initial_tasks, initial_ledger = _source_done_state(
        store, user_id, source.id, title
    )
    assert initial_events == []
    assert initial_occurrences == []
    assert len(initial_tasks) == 1
    assert initial_tasks[0]["id"] == source.id
    assert initial_tasks[0]["status"] == TaskStatus.TODO.value
    assert initial_tasks[0]["recurrence"] == "daily"
    assert initial_ledger == []

    trigger_name = "test_fail_source_done_event_insert_for_request"
    try:
        with store._connect() as conn:
            conn.execute(
                f"""
                CREATE TRIGGER \"{trigger_name}\"
                BEFORE INSERT ON task_events
                WHEN NEW.task_id = {source.id}
                 AND NEW.event_type = 'status_changed'
                 AND NEW.payload = 'done'
                BEGIN
                    SELECT RAISE(ABORT, 'test-only source DONE event insert failure');
                END
                """
            )

        failed_status, failed_body = _post_done(base_url, token, source.id, key)
        assert failed_status == HTTPStatus.INTERNAL_SERVER_ERROR
        failure_payload = json.loads(failed_body)
        assert isinstance(failure_payload, dict)
        assert isinstance(failure_payload.get("error"), str)
        assert failure_payload["error"].startswith("服务器错误：")
        assert token not in failure_payload["error"]
        assert store.get_task_for_user(source.id, user_id).status == TaskStatus.TODO
        assert _user_write_snapshot(store, user_id) == before

        failed_events, failed_occurrences, failed_tasks, failed_ledger = _source_done_state(
            store, user_id, source.id, title
        )
        assert failed_events == []
        assert failed_occurrences == []
        assert len(failed_tasks) == 1
        assert failed_tasks[0]["id"] == source.id
        assert failed_tasks[0]["status"] == TaskStatus.TODO.value
        assert failed_tasks[0]["recurrence"] == "daily"
        assert failed_ledger == []
    finally:
        with store._connect() as conn:
            conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')

    retry_status, retry_body = _post_done(base_url, token, source.id, key)
    assert retry_status == HTTPStatus.OK
    events, occurrences, recurring_tasks, ledger = _source_done_state(
        store, user_id, source.id, title
    )
    assert store.get_task_for_user(source.id, user_id).status == TaskStatus.DONE
    assert len(events) == 1
    assert events[0]["payload"] == TaskStatus.DONE.value
    assert len(occurrences) == 1
    assert len(recurring_tasks) == 2
    source_row = next(row for row in recurring_tasks if row["id"] == source.id)
    next_task = next(row for row in recurring_tasks if row["id"] != source.id)
    assert source_row["status"] == TaskStatus.DONE.value
    assert source_row["recurrence"] == "daily"
    assert next_task["status"] == TaskStatus.TODO.value
    assert next_task["recurrence"] == "daily"
    assert len(ledger) == 1
    assert ledger[0]["idempotency_key"] == key

    expected_response = {
        "message": f"已创建下一期任务 #{next_task['id']}：{title}"
    }
    assert retry_body == _exact_json(expected_response)
    assert json.loads(retry_body) == expected_response
    occurrence = occurrences[0]
    assert occurrence["user_id"] == user_id
    assert occurrence["source_task_id"] == source.id
    assert occurrence["source_done_event_id"] == events[0]["id"]
    assert occurrence["next_task_id"] == next_task["id"]
    assert occurrence["response_status"] == retry_status == HTTPStatus.OK
    assert occurrence["response_json"].encode("utf-8") == retry_body
    assert ledger[0]["response_status"] == retry_status
    assert ledger[0]["response_json"].encode("utf-8") == retry_body
    assert occurrence["response_json"].encode("utf-8") == ledger[0][
        "response_json"
    ].encode("utf-8")

    after_retry = _user_write_snapshot(store, user_id)
    replay_status, replay_body = _post_done(base_url, token, source.id, key)
    assert replay_status == retry_status
    assert replay_body == retry_body
    assert _user_write_snapshot(store, user_id) == after_retry
