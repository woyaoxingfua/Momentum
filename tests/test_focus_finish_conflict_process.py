from __future__ import annotations

import json
import os
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src"

_CHILD_SERVER_BOOTSTRAP = r'''
import json
import os
import signal
import socket
import threading
from http.server import ThreadingHTTPServer

allowed_environment = {
    "PATH",
    "HOME",
    "LC_CTYPE",
    "PYTHONPATH",
    "PYTHONUTF8",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "MOMENTUM_DATABASE_URL",
    "MOMENTUM_TEST_FOCUS_GATE_HOST",
    "MOMENTUM_TEST_FOCUS_GATE_PORT",
}
if set(os.environ) != allowed_environment:
    raise RuntimeError("focus child environment is not the explicit test allowlist")
database_url = os.environ["MOMENTUM_DATABASE_URL"]
if not database_url.startswith("sqlite:////"):
    raise RuntimeError("focus child did not receive an absolute temporary SQLite URL")
from momentum_agent import config as config_module
def _dotenv_disabled(*args, **kwargs):
    raise RuntimeError("dotenv access is disabled in this test child")
config_module.load_dotenv = _dotenv_disabled
from momentum_agent.web.server import MomentumHandler

gate_host = os.environ["MOMENTUM_TEST_FOCUS_GATE_HOST"]
gate_port = int(os.environ["MOMENTUM_TEST_FOCUS_GATE_PORT"])
real_finish_dispatch = MomentumHandler.POST_EXACT_ROUTES["/api/focus/finish"]
gate_dispatch_lock = threading.Lock()
gate_dispatch_consumed = False

def _gated_finish_dispatch(handler, user_id):
    global gate_dispatch_consumed
    with gate_dispatch_lock:
        wait_at_gate = not gate_dispatch_consumed
        gate_dispatch_consumed = True
    if wait_at_gate:
        with socket.create_connection((gate_host, gate_port), timeout=20) as gate:
            gate.sendall(f"{os.getpid()}\n".encode("ascii"))
            gate.settimeout(20)
            release = bytearray()
            while len(release) < 2:
                chunk = gate.recv(2 - len(release))
                if not chunk:
                    raise RuntimeError("focus dispatch gate closed before release")
                release.extend(chunk)
            if bytes(release) != b"GO":
                raise RuntimeError("focus dispatch gate rejected the request")
    return real_finish_dispatch(handler, user_id)

class FocusFinishConflictHandler(MomentumHandler):
    database_url = database_url

post_routes = dict(MomentumHandler.POST_EXACT_ROUTES)
post_routes["/api/focus/finish"] = _gated_finish_dispatch
FocusFinishConflictHandler.POST_EXACT_ROUTES = post_routes
server = ThreadingHTTPServer(("127.0.0.1", 0), FocusFinishConflictHandler)
shutdown_thread = None

def _on_sigterm(_signum, _frame):
    global shutdown_thread
    if shutdown_thread is None or not shutdown_thread.is_alive():
        shutdown_thread = threading.Thread(
            target=server.shutdown,
            name="focus-test-server-shutdown",
            daemon=False,
        )
        shutdown_thread.start()

signal.signal(signal.SIGTERM, _on_sigterm)
print(
    json.dumps(
        {"pid": os.getpid(), "port": server.server_port, "database_url": database_url},
        separators=(",", ":"),
        sort_keys=True,
    ),
    flush=True,
)
try:
    server.serve_forever(poll_interval=0.05)
finally:
    server.server_close()
    if shutdown_thread is not None:
        shutdown_thread.join(timeout=5)
        if shutdown_thread.is_alive():
            raise RuntimeError("focus test shutdown thread did not finish")
'''


def _readiness_line(process: subprocess.Popen[str], timeout: float = 12.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.stdout is None:
            pytest.fail("focus child stdout pipe was not created", pytrace=False)
        readable, _, _ = select.select(
            [process.stdout], [], [], max(0.0, deadline - time.monotonic())
        )
        if not readable:
            if process.poll() is not None:
                pytest.fail("focus child exited before readiness", pytrace=False)
            continue
        line = process.stdout.readline()
        if not line:
            pytest.fail("focus child stdout closed before readiness", pytrace=False)
        return line
    pytest.fail("focus child did not emit readiness before timeout", pytrace=False)


def _child_environment(
    tmp_path: Path,
    database_url: str,
    gate_address: tuple[str, int],
) -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "LC_CTYPE": "C.UTF-8",
        "PYTHONPATH": str(SOURCE_ROOT),
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MOMENTUM_DATABASE_URL": database_url,
        "MOMENTUM_TEST_FOCUS_GATE_HOST": gate_address[0],
        "MOMENTUM_TEST_FOCUS_GATE_PORT": str(gate_address[1]),
    }


def _start_server_process(
    tmp_path: Path,
    bootstrap_path: Path,
    database_url: str,
    gate_address: tuple[str, int],
) -> tuple[subprocess.Popen[str], dict[str, object]]:
    process = subprocess.Popen(
        [sys.executable, "-u", str(bootstrap_path)],
        cwd=tmp_path,
        env=_child_environment(tmp_path, database_url, gate_address),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        try:
            readiness = json.loads(_readiness_line(process))
        except json.JSONDecodeError:
            pytest.fail("focus child readiness was not JSON", pytrace=False)
        assert set(readiness) == {"pid", "port", "database_url"}
        assert readiness["pid"] == process.pid
        assert readiness["database_url"] == database_url
        assert isinstance(readiness["port"], int)
        assert 0 < readiness["port"] < 65536
        return process, readiness
    except BaseException:
        _stop_server_process(process, None, graceful_timeout=3.0)
        raise
    finally:
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()


def _port_is_closed(port: int, timeout: float = 0.5) -> bool:
    try:
        connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return True
    connection.close()
    return False


def _stop_server_process(
    process: subprocess.Popen[str],
    port: int | None,
    *,
    graceful_timeout: float,
) -> tuple[int, bool, bool]:
    graceful = False
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            returncode = process.wait(timeout=graceful_timeout)
            graceful = True
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                returncode = process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=5.0)
    else:
        returncode = process.wait()
    reaped = process.poll() is not None and process.wait(timeout=0) == returncode
    port_closed = port is None or _port_is_closed(port)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()
    return returncode, graceful, reaped and port_closed


class _FocusFinishDispatchGate:
    """Test-only TCP barrier; both expected child PIDs arrive before either proceeds."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.expected_pids: set[int] | None = None
        self.pids: list[int] = []
        self.error: BaseException | None = None
        self.released = False
        self._finished = threading.Event()
        self._stop = threading.Event()
        self._connections: list[socket.socket] = []
        self._connections_lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(2)
        self._listener.settimeout(timeout)
        self.address = ("127.0.0.1", int(self._listener.getsockname()[1]))
        self._thread = threading.Thread(
            target=self._serve,
            name="test-focus-finish-conflict-dispatch-gate",
            daemon=False,
        )
        self._thread.start()

    def _serve(self) -> None:
        try:
            while len(self.pids) < 2:
                connection, _address = self._listener.accept()
                if self._stop.is_set():
                    connection.close()
                    return
                connection.settimeout(self.timeout)
                with self._connections_lock:
                    self._connections.append(connection)
                message = bytearray()
                while not message.endswith(b"\n"):
                    chunk = connection.recv(1)
                    if not chunk:
                        raise RuntimeError("child closed the focus gate connection early")
                    message.extend(chunk)
                    if len(message) > 32:
                        raise RuntimeError("child sent an invalid focus gate PID")
                self.pids.append(int(message[:-1].decode("ascii")))
            if self.expected_pids is None:
                raise RuntimeError("expected child PIDs were not configured before dispatch")
            if len(self.pids) != 2 or set(self.pids) != self.expected_pids:
                raise RuntimeError("gate did not receive both distinct expected PIDs")
            for connection in self._connections:
                connection.sendall(b"GO")
            self.released = True
        except BaseException as exc:
            if not self._stop.is_set():
                self.error = exc
                with self._connections_lock:
                    for connection in self._connections:
                        try:
                            connection.sendall(b"ER")
                        except OSError:
                            pass
        finally:
            with self._connections_lock:
                for connection in self._connections:
                    try:
                        connection.close()
                    except OSError:
                        pass
            self._finished.set()

    def assert_released(self) -> None:
        assert self._finished.wait(self.timeout + 1), (
            "focus gate did not receive both finish dispatches"
        )
        self._thread.join(timeout=1)
        assert not self._thread.is_alive(), "focus dispatch gate thread did not finish"
        assert self.error is None, "focus dispatch gate failed before release"
        assert self.released, "focus dispatch gate did not release both handlers"
        assert self.expected_pids is not None
        assert len(self.pids) == 2 and set(self.pids) == self.expected_pids
        assert len(set(self.pids)) == 2, "finish dispatches did not come from distinct PIDs"

    def close(self) -> None:
        self._stop.set()
        try:
            with socket.create_connection(self.address, timeout=0.2):
                pass
        except OSError:
            pass
        try:
            self._listener.close()
        except OSError:
            pass
        with self._connections_lock:
            for connection in self._connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    connection.close()
                except OSError:
                    pass
        self._thread.join(timeout=5)
        assert not self._thread.is_alive(), "focus dispatch gate thread did not stop"


def _http_request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, object] | None = None,
    timeout: float = 8.0,
) -> tuple[int, bytes]:
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    headers: dict[str, str] = {"Connection": "close"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as response:
        with response:
            return response.code, response.read()


def _snapshot_all_application_tables(database_path: Path):
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        table_names = tuple(
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        snapshots = {}
        for table_name in table_names:
            quoted_name = table_name.replace('"', '""')
            rows = connection.execute(
                f'SELECT * FROM "{quoted_name}" ORDER BY rowid'
            ).fetchall()
            snapshots[table_name] = tuple(tuple(row) for row in rows)
    counts = {table_name: len(rows) for table_name, rows in snapshots.items()}
    return counts, snapshots


def _focus_rows_for_session(database_path: Path, user_id: str, session_id: str):
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT e.id, e.task_id, e.payload, t.user_id "
            "FROM task_events AS e JOIN tasks AS t ON t.id = e.task_id "
            "WHERE e.event_type = 'focus_session' AND t.user_id = ?",
            (user_id,),
        ).fetchall()
    return [
        row for row in rows
        if json.loads(row["payload"]).get("session_id") == session_id
    ]


def test_cross_process_focus_finish_conflicting_payloads_have_one_winner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "focus-finish-conflict-process.sqlite3"
    database_url = f"sqlite:///{database_path}"
    assert database_path.parent.resolve() == tmp_path.resolve()
    assert database_url.startswith("sqlite:////")

    user_id = f"focus-conflict-{uuid.uuid4().hex}"
    password = f"temporary-password-{uuid.uuid4().hex}"
    session_id = uuid.uuid4().hex
    task_title = f"Focus process conflict {uuid.uuid4().hex}"
    started_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=90)
    ended_at = started_at + timedelta(seconds=90)
    common_payload: dict[str, object] = {
        "task_id": None,
        "session_id": session_id,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
        "planned_minutes": 25,
        "outcome": "stopped",
    }
    payload_73 = {**common_payload, "actual_seconds": 73}
    payload_74 = {**common_payload, "actual_seconds": 74}
    assert {key for key in payload_73 if payload_73[key] != payload_74[key]} == {
        "actual_seconds"
    }

    bootstrap_path = tmp_path / "focus_finish_conflict_child.py"
    bootstrap_path.write_text(textwrap.dedent(_CHILD_SERVER_BOOTSTRAP), encoding="utf-8")
    gate = _FocusFinishDispatchGate(timeout=20.0)
    children: list[tuple[subprocess.Popen[str], int]] = []
    database_schema_cache_key = str(database_path.resolve())
    database_url_cache_key = database_url
    try:
        first_process, first_ready = _start_server_process(
            tmp_path, bootstrap_path, database_url, gate.address
        )
        first_port = int(first_ready["port"])
        first_pid = int(first_ready["pid"])
        children.append((first_process, first_port))
        first_base_url = f"http://127.0.0.1:{first_port}"

        register_status, _register_body = _http_request(
            first_base_url,
            "POST",
            "/api/register",
            payload={"user_id": user_id, "display_name": user_id, "password": password},
        )
        assert register_status == HTTPStatus.OK
        login_status, login_body = _http_request(
            first_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        assert login_status == HTTPStatus.OK
        token = json.loads(login_body)["token"]
        assert token

        from momentum_agent.storage import SQLiteTaskStore

        task = SQLiteTaskStore(database_path).create_task(task_title, user_id=user_id)
        payload_73["task_id"] = task.id
        payload_74["task_id"] = task.id

        second_process, second_ready = _start_server_process(
            tmp_path, bootstrap_path, database_url, gate.address
        )
        second_port = int(second_ready["port"])
        second_pid = int(second_ready["pid"])
        children.append((second_process, second_port))
        assert first_pid != second_pid, "servers do not have independent OS PIDs"
        assert first_port != second_port, "servers do not listen on independent TCP ports"
        second_base_url = f"http://127.0.0.1:{second_port}"

        me_status, me_body = _http_request(second_base_url, "GET", "/api/me", token=token)
        assert me_status == HTTPStatus.OK
        assert json.loads(me_body) == {"user_id": user_id}

        before_counts, before_snapshot = _snapshot_all_application_tables(database_path)
        assert before_counts.get("sessions") == 1
        assert before_counts.get("tasks") == 1
        assert not _focus_rows_for_session(database_path, user_id, session_id)
        gate.expected_pids = {first_pid, second_pid}

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="focus-conflict-http") as clients:
            first_request = clients.submit(
                _http_request,
                first_base_url,
                "POST",
                "/api/focus/finish",
                token=token,
                payload=payload_73,
                timeout=30.0,
            )
            second_request = clients.submit(
                _http_request,
                second_base_url,
                "POST",
                "/api/focus/finish",
                token=token,
                payload=payload_74,
                timeout=30.0,
            )
            first_response = first_request.result(timeout=32.0)
            second_response = second_request.result(timeout=32.0)

        gate.assert_released()
        assert sorted((first_response[0], second_response[0])) == [
            HTTPStatus.OK,
            HTTPStatus.CONFLICT,
        ]
        response_by_payload = {
            "73": (payload_73, first_response, first_base_url),
            "74": (payload_74, second_response, second_base_url),
        }
        winner_seconds = 73 if first_response[0] == HTTPStatus.OK else 74
        winner_payload, winner_response, winner_base_url = response_by_payload[
            str(winner_seconds)
        ]
        loser_seconds = 74 if winner_seconds == 73 else 73
        loser_payload, loser_response, loser_base_url = response_by_payload[
            str(loser_seconds)
        ]

        assert json.loads(winner_response[1]) == {
            "message": "实际专注时长已保存",
            "task_id": task.id,
            "session_id": session_id,
            "started_at": started_at.astimezone(timezone.utc).isoformat(),
            "ended_at": ended_at.astimezone(timezone.utc).isoformat(),
            "planned_minutes": 25,
            "actual_seconds": winner_seconds,
            "outcome": "stopped",
        }
        assert json.loads(loser_response[1]) == {"error": "idempotency_conflict"}

        focus_rows = _focus_rows_for_session(database_path, user_id, session_id)
        assert len(focus_rows) == 1, "only the winning payload may write a focus event"
        persisted = focus_rows[0]
        assert persisted["task_id"] == task.id
        assert persisted["user_id"] == user_id
        event_payload = json.loads(persisted["payload"])
        assert event_payload["session_id"] == session_id
        assert event_payload["actual_seconds"] == winner_seconds
        assert event_payload["actual_seconds"] != loser_seconds

        focus_sessions = [
            session
            for session in SQLiteTaskStore(database_path).get_focus_sessions(user_id=user_id)
            if session["session_id"] == session_id
        ]
        assert len(focus_sessions) == 1
        assert focus_sessions[0]["actual_seconds"] == winner_seconds
        assert focus_sessions[0]["duration_minutes"] == pytest.approx(winner_seconds / 60)

        after_race_counts, after_race_snapshot = _snapshot_all_application_tables(
            database_path
        )
        assert set(after_race_snapshot) == set(before_snapshot)
        assert after_race_counts.get("task_events", 0) == before_counts.get(
            "task_events", 0
        ) + 1
        for table_name in set(before_snapshot) - {"task_events"}:
            assert after_race_snapshot[table_name] == before_snapshot[table_name]

        # Replay the exact winning request through the other server process.
        other_server_url = loser_base_url if winner_base_url == first_base_url else first_base_url
        before_winner_replay_counts, before_winner_replay_snapshot = (
            _snapshot_all_application_tables(database_path)
        )
        assert before_winner_replay_counts == after_race_counts
        assert before_winner_replay_snapshot == after_race_snapshot
        winner_replay = _http_request(
            other_server_url,
            "POST",
            "/api/focus/finish",
            token=token,
            payload=winner_payload,
        )
        assert winner_replay == winner_response
        after_winner_replay_counts, after_winner_replay_snapshot = (
            _snapshot_all_application_tables(database_path)
        )
        assert after_winner_replay_counts == before_winner_replay_counts
        assert after_winner_replay_snapshot == before_winner_replay_snapshot

        # Repeated exact retries of the losing request must remain conflicts and write nothing.
        for _attempt in range(2):
            before_loser_retry_counts, before_loser_retry_snapshot = (
                _snapshot_all_application_tables(database_path)
            )
            loser_retry = _http_request(
                loser_base_url,
                "POST",
                "/api/focus/finish",
                token=token,
                payload=loser_payload,
            )
            assert loser_retry[0] == HTTPStatus.CONFLICT
            assert json.loads(loser_retry[1]) == {"error": "idempotency_conflict"}
            after_loser_retry_counts, after_loser_retry_snapshot = (
                _snapshot_all_application_tables(database_path)
            )
            assert after_loser_retry_counts == before_loser_retry_counts
            assert after_loser_retry_snapshot == before_loser_retry_snapshot

        for process, port in reversed(children):
            returncode, graceful, clean = _stop_server_process(
                process, port, graceful_timeout=8.0
            )
            assert graceful, "focus HTTP child did not exit through graceful SIGTERM"
            assert returncode == 0, "focus HTTP child did not exit normally"
            assert clean, "focus HTTP child was not reaped or its TCP port stayed open"
    finally:
        gate.close()
        for process, port in reversed(children):
            _stop_server_process(process, port, graceful_timeout=5.0)
        from momentum_agent.web.server import _store_cache
        from momentum_agent.storage import SQLiteTaskStore

        _store_cache.pop(database_url_cache_key, None)
        SQLiteTaskStore._schema_initialized.discard(database_schema_cache_key)
