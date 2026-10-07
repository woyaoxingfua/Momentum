from __future__ import annotations

import http.client
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
from urllib.error import HTTPError, URLError
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
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

allowed_environment = {
    "PATH",
    "HOME",
    "LC_CTYPE",
    "PYTHONPATH",
    "PYTHONUTF8",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "MOMENTUM_DATABASE_URL",
    "MOMENTUM_TEST_FOCUS_RESPONSE_GATE_HOST",
    "MOMENTUM_TEST_FOCUS_RESPONSE_GATE_PORT",
}
if set(os.environ) - allowed_environment:
    raise RuntimeError("unexpected environment variable in isolated child")

database_url = os.environ["MOMENTUM_DATABASE_URL"]
if not database_url.startswith("sqlite:////"):
    raise RuntimeError("child did not receive the unique absolute SQLite URL")

from momentum_agent import config as config_module


def _dotenv_disabled(*args, **kwargs):
    raise RuntimeError("dotenv access is disabled in this test child")


config_module.load_dotenv = _dotenv_disabled

from momentum_agent.web.server import MomentumHandler

gate_host = os.environ.get("MOMENTUM_TEST_FOCUS_RESPONSE_GATE_HOST")
gate_port = os.environ.get("MOMENTUM_TEST_FOCUS_RESPONSE_GATE_PORT")
if (gate_host is None) != (gate_port is None):
    raise RuntimeError("incomplete focus response-gate configuration")
response_gate_lock = threading.Lock()
response_gate_consumed = False


class FocusFinishResponseLossHandler(MomentumHandler):
    database_url = database_url

    def send_json(self, payload, status=HTTPStatus.OK):
        global response_gate_consumed
        is_successful_focus_finish = (
            self.command == "POST"
            and self.path.split("?", 1)[0] == "/api/focus/finish"
            and int(status) == HTTPStatus.OK
        )
        if gate_host is not None and is_successful_focus_finish:
            with response_gate_lock:
                wait_at_response_gate = not response_gate_consumed
                response_gate_consumed = True
            if wait_at_response_gate:
                # Match MomentumHandler.send_json exactly, but do not write even
                # a response status line before the parent has captured these bytes.
                raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                header = json.dumps(
                    {
                        "pid": os.getpid(),
                        "status": int(status),
                        "body_length": len(raw_body),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("ascii") + b"\n"
                with socket.create_connection((gate_host, int(gate_port)), timeout=30) as gate:
                    gate.settimeout(30)
                    gate.sendall(header)
                    gate.sendall(raw_body)
                    release = gate.recv(1)
                    if release:
                        raise RuntimeError("focus response gate received unexpected release data")
                    raise RuntimeError("focus response gate closed before the child was killed")
        super().send_json(payload, status)

    def do_GET(self):
        if self.path == "/__test__/database":
            store = self.store
            with store._connect() as connection:
                main_database = next(
                    row
                    for row in connection.execute("PRAGMA database_list").fetchall()
                    if row["name"] == "main"
                )
            self.send_json(
                {
                    "pid": os.getpid(),
                    "handler_database_url": type(self).database_url,
                    "store_class": type(store).__name__,
                    "store_db_path": str(Path(store.db_path).resolve()),
                    "pragma_main_path": str(Path(main_database["file"]).resolve()),
                }
            )
            return
        super().do_GET()


server = ThreadingHTTPServer(("127.0.0.1", 0), FocusFinishResponseLossHandler)
shutdown_thread = None


def _on_sigterm(_signum, _frame):
    global shutdown_thread
    if shutdown_thread is None or not shutdown_thread.is_alive():
        shutdown_thread = threading.Thread(
            target=server.shutdown,
            name="test-focus-server-shutdown",
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


def _require(condition: bool, message: str) -> None:
    if not condition:
        pytest.fail(message, pytrace=False)


def _readiness_line(process: subprocess.Popen[str], timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.stdout is None:
            pytest.fail("focus child stdout pipe was not created", pytrace=False)
        readable, _, _ = select.select(
            [process.stdout], [], [], max(0.0, deadline - time.monotonic())
        )
        if not readable:
            if process.poll() is not None:
                pytest.fail("focus child exited before emitting readiness", pytrace=False)
            continue
        line = process.stdout.readline()
        if not line:
            pytest.fail("focus child stdout closed before readiness", pytrace=False)
        return line
    pytest.fail("focus child did not emit readiness before timeout", pytrace=False)


def _start_server_process(
    tmp_path: Path,
    bootstrap_path: Path,
    database_url: str,
    response_gate_address: tuple[str, int] | None = None,
) -> tuple[subprocess.Popen[str], dict[str, object]]:
    child_env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "LC_CTYPE": "C.UTF-8",
        "PYTHONPATH": str(SOURCE_ROOT),
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MOMENTUM_DATABASE_URL": database_url,
    }
    if response_gate_address is not None:
        child_env["MOMENTUM_TEST_FOCUS_RESPONSE_GATE_HOST"] = response_gate_address[0]
        child_env["MOMENTUM_TEST_FOCUS_RESPONSE_GATE_PORT"] = str(response_gate_address[1])
    bootstrap_path.write_text(textwrap.dedent(_CHILD_SERVER_BOOTSTRAP), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-u", str(bootstrap_path)],
        cwd=tmp_path,
        env=child_env,
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
        _require(
            set(readiness) == {"pid", "port", "database_url"},
            "focus child readiness had an unexpected shape",
        )
        _require(readiness["pid"] == process.pid, "focus child readiness PID mismatch")
        _require(readiness["database_url"] == database_url, "focus child used a different database URL")
        _require(isinstance(readiness["port"], int), "focus child port was not an integer")
        _require(0 < readiness["port"] < 65536, "focus child did not bind an ephemeral port")
        return process, readiness
    except BaseException:
        _stop_server_process(process, None, graceful_timeout=2.0)
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


def _recv_line(connection: socket.socket, max_bytes: int = 512) -> bytes:
    line = bytearray()
    while not line.endswith(b"\n"):
        chunk = connection.recv(1)
        if not chunk:
            raise RuntimeError("focus response-gate client closed before its header")
        line.extend(chunk)
        if len(line) > max_bytes:
            raise RuntimeError("focus response-gate header was too large")
    return bytes(line[:-1])


def _recv_exact(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise RuntimeError("focus response-gate client truncated the raw body")
        result.extend(chunk)
    return bytes(result)


class _PostCommitFocusResponseGate:
    """Capture status/raw JSON bytes, then hold the first server before HTTP headers."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.pid: int | None = None
        self.status: int | None = None
        self.raw_body: bytes | None = None
        self.error: BaseException | None = None
        self._hit = threading.Event()
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(timeout)
        self.address = ("127.0.0.1", int(self._listener.getsockname()[1]))
        self._connection: socket.socket | None = None
        self._connection_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._serve,
            name="test-focus-post-commit-response-gate",
            daemon=False,
        )
        self._thread.start()

    def _serve(self) -> None:
        try:
            connection, _address = self._listener.accept()
            with self._connection_lock:
                self._connection = connection
            with connection:
                connection.settimeout(self.timeout)
                if self._stop.is_set():
                    return
                header = json.loads(_recv_line(connection).decode("ascii"))
                if set(header) != {"pid", "status", "body_length"}:
                    raise RuntimeError("focus response-gate header had an unexpected shape")
                body_length = header["body_length"]
                if type(body_length) is not int or not 0 <= body_length <= 1024 * 1024:
                    raise RuntimeError("focus response-gate body length was invalid")
                self.pid = int(header["pid"])
                self.status = int(header["status"])
                self.raw_body = _recv_exact(connection, body_length)
                self._hit.set()
                # The child waits for the parent; SIGKILL closes this socket and ends this read.
                release = connection.recv(1)
                if release:
                    raise RuntimeError("parent unexpectedly released the response gate")
        except BaseException as exc:
            if not self._stop.is_set() and not self._hit.is_set():
                self.error = exc
        finally:
            if self.error is not None:
                self._hit.set()

    def assert_capture(self, expected_pid: int) -> tuple[int, bytes]:
        _require(self._hit.wait(self.timeout + 1), "post-commit focus response gate was not reached")
        _require(self.error is None, "post-commit focus response gate failed before capture")
        _require(self.pid == expected_pid, "focus response gate was reached by an unexpected PID")
        _require(self.status == HTTPStatus.OK, "captured focus finish response was not HTTP 200")
        _require(self.raw_body is not None, "focus response gate did not capture raw response bytes")
        _require(self._thread.is_alive(), "focus response gate stopped before the parent killed the child")
        return self.status, self.raw_body

    def close(self) -> None:
        self._stop.set()
        try:
            with socket.create_connection(self.address, timeout=0.2):
                pass
        except OSError:
            pass
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        try:
            self._listener.close()
        finally:
            self._thread.join(timeout=5)
        _require(not self._thread.is_alive(), "focus response-gate thread did not stop")


def _http_request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, object] | None = None,
    timeout: float = 8.0,
) -> tuple[int, bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
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


def _database_diagnostic(
    base_url: str,
    readiness: dict[str, object],
    database_url: str,
    database_path: Path,
) -> None:
    status, raw_body = _http_request(base_url, "GET", "/__test__/database")
    _require(status == HTTPStatus.OK, "focus child database diagnostic endpoint failed")
    diagnostic = json.loads(raw_body)
    expected_path = str(database_path.resolve())
    _require(diagnostic.get("pid") == readiness["pid"], "focus handler ran in an unexpected PID")
    _require(diagnostic.get("handler_database_url") == database_url, "focus handler database URL changed")
    _require(diagnostic.get("store_class") == "SQLiteTaskStore", "focus handler did not use SQLite")
    _require(diagnostic.get("store_db_path") == expected_path, "focus handler opened a different SQLite file")
    _require(diagnostic.get("pragma_main_path") == expected_path, "focus SQLite connection opened a different file")


def _snapshot_all_application_tables(database_path: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        table_names = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        snapshots: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table_name in table_names:
            quoted_name = table_name.replace('"', '""')
            rows = connection.execute(f'SELECT * FROM "{quoted_name}" ORDER BY rowid').fetchall()
            snapshots[table_name] = tuple(tuple(row) for row in rows)
    return snapshots


def _focus_session_rows(
    database_path: Path,
    user_id: str,
    task_id: int,
    session_id: str,
) -> list[tuple[int, dict[str, object]]]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT e.task_id, e.payload
            FROM task_events AS e
            JOIN tasks AS t ON t.id = e.task_id
            WHERE t.user_id = ? AND e.event_type = 'focus_session'
            ORDER BY e.rowid
            """,
            (user_id,),
        ).fetchall()
    matching = []
    for stored_task_id, payload_json in rows:
        payload = json.loads(payload_json or "{}")
        if payload.get("session_id") == session_id:
            matching.append((int(stored_task_id), payload))
    return matching


def test_focus_finish_recovers_after_committed_response_is_lost(tmp_path: Path) -> None:
    database_path = tmp_path / "focus-finish-response-loss.sqlite3"
    database_url = f"sqlite:///{database_path}"
    _require(database_url.startswith("sqlite:////"), "focus database URL was not absolute")
    _require(database_path.parent.resolve() == tmp_path.resolve(), "focus database escaped tmp_path")

    unique = uuid.uuid4().hex
    user_id = f"focus-loss-{unique}"
    password = f"focus-loss-password-{uuid.uuid4().hex}"
    task_title = f"focus-response-loss-{uuid.uuid4().hex}"
    session_id = uuid.uuid4().hex
    _require(uuid.UUID(session_id).version == 4, "focus session_id was not UUIDv4")

    ended_at = datetime.now(timezone.utc).replace(microsecond=0)
    started_at = ended_at - timedelta(seconds=73)
    payload: dict[str, object] = {
        "task_id": 0,
        "session_id": session_id,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
        "planned_minutes": 25,
        "actual_seconds": 73,
        "outcome": "stopped",
    }

    bootstrap_path = tmp_path / "focus_response_loss_child.py"
    response_gate = _PostCommitFocusResponseGate(timeout=30.0)
    children: list[tuple[subprocess.Popen[str], int | None]] = []
    client_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="focus-response-loss-client")
    first_client_request = None
    try:
        first_process, first_ready = _start_server_process(
            tmp_path,
            bootstrap_path,
            database_url,
            response_gate_address=response_gate.address,
        )
        first_port = int(first_ready["port"])
        children.append((first_process, first_port))
        first_pid = int(first_ready["pid"])
        first_base_url = f"http://127.0.0.1:{first_port}"
        _database_diagnostic(first_base_url, first_ready, database_url, database_path)

        register_status, _register_body = _http_request(
            first_base_url,
            "POST",
            "/api/register",
            payload={"user_id": user_id, "display_name": user_id, "password": password},
        )
        _require(register_status == HTTPStatus.OK, "real HTTP focus-user registration failed")
        login_status, login_body = _http_request(
            first_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        _require(login_status == HTTPStatus.OK, "real HTTP first focus-user login failed")
        first_token = json.loads(login_body)["token"]
        _require(bool(first_token), "first server did not issue a bearer session")

        create_status, create_body = _http_request(
            first_base_url,
            "POST",
            "/api/tasks",
            token=first_token,
            payload={"text": task_title},
        )
        _require(create_status == HTTPStatus.OK, "real HTTP focus task creation failed")
        task_list = json.loads(create_body)["tasks"]
        matching_tasks = [task for task in task_list if task.get("title") == task_title]
        _require(len(matching_tasks) == 1, "focus task was not created exactly once")
        task_id = int(matching_tasks[0]["id"])
        payload["task_id"] = task_id

        before_finish = _snapshot_all_application_tables(database_path)
        required_tables = {"users", "sessions", "tasks", "task_events"}
        _require(required_tables.issubset(before_finish), "pre-finish snapshot omitted required application tables")
        _require(
            len(before_finish["sessions"]) == 1,
            "first HTTP login did not create exactly one session row",
        )
        _require(
            not _focus_session_rows(database_path, user_id, task_id, session_id),
            "fresh focus session_id already existed before the first POST",
        )

        def make_first_request() -> tuple[object, ...]:
            try:
                result = _http_request(
                    first_base_url,
                    "POST",
                    "/api/focus/finish",
                    token=first_token,
                    payload=payload,
                    timeout=40.0,
                )
            except (URLError, OSError, http.client.HTTPException) as exc:
                return ("connection_failure", type(exc).__name__)
            return ("http_response", result[0], result[1])

        first_client_request = client_executor.submit(make_first_request)
        captured_status, captured_body = response_gate.assert_capture(first_pid)
        _require(first_process.poll() is None, "first focus server exited before parent confirmation")

        first_process.send_signal(signal.SIGKILL)
        first_returncode = first_process.wait(timeout=5.0)
        _require(
            first_returncode == -signal.SIGKILL,
            "first focus server was not terminated by SIGKILL",
        )
        _first_code, _first_graceful, first_clean = _stop_server_process(
            first_process, first_port, graceful_timeout=0.1
        )
        _require(first_clean, "SIGKILLed focus child was not reaped or its HTTP port remained open")
        response_gate.close()

        client_outcome = first_client_request.result(timeout=10.0)
        _require(
            client_outcome[0] == "connection_failure",
            "first focus client received an HTTP response instead of only a connection failure",
        )

        after_first_commit = _snapshot_all_application_tables(database_path)
        _require(
            set(after_first_commit) == set(before_finish),
            "application table set changed during the first focus finish transaction",
        )
        first_focus_rows = _focus_session_rows(database_path, user_id, task_id, session_id)
        _require(len(first_focus_rows) == 1, "first transaction did not persist exactly one matching focus_session event")
        stored_task_id, stored_payload = first_focus_rows[0]
        _require(stored_task_id == task_id, "persisted focus event references a different task")
        _require(
            stored_payload.get("actual_seconds") == payload["actual_seconds"] == 73,
            "first committed focus event did not persist the expected actual_seconds",
        )
        _require(
            stored_payload.get("planned_minutes") == payload["planned_minutes"]
            and stored_payload.get("outcome") == payload["outcome"],
            "first committed focus event did not persist the request payload",
        )
        before_event_count = sum(
            1 for row in before_finish["task_events"] if row[2] == "focus_session"
        )
        committed_event_count = sum(
            1 for row in after_first_commit["task_events"] if row[2] == "focus_session"
        )
        _require(
            before_event_count == 0 and committed_event_count == 1,
            "first transaction did not add exactly one focus_session event",
        )
        _require(
            captured_status == HTTPStatus.OK,
            "captured pre-response focus status was not HTTP 200",
        )
        _require(
            json.loads(captured_body.decode("utf-8"))["actual_seconds"] == 73,
            "captured successful finish response omitted the expected actual_seconds",
        )

        second_process, second_ready = _start_server_process(
            tmp_path, bootstrap_path, database_url
        )
        second_port = int(second_ready["port"])
        children.append((second_process, second_port))
        second_pid = int(second_ready["pid"])
        _require(first_pid != second_pid, "focus replay reused the first OS process PID")
        second_base_url = f"http://127.0.0.1:{second_port}"
        _database_diagnostic(second_base_url, second_ready, database_url, database_path)

        second_login_status, second_login_body = _http_request(
            second_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        _require(second_login_status == HTTPStatus.OK, "real HTTP re-login failed in second focus server")
        second_token = json.loads(second_login_body)["token"]
        _require(bool(second_token), "second focus server did not issue a bearer session")
        _require(second_token != first_token, "second login did not issue a distinct bearer token")
        me_status, me_body = _http_request(
            second_base_url, "GET", "/api/me", token=second_token
        )
        _require(
            me_status == HTTPStatus.OK and json.loads(me_body) == {"user_id": user_id},
            "second-process session did not authenticate as the original focus user",
        )

        after_second_login = _snapshot_all_application_tables(database_path)
        _require(
            set(after_second_login) == set(after_first_commit),
            "application table set changed after second-process login",
        )
        for table_name in after_first_commit:
            if table_name != "sessions":
                _require(
                    after_second_login[table_name] == after_first_commit[table_name],
                    f"second-process login changed non-session application rows in {table_name}",
                )
        _require(
            len(after_second_login["sessions"]) == len(after_first_commit["sessions"]) + 1,
            "second login did not add exactly one session row",
        )
        _require(
            after_second_login["sessions"][:-1] == after_first_commit["sessions"],
            "second login changed or removed a pre-existing session row",
        )
        with sqlite3.connect(database_path) as connection:
            new_session_user = connection.execute(
                "SELECT user_id FROM sessions WHERE token = ?", (second_token,)
            ).fetchone()
        _require(
            new_session_user is not None and new_session_user[0] == user_id,
            "second login did not add its unique session row for the original user",
        )

        replay_status, replay_body = _http_request(
            second_base_url,
            "POST",
            "/api/focus/finish",
            token=second_token,
            payload=payload,
        )
        _require(replay_status == captured_status, "focus replay status differed from the captured response status")
        _require(replay_body == captured_body, "focus replay raw body differed byte-for-byte from the captured response")

        after_replay = _snapshot_all_application_tables(database_path)
        _require(
            after_replay == after_second_login,
            "focus replay added or changed application rows, including event/time/session rows",
        )
        replay_focus_rows = _focus_session_rows(database_path, user_id, task_id, session_id)
        _require(replay_focus_rows == first_focus_rows, "focus replay increased or changed the persisted event/time")
        _require(
            sum(int(event.get("actual_seconds", 0)) for _event_task_id, event in replay_focus_rows) == 73,
            "focus replay changed the total actual_seconds for the session",
        )

        second_returncode, second_graceful, second_clean = _stop_server_process(
            second_process, second_port, graceful_timeout=10.0
        )
        _require(second_graceful, "second focus process did not exit through SIGTERM shutdown")
        _require(second_returncode == 0, "second focus process did not exit normally")
        _require(second_clean, "second focus child was not reaped or its HTTP port remained open")
    finally:
        try:
            for process, port in reversed(children):
                _stop_server_process(process, port, graceful_timeout=5.0)
        finally:
            try:
                response_gate.close()
            finally:
                client_executor.shutdown(wait=True, cancel_futures=True)
