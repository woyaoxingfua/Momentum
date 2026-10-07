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
import threading
import textwrap
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    "MOMENTUM_TEST_DONE_GATE_HOST",
    "MOMENTUM_TEST_DONE_GATE_PORT",
    "MOMENTUM_TEST_DONE_RESPONSE_GATE_HOST",
    "MOMENTUM_TEST_DONE_RESPONSE_GATE_PORT",
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

gate_host = os.environ.get("MOMENTUM_TEST_DONE_GATE_HOST")
gate_port = os.environ.get("MOMENTUM_TEST_DONE_GATE_PORT")
if (gate_host is None) != (gate_port is None):
    raise RuntimeError("incomplete test-only done dispatch gate configuration")
response_gate_host = os.environ.get("MOMENTUM_TEST_DONE_RESPONSE_GATE_HOST")
response_gate_port = os.environ.get("MOMENTUM_TEST_DONE_RESPONSE_GATE_PORT")
if (response_gate_host is None) != (response_gate_port is None):
    raise RuntimeError("incomplete test-only done response gate configuration")
response_gate_lock = threading.Lock()
response_gate_consumed = False


class ProcessRestartTestHandler(MomentumHandler):
    database_url = database_url

    def send_json(self, payload, status=HTTPStatus.OK):
        global response_gate_consumed
        path_parts = self.path.split("?", 1)[0].strip("/").split("/")
        is_successful_done_response = (
            self.command == "POST"
            and len(path_parts) == 4
            and path_parts[0] == "api"
            and path_parts[1] == "tasks"
            and path_parts[2].isdigit()
            and path_parts[3] == "done"
            and status == 200
        )
        if response_gate_host is not None and is_successful_done_response:
            with response_gate_lock:
                wait_at_response_gate = not response_gate_consumed
                response_gate_consumed = True
            if wait_at_response_gate:
                with socket.create_connection(
                    (response_gate_host, int(response_gate_port)), timeout=25
                ) as gate:
                    gate.sendall(f"{os.getpid()}\n".encode("ascii"))
                    gate.settimeout(25)
                    release = gate.recv(1)
                    if release:
                        raise RuntimeError("test-only done response gate received unexpected data")
                    raise RuntimeError("test-only done response gate closed before the process was stopped")
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
                    "handler_class_database_url": type(self).database_url,
                    "handler_instance_database_url": self.database_url,
                    "store_class": type(store).__name__,
                    "store_db_path": str(Path(store.db_path).resolve()),
                    "pragma_main_path": str(Path(main_database["file"]).resolve()),
                }
            )
            return
        super().do_GET()


if gate_host is not None:
    real_done_dispatch = MomentumHandler.POST_PREFIX_ROUTES["done"]
    gate_dispatch_lock = threading.Lock()
    gate_dispatch_consumed = False

    def _wait_for_competing_done_dispatch(handler, path, user_id):
        global gate_dispatch_consumed
        with gate_dispatch_lock:
            wait_for_gate = not gate_dispatch_consumed
            gate_dispatch_consumed = True
        if wait_for_gate:
            with socket.create_connection((gate_host, int(gate_port)), timeout=25) as gate:
                gate.sendall(f"{os.getpid()}\n".encode("ascii"))
                gate.settimeout(25)
                release = bytearray()
                while len(release) < 2:
                    chunk = gate.recv(2 - len(release))
                    if not chunk:
                        raise RuntimeError("test-only done dispatch gate closed before release")
                    release.extend(chunk)
                if bytes(release) != b"GO":
                    raise RuntimeError("test-only done dispatch gate rejected the request")
        return real_done_dispatch(handler, path, user_id)

    gated_routes = dict(MomentumHandler.POST_PREFIX_ROUTES)
    gated_routes["done"] = _wait_for_competing_done_dispatch
    ProcessRestartTestHandler.POST_PREFIX_ROUTES = gated_routes


server = ThreadingHTTPServer(("127.0.0.1", 0), ProcessRestartTestHandler)
shutdown_thread = None


def _on_sigterm(_signum, _frame):
    global shutdown_thread
    if shutdown_thread is None or not shutdown_thread.is_alive():
        shutdown_thread = threading.Thread(
            target=server.shutdown,
            name="test-server-shutdown",
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
            raise RuntimeError("test shutdown thread did not finish")
'''


def _require(condition: bool, message: str) -> None:
    """Fail without rendering compared values that may contain credentials."""
    if not condition:
        pytest.fail(message, pytrace=False)


def _child_environment(
    tmp_path: Path,
    database_url: str,
    gate_address: tuple[str, int] | None = None,
    response_gate_address: tuple[str, int] | None = None,
) -> dict[str, str]:
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
    if gate_address is not None:
        child_env["MOMENTUM_TEST_DONE_GATE_HOST"] = gate_address[0]
        child_env["MOMENTUM_TEST_DONE_GATE_PORT"] = str(gate_address[1])
    if response_gate_address is not None:
        child_env["MOMENTUM_TEST_DONE_RESPONSE_GATE_HOST"] = response_gate_address[0]
        child_env["MOMENTUM_TEST_DONE_RESPONSE_GATE_PORT"] = str(response_gate_address[1])
    return child_env


def _readiness_line(process: subprocess.Popen[str], timeout: float = 12.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.stdout is None:
            pytest.fail("child stdout pipe was not created", pytrace=False)
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        if not readable:
            if process.poll() is not None:
                pytest.fail("child exited before emitting readiness JSON", pytrace=False)
            continue
        line = process.stdout.readline()
        if not line:
            pytest.fail("child stdout closed before emitting readiness JSON", pytrace=False)
        return line
    pytest.fail("child did not emit readiness JSON before timeout", pytrace=False)


def _start_server_process(
    tmp_path: Path,
    bootstrap_path: Path,
    database_url: str,
    gate_address: tuple[str, int] | None = None,
    response_gate_address: tuple[str, int] | None = None,
) -> tuple[subprocess.Popen[str], dict[str, object]]:
    bootstrap_path.write_text(textwrap.dedent(_CHILD_SERVER_BOOTSTRAP), encoding="utf-8")
    child_env = _child_environment(
        tmp_path, database_url, gate_address, response_gate_address
    )
    allowed_keys = {
        "PATH",
        "HOME",
        "LC_CTYPE",
        "PYTHONPATH",
        "PYTHONUTF8",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "MOMENTUM_DATABASE_URL",
    }
    if gate_address is not None:
        allowed_keys.update(
            {"MOMENTUM_TEST_DONE_GATE_HOST", "MOMENTUM_TEST_DONE_GATE_PORT"}
        )
    if response_gate_address is not None:
        allowed_keys.update(
            {
                "MOMENTUM_TEST_DONE_RESPONSE_GATE_HOST",
                "MOMENTUM_TEST_DONE_RESPONSE_GATE_PORT",
            }
        )
    _require(set(child_env) == allowed_keys, "child environment is not an explicit allowlist")
    _require(child_env["MOMENTUM_DATABASE_URL"] == database_url, "child database URL changed")
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
        line = _readiness_line(process)
        try:
            readiness = json.loads(line)
        except json.JSONDecodeError:
            pytest.fail("child readiness line was not JSON", pytrace=False)
        _require(
            set(readiness) == {"pid", "port", "database_url"},
            "child readiness JSON has an unexpected shape",
        )
        _require(readiness["pid"] == process.pid, "readiness PID does not match the OS child PID")
        _require(readiness["database_url"] == database_url, "child bound a different database URL")
        _require(isinstance(readiness["port"], int), "child readiness port is not an integer")
        _require(0 < readiness["port"] < 65536, "child did not bind an ephemeral TCP port")
        return process, readiness
    except BaseException:
        _finish_child_process(process, None, graceful_timeout=5.0)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()


def _port_is_closed(port: int, timeout: float = 0.5) -> bool:
    try:
        connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return True
    else:
        connection.close()
        return False


def _finish_child_process(
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


class _DoneDispatchGate:
    """Test-only TCP barrier that releases two child-process /done dispatches together."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.expected_pids: set[int] | None = None
        self.pids: list[int] = []
        self.error: BaseException | None = None
        self.released = False
        self._finished = threading.Event()
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(2)
        self._listener.settimeout(timeout)
        self.address = ("127.0.0.1", int(self._listener.getsockname()[1]))
        self._thread = threading.Thread(
            target=self._serve,
            name="test-done-dispatch-gate",
            daemon=False,
        )
        self._thread.start()

    def _serve(self) -> None:
        connections: list[socket.socket] = []
        try:
            while len(connections) < 2:
                connection, _ = self._listener.accept()
                if self._stop.is_set():
                    connection.close()
                    return
                connection.settimeout(self.timeout)
                connections.append(connection)
                message = bytearray()
                while not message.endswith(b"\n"):
                    chunk = connection.recv(1)
                    if not chunk:
                        raise RuntimeError("child closed its dispatch-gate connection early")
                    message.extend(chunk)
                    if len(message) > 32:
                        raise RuntimeError("child sent an invalid dispatch-gate PID")
                self.pids.append(int(message[:-1].decode("ascii")))

            if self.expected_pids is None:
                raise RuntimeError("expected child PIDs were not configured before dispatch")
            if set(self.pids) != self.expected_pids or len(self.pids) != 2:
                raise RuntimeError("dispatch gate did not receive both expected child PIDs")
            for connection in connections:
                connection.sendall(b"GO")
            self.released = True
        except BaseException as exc:
            self.error = exc
            for connection in connections:
                try:
                    connection.sendall(b"ER")
                except OSError:
                    pass
        finally:
            for connection in connections:
                try:
                    connection.close()
                except OSError:
                    pass
            self._finished.set()

    def assert_released(self) -> None:
        _require(
            self._finished.wait(self.timeout + 1),
            "dispatch gate did not finish receiving both /done handlers",
        )
        self._thread.join(timeout=1)
        _require(not self._thread.is_alive(), "dispatch gate thread did not finish")
        _require(self.error is None, "dispatch gate failed before releasing both handlers")
        _require(self.released, "dispatch gate did not release both handlers")
        _require(
            self.expected_pids is not None
            and len(self.pids) == 2
            and set(self.pids) == self.expected_pids,
            "dispatch gate did not verify both expected, distinct OS processes",
        )

    def close(self) -> None:
        self._stop.set()
        try:
            with socket.create_connection(self.address, timeout=0.2):
                pass
        except OSError:
            pass
        try:
            self._listener.close()
        finally:
            self._thread.join(timeout=5)


class _PostCommitResponseGate:
    """Hold a successful /done response before HTTP headers until the parent kills its process."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.pid: int | None = None
        self.error: BaseException | None = None
        self._hit = threading.Event()
        self._release = threading.Event()
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(timeout)
        self.address = ("127.0.0.1", int(self._listener.getsockname()[1]))
        self._thread = threading.Thread(
            target=self._serve,
            name="test-done-post-commit-response-gate",
            daemon=False,
        )
        self._thread.start()

    def _serve(self) -> None:
        try:
            connection, _address = self._listener.accept()
            with connection:
                if self._stop.is_set():
                    return
                connection.settimeout(self.timeout)
                message = bytearray()
                while not message.endswith(b"\n"):
                    chunk = connection.recv(1)
                    if not chunk:
                        raise RuntimeError("child closed the response-gate connection early")
                    message.extend(chunk)
                    if len(message) > 32:
                        raise RuntimeError("child sent an invalid response-gate PID")
                self.pid = int(message[:-1].decode("ascii"))
                self._hit.set()
                if not self._release.wait(self.timeout):
                    raise TimeoutError("parent did not stop the child at the response gate")
        except BaseException as exc:
            if not self._stop.is_set():
                self.error = exc
        finally:
            if self.error is not None:
                self._hit.set()

    def assert_hit(self, expected_pid: int) -> None:
        _require(self._hit.wait(self.timeout + 1), "post-commit response gate was not reached")
        _require(self.error is None, "post-commit response gate failed before confirming the child")
        _require(self.pid == expected_pid, "response gate was reached by an unexpected OS process")
        _require(self._thread.is_alive(), "child was not blocked at the pre-response gate")

    def close(self) -> None:
        self._stop.set()
        self._release.set()
        try:
            with socket.create_connection(self.address, timeout=0.2):
                pass
        except OSError:
            pass
        try:
            self._listener.close()
        finally:
            self._thread.join(timeout=5)
        _require(not self._thread.is_alive(), "post-commit response gate thread did not stop")


def _http_request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    timeout: float = 8.0,
) -> tuple[int, bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as response:
        return response.code, response.read()


def _database_diagnostic(
    base_url: str,
    readiness: dict[str, object],
    database_url: str,
    database_path: Path,
) -> None:
    status, raw_body = _http_request(base_url, "GET", "/__test__/database")
    _require(status == HTTPStatus.OK, "child database diagnostic endpoint failed")
    diagnostic = json.loads(raw_body)
    expected_path = str(database_path.resolve())
    _require(diagnostic.get("pid") == readiness["pid"], "handler ran in an unexpected OS process")
    _require(
        diagnostic.get("handler_class_database_url") == database_url,
        "handler class is configured for a different database URL",
    )
    _require(
        diagnostic.get("handler_instance_database_url") == database_url,
        "handler instance is configured for a different database URL",
    )
    _require(diagnostic.get("store_class") == "SQLiteTaskStore", "handler did not open a SQLite store")
    _require(diagnostic.get("store_db_path") == expected_path, "handler store opened a different SQLite file")
    _require(diagnostic.get("pragma_main_path") == expected_path, "SQLite PRAGMA points to a different file")


def _snapshot_all_application_tables(database_path: Path):
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
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
            rows = connection.execute(f'SELECT * FROM "{quoted_name}" ORDER BY rowid').fetchall()
            snapshots[table_name] = tuple(tuple(row) for row in rows)
    counts = {table_name: len(rows) for table_name, rows in snapshots.items()}
    return counts, snapshots


def _assert_first_done_state(
    database_path: Path,
    user_id: str,
    task_id: int,
    idempotency_key: str,
    response_status: int,
    response_body: bytes,
) -> None:
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        source = connection.execute(
            "SELECT id, title, status, recurrence, user_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        done_events = connection.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id = ? AND event_type = ? AND payload = ? ORDER BY id",
            (task_id, "status_changed", "done"),
        ).fetchall()
        occurrences = connection.execute(
            "SELECT source_done_event_id, next_task_id, response_status, response_json "
            "FROM task_done_occurrences WHERE user_id = ? AND source_task_id = ?",
            (user_id, task_id),
        ).fetchall()
        ledger = connection.execute(
            "SELECT request_fingerprint, response_status, response_json "
            "FROM task_done_idempotency WHERE user_id = ? AND idempotency_key = ?",
            (user_id, idempotency_key),
        ).fetchall()
        next_task = None
        if len(occurrences) == 1 and occurrences[0]["next_task_id"] is not None:
            next_task = connection.execute(
                "SELECT id, title, status, recurrence, user_id FROM tasks WHERE id = ?",
                (occurrences[0]["next_task_id"],),
            ).fetchone()
    _require(source is not None, "first completion source task is missing")
    _require(
        source["status"] == "done" and source["recurrence"] == "daily" and source["user_id"] == user_id,
        "first completion did not persist the recurring source task state",
    )
    _require(len(done_events) == 1, "first completion did not persist exactly one done event")
    _require(len(occurrences) == 1, "first completion did not persist exactly one occurrence mapping")
    _require(next_task is not None, "first completion did not persist the next recurring task")
    _require(
        next_task["title"] == source["title"]
        and next_task["status"] == "todo"
        and next_task["recurrence"] == "daily"
        and next_task["user_id"] == user_id,
        "next recurring task state does not match its source",
    )
    _require(
        occurrences[0]["source_done_event_id"] == done_events[0]["id"]
        and occurrences[0]["next_task_id"] == next_task["id"],
        "occurrence mapping does not reference the done event and next task",
    )
    _require(len(ledger) == 1, "first completion did not persist exactly one done ledger row")
    _require(
        occurrences[0]["response_status"] == response_status
        and ledger[0]["response_status"] == response_status,
        "done response status was not persisted in occurrence and ledger",
    )
    response_payload = json.loads(response_body)
    _require(
        json.loads(occurrences[0]["response_json"]) == response_payload
        and json.loads(ledger[0]["response_json"]) == response_payload,
        "stored occurrence or ledger response does not match the HTTP response",
    )


def test_done_http_replay_survives_independent_python_process_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "done-process-restart.sqlite3"
    database_url = f"sqlite:///{database_path}"
    _require(database_url.startswith("sqlite:////"), "database URL is not an absolute temporary SQLite URL")
    _require(database_path.parent.resolve() == tmp_path.resolve(), "database is not isolated under tmp_path")

    user_id = f"restart-{uuid.uuid4().hex}"
    password = f"temporary-password-{uuid.uuid4().hex}"
    task_title = f"Daily process restart {uuid.uuid4().hex}"
    idempotency_key = str(uuid.uuid4())
    _require(uuid.UUID(idempotency_key).version == 4, "idempotency key is not UUIDv4")

    bootstrap_path = tmp_path / "http_server_child.py"
    children: list[tuple[subprocess.Popen[str], int | None]] = []
    first_token = None
    try:
        first_process, first_ready = _start_server_process(tmp_path, bootstrap_path, database_url)
        first_port = int(first_ready["port"])
        first_children_index = len(children)
        children.append((first_process, first_port))
        first_pid = int(first_ready["pid"])
        first_base_url = f"http://127.0.0.1:{first_port}"
        _database_diagnostic(first_base_url, first_ready, database_url, database_path)

        register_status, _ = _http_request(
            first_base_url,
            "POST",
            "/api/register",
            payload={"user_id": user_id, "display_name": user_id, "password": password},
        )
        _require(register_status == HTTPStatus.OK, "real HTTP registration failed in first process")

        login_status, login_body = _http_request(
            first_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        _require(login_status == HTTPStatus.OK, "real HTTP login failed in first process")
        first_token = json.loads(login_body)["token"]
        _require(bool(first_token), "first process did not return a bearer session")

        me_status, me_body = _http_request(
            first_base_url, "GET", "/api/me", token=first_token
        )
        _require(
            me_status == HTTPStatus.OK and json.loads(me_body) == {"user_id": user_id},
            "first process bearer did not authenticate /api/me as the registered user",
        )

        create_status, create_body = _http_request(
            first_base_url,
            "POST",
            "/api/tasks",
            token=first_token,
            payload={"text": f"每天 {task_title}"},
        )
        _require(create_status == HTTPStatus.OK, "real HTTP recurring-task creation failed")
        created_tasks = json.loads(create_body)["tasks"]
        matching_tasks = [task for task in created_tasks if task.get("title") == task_title]
        _require(len(matching_tasks) == 1, "HTTP task creation did not create the requested task exactly once")
        task_id = int(matching_tasks[0]["id"])
        _require(
            matching_tasks[0].get("recurrence") == "daily"
            and matching_tasks[0].get("status") == "todo",
            "HTTP-created task is not a daily recurring todo",
        )

        first_done_status, first_done_body = _http_request(
            first_base_url,
            "POST",
            f"/api/tasks/{task_id}/done",
            token=first_token,
            idempotency_key=idempotency_key,
        )
        _require(first_done_status == HTTPStatus.OK, "first real HTTP /done request did not succeed")
        _require(isinstance(first_done_body, bytes), "first /done response body is not raw bytes")
        _assert_first_done_state(
            database_path,
            user_id,
            task_id,
            idempotency_key,
            first_done_status,
            first_done_body,
        )
        first_counts, first_snapshot = _snapshot_all_application_tables(database_path)
        required_tables = {
            "users",
            "sessions",
            "tasks",
            "task_events",
            "task_done_occurrences",
            "task_done_idempotency",
            "task_postpone_idempotency",
        }
        _require(required_tables.issubset(first_snapshot), "first SQLite snapshot omitted application tables")
        _require(first_counts.get("sessions") == 1, "first process did not create exactly one session row")
        _require(first_counts.get("tasks") == 2, "first completion did not leave source and next task rows")
        _require(
            first_counts.get("task_done_occurrences") == 1
            and first_counts.get("task_done_idempotency") == 1,
            "first completion did not leave exactly one occurrence and done-ledger row",
        )

        first_returncode, first_graceful, first_clean = _finish_child_process(
            first_process, first_port, graceful_timeout=10.0
        )
        _require(first_graceful, "first Python server process did not exit through SIGTERM shutdown")
        _require(first_returncode == 0, "first Python server process did not exit normally")
        _require(first_clean, "first child process was not reaped or its TCP port stayed open")
        _require(
            children[first_children_index][0].poll() is not None,
            "first OS process is still running after shutdown",
        )

        second_process, second_ready = _start_server_process(tmp_path, bootstrap_path, database_url)
        second_port = int(second_ready["port"])
        children.append((second_process, second_port))
        second_pid = int(second_ready["pid"])
        _require(first_pid != second_pid, "restart reused the first OS process instead of starting a new process")
        second_base_url = f"http://127.0.0.1:{second_port}"
        _database_diagnostic(second_base_url, second_ready, database_url, database_path)

        second_login_status, second_login_body = _http_request(
            second_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        _require(second_login_status == HTTPStatus.OK, "real HTTP re-login failed in second process")
        second_token = json.loads(second_login_body)["token"]
        _require(bool(second_token), "second process did not return a bearer session")
        _require(second_token != first_token, "second process did not issue a new bearer session")

        me_status, me_body = _http_request(
            second_base_url, "GET", "/api/me", token=second_token
        )
        _require(
            me_status == HTTPStatus.OK and json.loads(me_body) == {"user_id": user_id},
            "second process bearer did not authenticate /api/me as the registered user",
        )

        after_login_counts, after_login_snapshot = _snapshot_all_application_tables(database_path)
        _require(
            set(after_login_snapshot) == set(first_snapshot),
            "application table set changed after second-process login",
        )
        for table_name in first_snapshot:
            if table_name != "sessions":
                _require(
                    after_login_snapshot[table_name] == first_snapshot[table_name],
                    f"non-session application rows changed after re-login in {table_name}",
                )
        previous_sessions = first_snapshot["sessions"]
        current_sessions = after_login_snapshot["sessions"]
        _require(
            after_login_counts.get("sessions") == first_counts.get("sessions", 0) + 1,
            "second login did not add exactly one session row",
        )
        _require(
            current_sessions[:-1] == previous_sessions,
            "second login changed or removed a pre-existing session row",
        )
        _require(
            len(current_sessions) == len(previous_sessions) + 1
            and current_sessions[-1][1] == user_id,
            "the one additional session row does not belong to the test user",
        )

        before_replay_counts, before_replay_snapshot = _snapshot_all_application_tables(database_path)
        _require(
            before_replay_counts == after_login_counts and before_replay_snapshot == after_login_snapshot,
            "database changed between re-login snapshot and replay",
        )
        replay_status, replay_body = _http_request(
            second_base_url,
            "POST",
            f"/api/tasks/{task_id}/done",
            token=second_token,
            idempotency_key=idempotency_key,
        )
        _require(replay_status == first_done_status, "cross-process replay changed the HTTP status")
        _require(replay_body == first_done_body, "cross-process replay changed the raw HTTP response bytes")

        after_replay_counts, after_replay_snapshot = _snapshot_all_application_tables(database_path)
        _require(
            after_replay_counts == before_replay_counts
            and after_replay_snapshot == before_replay_snapshot,
            "replay mutated one or more SQLite application tables",
        )
        _require(
            after_replay_snapshot["sessions"] == current_sessions,
            "replay added, removed, or changed a session row",
        )

        second_returncode, second_graceful, second_clean = _finish_child_process(
            second_process, second_port, graceful_timeout=10.0
        )
        _require(second_graceful, "second Python server process did not exit through SIGTERM shutdown")
        _require(second_returncode == 0, "second Python server process did not exit normally")
        _require(second_clean, "second child process was not reaped or its TCP port stayed open")
        _require(
            second_process.poll() is not None,
            "second OS process is still running after shutdown",
        )

        print(
            "verified independent server processes: "
            f"PID {first_pid} on port {first_port} -> PID {second_pid} on port {second_port}; "
            "same isolated SQLite file, graceful SIGTERM exits, replay status/body and all-table snapshots verified"
        )
    finally:
        for process, port in reversed(children):
            _finish_child_process(process, port, graceful_timeout=5.0)


def test_concurrent_done_http_requests_across_independent_processes_write_once(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "done-process-race.sqlite3"
    database_url = f"sqlite:///{database_path}"
    _require(database_url.startswith("sqlite:////"), "race database URL is not an absolute SQLite URL")
    _require(database_path.parent.resolve() == tmp_path.resolve(), "race database is outside tmp_path")

    user_id = f"race-{uuid.uuid4().hex}"
    password = f"temporary-password-{uuid.uuid4().hex}"
    task_title = f"Daily process race {uuid.uuid4().hex}"
    idempotency_key = str(uuid.uuid4())
    _require(
        uuid.UUID(idempotency_key).version == 4 and str(uuid.UUID(idempotency_key)) == idempotency_key,
        "idempotency key is not a canonical UUIDv4",
    )

    bootstrap_path = tmp_path / "http_server_race_child.py"
    gate = _DoneDispatchGate(timeout=25.0)
    children: list[tuple[subprocess.Popen[str], int]] = []
    try:
        first_process, first_ready = _start_server_process(
            tmp_path, bootstrap_path, database_url, gate.address
        )
        first_port = int(first_ready["port"])
        children.append((first_process, first_port))
        first_pid = int(first_ready["pid"])
        first_base_url = f"http://127.0.0.1:{first_port}"
        _database_diagnostic(first_base_url, first_ready, database_url, database_path)

        register_status, _ = _http_request(
            first_base_url,
            "POST",
            "/api/register",
            payload={"user_id": user_id, "display_name": user_id, "password": password},
        )
        _require(register_status == HTTPStatus.OK, "real HTTP registration failed before the race")
        login_status, login_body = _http_request(
            first_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        _require(login_status == HTTPStatus.OK, "real HTTP login failed before the race")
        token = json.loads(login_body)["token"]
        _require(bool(token), "first process did not return a bearer session")

        create_status, create_body = _http_request(
            first_base_url,
            "POST",
            "/api/tasks",
            token=token,
            payload={"text": f"每天 {task_title}"},
        )
        _require(create_status == HTTPStatus.OK, "real HTTP recurring-task creation failed")
        created_tasks = json.loads(create_body)["tasks"]
        matching_tasks = [task for task in created_tasks if task.get("title") == task_title]
        _require(len(matching_tasks) == 1, "HTTP creation did not create exactly one race source task")
        task_id = int(matching_tasks[0]["id"])
        _require(
            matching_tasks[0].get("recurrence") == "daily"
            and matching_tasks[0].get("status") == "todo",
            "race source task is not a daily recurring todo",
        )

        second_process, second_ready = _start_server_process(
            tmp_path, bootstrap_path, database_url, gate.address
        )
        second_port = int(second_ready["port"])
        children.append((second_process, second_port))
        second_pid = int(second_ready["pid"])
        _require(first_pid != second_pid, "race servers do not have independent OS PIDs")
        _require(first_port != second_port, "race servers do not listen on independent TCP ports")
        second_base_url = f"http://127.0.0.1:{second_port}"
        _database_diagnostic(second_base_url, second_ready, database_url, database_path)

        me_status, me_body = _http_request(second_base_url, "GET", "/api/me", token=token)
        _require(
            me_status == HTTPStatus.OK and json.loads(me_body) == {"user_id": user_id},
            "the first process session did not authenticate against the second process database",
        )

        before_counts, before_snapshot = _snapshot_all_application_tables(database_path)
        _require(before_counts.get("tasks") == 1, "race did not start with exactly one task")
        _require(before_counts.get("sessions") == 1, "race did not start with exactly one session")
        gate.expected_pids = {first_pid, second_pid}

        done_path = f"/api/tasks/{task_id}/done"
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="done-race-client") as clients:
            first_request = clients.submit(
                _http_request,
                first_base_url,
                "POST",
                done_path,
                token=token,
                idempotency_key=idempotency_key,
                timeout=35.0,
            )
            second_request = clients.submit(
                _http_request,
                second_base_url,
                "POST",
                done_path,
                token=token,
                idempotency_key=idempotency_key,
                timeout=35.0,
            )
            first_response = first_request.result(timeout=40.0)
            second_response = second_request.result(timeout=40.0)

        gate.assert_released()
        _require(
            first_response[0] == HTTPStatus.OK and second_response[0] == HTTPStatus.OK,
            "both concurrent real HTTP /done requests did not return 200",
        )
        _require(
            first_response == second_response,
            "concurrent processes returned different HTTP status or raw response bytes",
        )
        response_status, response_body = first_response
        _require(isinstance(response_body, bytes), "concurrent /done response body is not raw bytes")
        _assert_first_done_state(
            database_path,
            user_id,
            task_id,
            idempotency_key,
            response_status,
            response_body,
        )

        after_race_counts, after_race_snapshot = _snapshot_all_application_tables(database_path)
        required_tables = {
            "users",
            "sessions",
            "tasks",
            "task_events",
            "task_done_occurrences",
            "task_done_idempotency",
            "task_postpone_idempotency",
        }
        _require(required_tables.issubset(after_race_snapshot), "race snapshot omitted application tables")
        expected_count_deltas = {
            "tasks": 1,
            "task_events": 2,
            "task_done_occurrences": 1,
            "task_done_idempotency": 1,
        }
        for table_name in before_counts:
            expected_delta = expected_count_deltas.get(table_name, 0)
            _require(
                after_race_counts.get(table_name, 0) - before_counts[table_name] == expected_delta,
                f"race wrote an unexpected number of rows in {table_name}",
            )
        with sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True) as connection:
            next_tasks = connection.execute(
                "SELECT id, status, recurrence FROM tasks WHERE user_id = ? AND title = ? ORDER BY id",
                (user_id, task_title),
            ).fetchall()
        _require(len(next_tasks) == 2, "race created more or fewer than one recurring next task")
        _require(
            sum(row[1] == "todo" and row[2] == "daily" for row in next_tasks) == 1,
            "race did not leave exactly one daily recurring next task",
        )
        _require(
            after_race_counts.get("task_done_occurrences") == 1
            and after_race_counts.get("task_done_idempotency") == 1,
            "race did not persist exactly one occurrence and one done ledger row",
        )

        before_replays_counts, before_replays_snapshot = _snapshot_all_application_tables(database_path)
        _require(
            before_replays_counts == after_race_counts
            and before_replays_snapshot == after_race_snapshot,
            "database changed before replay requests began",
        )
        for process_name, base_url in (
            ("first", first_base_url),
            ("second", second_base_url),
        ):
            replay_status, replay_body = _http_request(
                base_url,
                "POST",
                done_path,
                token=token,
                idempotency_key=idempotency_key,
            )
            _require(
                (replay_status, replay_body) == first_response,
                f"{process_name} server replay changed the original status or raw response bytes",
            )
            replay_counts, replay_snapshot = _snapshot_all_application_tables(database_path)
            _require(
                replay_counts == before_replays_counts and replay_snapshot == before_replays_snapshot,
                f"{process_name} server replay mutated SQLite application tables",
            )

        for process, port in reversed(children):
            returncode, graceful, clean = _finish_child_process(
                process, port, graceful_timeout=10.0
            )
            _require(graceful, "race server process did not exit through graceful SIGTERM shutdown")
            _require(returncode == 0, "race server process did not exit normally")
            _require(clean, "race child was not reaped or its TCP port remained open")
        print(
            "verified simultaneous real HTTP /done dispatch across independent processes "
            f"PID {first_pid} on port {first_port} and PID {second_pid} on port {second_port}; "
            "both handlers met at the test gate, one completion was persisted, and both replays were byte-identical"
        )
    finally:
        gate.close()
        for process, port in reversed(children):
            _finish_child_process(process, port, graceful_timeout=5.0)


def test_same_key_different_task_ids_conflicts_across_processes_and_replays_exactly(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "done-cross-task-conflict.sqlite3"
    database_url = f"sqlite:///{database_path}"
    _require(database_url.startswith("sqlite:////"), "database URL is not an absolute temporary SQLite URL")
    _require(database_path.parent.resolve() == tmp_path.resolve(), "database is not isolated under tmp_path")

    user_id = f"cross-task-{uuid.uuid4().hex}"
    password = f"temporary-password-{uuid.uuid4().hex}"
    task_titles = {
        "first": f"Cross-process first daily {uuid.uuid4().hex}",
        "second": f"Cross-process second daily {uuid.uuid4().hex}",
    }
    idempotency_key = str(uuid.uuid4())
    _require(
        uuid.UUID(idempotency_key).version == 4
        and str(uuid.UUID(idempotency_key)) == idempotency_key,
        "idempotency key is not a canonical UUIDv4",
    )

    bootstrap_path = tmp_path / "http_server_cross_task_child.py"
    gate = _DoneDispatchGate(timeout=25.0)
    children: list[tuple[subprocess.Popen[str], int]] = []
    try:
        first_process, first_ready = _start_server_process(
            tmp_path, bootstrap_path, database_url, gate.address
        )
        first_port = int(first_ready["port"])
        children.append((first_process, first_port))
        first_pid = int(first_ready["pid"])
        first_base_url = f"http://127.0.0.1:{first_port}"
        _database_diagnostic(first_base_url, first_ready, database_url, database_path)

        register_status, _ = _http_request(
            first_base_url,
            "POST",
            "/api/register",
            payload={"user_id": user_id, "display_name": user_id, "password": password},
        )
        _require(register_status == HTTPStatus.OK, "real HTTP registration failed before the cross-task race")
        login_status, login_body = _http_request(
            first_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        _require(login_status == HTTPStatus.OK, "real HTTP login failed before the cross-task race")
        token = json.loads(login_body)["token"]
        _require(bool(token), "first process did not return a bearer session")

        task_ids: dict[str, int] = {}
        for label, title in task_titles.items():
            create_status, create_body = _http_request(
                first_base_url,
                "POST",
                "/api/tasks",
                token=token,
                payload={"text": f"每天 {title}"},
            )
            _require(create_status == HTTPStatus.OK, f"real HTTP creation of the {label} recurring task failed")
            created_tasks = json.loads(create_body)["tasks"]
            matching_tasks = [task for task in created_tasks if task.get("title") == title]
            _require(len(matching_tasks) == 1, f"HTTP creation did not create exactly one {label} source task")
            _require(
                matching_tasks[0].get("recurrence") == "daily"
                and matching_tasks[0].get("status") == "todo",
                f"{label} source task is not a daily recurring todo",
            )
            task_ids[label] = int(matching_tasks[0]["id"])
        _require(task_ids["first"] != task_ids["second"], "cross-task requests do not target distinct task IDs")

        second_process, second_ready = _start_server_process(
            tmp_path, bootstrap_path, database_url, gate.address
        )
        second_port = int(second_ready["port"])
        children.append((second_process, second_port))
        second_pid = int(second_ready["pid"])
        _require(first_pid != second_pid, "cross-task race servers do not have independent OS PIDs")
        _require(first_port != second_port, "cross-task race servers do not listen on independent TCP ports")
        second_base_url = f"http://127.0.0.1:{second_port}"
        _database_diagnostic(second_base_url, second_ready, database_url, database_path)
        me_status, me_body = _http_request(second_base_url, "GET", "/api/me", token=token)
        _require(
            me_status == HTTPStatus.OK and json.loads(me_body) == {"user_id": user_id},
            "the first process session did not authenticate against the second process database",
        )

        before_counts, before_snapshot = _snapshot_all_application_tables(database_path)
        _require(before_counts.get("tasks") == 2, "cross-task race did not start with exactly two tasks")
        _require(before_counts.get("sessions") == 1, "cross-task race did not start with exactly one session")
        gate.expected_pids = {first_pid, second_pid}

        first_done_path = f"/api/tasks/{task_ids['first']}/done"
        second_done_path = f"/api/tasks/{task_ids['second']}/done"
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="cross-task-done-client") as clients:
            first_request = clients.submit(
                _http_request,
                first_base_url,
                "POST",
                first_done_path,
                token=token,
                idempotency_key=idempotency_key,
                timeout=35.0,
            )
            second_request = clients.submit(
                _http_request,
                second_base_url,
                "POST",
                second_done_path,
                token=token,
                idempotency_key=idempotency_key,
                timeout=35.0,
            )
            first_response = first_request.result(timeout=40.0)
            second_response = second_request.result(timeout=40.0)

        gate.assert_released()
        responses = {"first": first_response, "second": second_response}
        winners = [label for label, response in responses.items() if response[0] == HTTPStatus.OK]
        losers = [label for label, response in responses.items() if response[0] == HTTPStatus.CONFLICT]
        _require(
            len(winners) == 1 and len(losers) == 1,
            "same-key requests for different tasks did not return exactly one 200 and one 409",
        )
        winner_label = winners[0]
        loser_label = losers[0]
        winner_task_id = task_ids[winner_label]
        loser_task_id = task_ids[loser_label]
        winner_response = responses[winner_label]
        loser_response = responses[loser_label]
        expected_conflict_body = json.dumps(
            {"error": "idempotency_conflict"}, ensure_ascii=False
        ).encode("utf-8")
        _require(
            loser_response[1] == expected_conflict_body,
            "losing task did not return the exact idempotency_conflict response body",
        )

        _assert_first_done_state(
            database_path,
            user_id,
            winner_task_id,
            idempotency_key,
            winner_response[0],
            winner_response[1],
        )
        after_race_counts, after_race_snapshot = _snapshot_all_application_tables(database_path)
        required_tables = {
            "users",
            "sessions",
            "tasks",
            "task_events",
            "task_done_occurrences",
            "task_done_idempotency",
            "task_postpone_idempotency",
        }
        _require(required_tables.issubset(after_race_snapshot), "cross-task race snapshot omitted application tables")
        _require(
            set(before_snapshot) == set(after_race_snapshot),
            "cross-task race changed the set of application tables",
        )
        expected_count_deltas = {
            "tasks": 1,
            "task_events": 2,
            "task_done_occurrences": 1,
            "task_done_idempotency": 1,
        }
        for table_name in before_counts:
            expected_delta = expected_count_deltas.get(table_name, 0)
            _require(
                after_race_counts.get(table_name, 0) - before_counts[table_name] == expected_delta,
                f"cross-task race wrote an unexpected number of rows in {table_name}",
            )

        database_uri = f"file:{database_path.resolve()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            source_rows = connection.execute(
                "SELECT id, title, status, recurrence, user_id FROM tasks "
                "WHERE user_id = ? AND id IN (?, ?) ORDER BY id",
                (user_id, task_ids["first"], task_ids["second"]),
            ).fetchall()
            source_events = connection.execute(
                "SELECT id, task_id, event_type, payload FROM task_events "
                "WHERE task_id IN (?, ?) AND event_type = ? AND payload = ? ORDER BY id",
                (task_ids["first"], task_ids["second"], "status_changed", "done"),
            ).fetchall()
            source_occurrences = connection.execute(
                "SELECT source_task_id, source_done_event_id, next_task_id "
                "FROM task_done_occurrences WHERE user_id = ? AND source_task_id IN (?, ?)",
                (user_id, task_ids["first"], task_ids["second"]),
            ).fetchall()
            next_rows = connection.execute(
                "SELECT id, title, status, recurrence, user_id FROM tasks "
                "WHERE user_id = ? AND title = ? ORDER BY id",
                (user_id, task_titles[winner_label]),
            ).fetchall()
            loser_title_rows = connection.execute(
                "SELECT id, status, recurrence FROM tasks WHERE user_id = ? AND title = ?",
                (user_id, task_titles[loser_label]),
            ).fetchall()

        source_by_id = {row["id"]: row for row in source_rows}
        _require(len(source_rows) == 2, "one of the original cross-task sources is missing")
        _require(
            source_by_id[winner_task_id]["status"] == "done"
            and source_by_id[winner_task_id]["recurrence"] == "daily"
            and source_by_id[loser_task_id]["status"] == "todo"
            and source_by_id[loser_task_id]["recurrence"] == "daily",
            "cross-task race did not leave only the winning source done",
        )
        _require(
            len(source_events) == 1 and source_events[0]["task_id"] == winner_task_id,
            "cross-task race wrote a done event for more or fewer than the winning source",
        )
        _require(
            len(source_occurrences) == 1
            and source_occurrences[0]["source_task_id"] == winner_task_id,
            "cross-task race wrote an occurrence for the loser or an unexpected number of occurrences",
        )
        winner_occurrence = source_occurrences[0]
        _require(
            winner_occurrence["source_done_event_id"] == source_events[0]["id"]
            and winner_occurrence["next_task_id"] is not None,
            "winning occurrence does not reference the unique done event and next task",
        )
        _require(
            len(next_rows) == 2
            and sum(
                row["id"] == winner_occurrence["next_task_id"]
                and row["status"] == "todo"
                and row["recurrence"] == "daily"
                and row["user_id"] == user_id
                for row in next_rows
            ) == 1,
            "winning task did not create exactly one matching recurring next task",
        )
        _require(
            len(loser_title_rows) == 1
            and loser_title_rows[0]["id"] == loser_task_id
            and loser_title_rows[0]["status"] == "todo",
            "losing task did not remain TODO without a next task",
        )

        before_replays_counts, before_replays_snapshot = _snapshot_all_application_tables(database_path)
        _require(
            before_replays_counts == after_race_counts
            and before_replays_snapshot == after_race_snapshot,
            "database changed before cross-task replay requests began",
        )
        replay_base_url = second_base_url if winner_label == "first" else first_base_url
        winner_replay_status, winner_replay_body = _http_request(
            replay_base_url,
            "POST",
            f"/api/tasks/{winner_task_id}/done",
            token=token,
            idempotency_key=idempotency_key,
        )
        _require(
            (winner_replay_status, winner_replay_body) == winner_response,
            "winning task replay changed the original HTTP status or raw response bytes",
        )
        replay_counts, replay_snapshot = _snapshot_all_application_tables(database_path)
        _require(
            replay_counts == before_replays_counts and replay_snapshot == before_replays_snapshot,
            "winning task replay mutated SQLite application tables",
        )

        loser_replay_status, loser_replay_body = _http_request(
            replay_base_url,
            "POST",
            f"/api/tasks/{loser_task_id}/done",
            token=token,
            idempotency_key=idempotency_key,
        )
        _require(
            (loser_replay_status, loser_replay_body) == (HTTPStatus.CONFLICT, expected_conflict_body),
            "losing task replay did not remain an exact 409 idempotency_conflict",
        )
        final_counts, final_snapshot = _snapshot_all_application_tables(database_path)
        _require(
            final_counts == before_replays_counts and final_snapshot == before_replays_snapshot,
            "losing task replay mutated SQLite application tables",
        )

        for process, port in reversed(children):
            returncode, graceful, clean = _finish_child_process(
                process, port, graceful_timeout=10.0
            )
            _require(graceful, "cross-task race server did not exit through graceful SIGTERM shutdown")
            _require(returncode == 0, "cross-task race server did not exit normally")
            _require(clean, "cross-task child was not reaped or its TCP port remained open")
        children.clear()
    finally:
        gate.close()
        for process, port in reversed(children):
            _finish_child_process(process, port, graceful_timeout=5.0)



def test_concurrent_done_http_requests_with_distinct_keys_across_processes_replay_write_once(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "done-process-distinct-keys-race.sqlite3"
    database_url = f"sqlite:///{database_path}"
    _require(database_url.startswith("sqlite:////"), "race database URL is not an absolute SQLite URL")
    _require(database_path.parent.resolve() == tmp_path.resolve(), "race database is outside tmp_path")

    user_id = f"distinct-keys-race-{uuid.uuid4().hex}"
    password = f"temporary-password-{uuid.uuid4().hex}"
    task_title = f"Daily distinct-keys process race {uuid.uuid4().hex}"
    idempotency_keys = [str(uuid.uuid4()), str(uuid.uuid4())]
    _require(len(set(idempotency_keys)) == 2, "race idempotency keys are not distinct")
    _require(
        all(
            uuid.UUID(key).version == 4 and str(uuid.UUID(key)) == key
            for key in idempotency_keys
        ),
        "race idempotency keys are not canonical UUIDv4 values",
    )

    bootstrap_path = tmp_path / "http_server_distinct_keys_race_child.py"
    gate = _DoneDispatchGate(timeout=25.0)
    children: list[tuple[subprocess.Popen[str], int]] = []
    try:
        first_process, first_ready = _start_server_process(
            tmp_path, bootstrap_path, database_url, gate.address
        )
        first_port = int(first_ready["port"])
        children.append((first_process, first_port))
        first_pid = int(first_ready["pid"])
        first_base_url = f"http://127.0.0.1:{first_port}"
        _database_diagnostic(first_base_url, first_ready, database_url, database_path)

        register_status, _ = _http_request(
            first_base_url,
            "POST",
            "/api/register",
            payload={"user_id": user_id, "display_name": user_id, "password": password},
        )
        _require(register_status == HTTPStatus.OK, "real HTTP registration failed before distinct-key race")
        login_status, login_body = _http_request(
            first_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        _require(login_status == HTTPStatus.OK, "real HTTP login failed before distinct-key race")
        token = json.loads(login_body)["token"]
        _require(bool(token), "first process did not return a bearer session")

        create_status, create_body = _http_request(
            first_base_url,
            "POST",
            "/api/tasks",
            token=token,
            payload={"text": f"每天 {task_title}"},
        )
        _require(create_status == HTTPStatus.OK, "real HTTP recurring-task creation failed")
        created_tasks = json.loads(create_body)["tasks"]
        matching_tasks = [task for task in created_tasks if task.get("title") == task_title]
        _require(len(matching_tasks) == 1, "HTTP creation did not create exactly one race source task")
        task_id = int(matching_tasks[0]["id"])
        _require(
            matching_tasks[0].get("recurrence") == "daily"
            and matching_tasks[0].get("status") == "todo",
            "race source task is not a daily recurring todo",
        )

        second_process, second_ready = _start_server_process(
            tmp_path, bootstrap_path, database_url, gate.address
        )
        second_port = int(second_ready["port"])
        children.append((second_process, second_port))
        second_pid = int(second_ready["pid"])
        _require(first_pid != second_pid, "race servers do not have independent OS PIDs")
        _require(first_port != second_port, "race servers do not listen on independent TCP ports")
        second_base_url = f"http://127.0.0.1:{second_port}"
        _database_diagnostic(second_base_url, second_ready, database_url, database_path)

        me_status, me_body = _http_request(second_base_url, "GET", "/api/me", token=token)
        _require(
            me_status == HTTPStatus.OK and json.loads(me_body) == {"user_id": user_id},
            "the first process session did not authenticate against the second process database",
        )

        before_counts, before_snapshot = _snapshot_all_application_tables(database_path)
        _require(before_counts.get("tasks") == 1, "race did not start with exactly one task")
        _require(before_counts.get("sessions") == 1, "race did not start with exactly one session")
        gate.expected_pids = {first_pid, second_pid}

        done_path = f"/api/tasks/{task_id}/done"
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="distinct-keys-done-client") as clients:
            first_request = clients.submit(
                _http_request,
                first_base_url,
                "POST",
                done_path,
                token=token,
                idempotency_key=idempotency_keys[0],
                timeout=35.0,
            )
            second_request = clients.submit(
                _http_request,
                second_base_url,
                "POST",
                done_path,
                token=token,
                idempotency_key=idempotency_keys[1],
                timeout=35.0,
            )
            responses = [first_request.result(timeout=40.0), second_request.result(timeout=40.0)]

        gate.assert_released()
        _require(
            all(status == HTTPStatus.OK for status, _body in responses),
            "distinct-key concurrent /done requests did not both return 200",
        )
        response_payloads = [json.loads(body) for _status, body in responses]
        created_response_indexes = [
            index
            for index, payload in enumerate(response_payloads)
            if payload.get("message", "").startswith("已创建下一期任务 #")
        ]
        already_done_indexes = [
            index
            for index, payload in enumerate(response_payloads)
            if payload == {"message": "任务已完成。"}
        ]
        _require(
            len(created_response_indexes) == 1 and len(already_done_indexes) == 1,
            "race did not return exactly one next-created and one already-done response",
        )

        database_uri = f"file:{database_path.resolve()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            source_rows = connection.execute(
                "SELECT id, title, status, recurrence, user_id FROM tasks "
                "WHERE user_id = ? AND id = ?",
                (user_id, task_id),
            ).fetchall()
            done_events = connection.execute(
                "SELECT id, task_id, event_type, payload FROM task_events "
                "WHERE task_id = ? AND event_type = ? AND payload = ? ORDER BY id",
                (task_id, "status_changed", "done"),
            ).fetchall()
            occurrences = connection.execute(
                "SELECT source_done_event_id, next_task_id, response_status, response_json "
                "FROM task_done_occurrences WHERE user_id = ? AND source_task_id = ?",
                (user_id, task_id),
            ).fetchall()
            next_tasks = connection.execute(
                "SELECT id, title, status, recurrence, user_id FROM tasks "
                "WHERE user_id = ? AND title = ? ORDER BY id",
                (user_id, task_title),
            ).fetchall()
            ledger = connection.execute(
                "SELECT idempotency_key, response_status, response_json "
                "FROM task_done_idempotency WHERE user_id = ? ORDER BY idempotency_key",
                (user_id,),
            ).fetchall()

        _require(
            len(source_rows) == 1
            and source_rows[0]["status"] == "done"
            and source_rows[0]["recurrence"] == "daily"
            and source_rows[0]["user_id"] == user_id,
            "race did not leave the recurring source task DONE",
        )
        _require(len(done_events) == 1, "race did not persist exactly one source DONE event")
        _require(len(occurrences) == 1, "race did not persist exactly one done occurrence")
        _require(
            len(next_tasks) == 2
            and sum(
                row["status"] == "todo"
                and row["recurrence"] == "daily"
                and row["user_id"] == user_id
                for row in next_tasks
            ) == 1,
            "race did not create exactly one recurring next task",
        )
        occurrence = occurrences[0]
        _require(
            occurrence["source_done_event_id"] == done_events[0]["id"]
            and occurrence["next_task_id"] is not None,
            "occurrence does not reference the unique source DONE event and next task",
        )
        next_task = next(row for row in next_tasks if row["id"] == occurrence["next_task_id"])
        created_response_index = created_response_indexes[0]
        _require(
            response_payloads[created_response_index]
            == {"message": f"已创建下一期任务 #{next_task['id']}：{task_title}"},
            "next-created HTTP response does not identify the persisted next task",
        )
        _require(
            occurrence["response_status"] == HTTPStatus.OK
            and json.loads(occurrence["response_json"])
            == response_payloads[created_response_index],
            "occurrence response does not match the unique next-created HTTP response",
        )

        ledger_by_key = {row["idempotency_key"]: row for row in ledger}
        _require(
            set(ledger_by_key) == set(idempotency_keys) and len(ledger) == 2,
            "race did not persist exactly one ledger row for each distinct key",
        )
        for key, (status, body) in zip(idempotency_keys, responses):
            ledger_row = ledger_by_key[key]
            _require(
                ledger_row["response_status"] == status
                and json.loads(ledger_row["response_json"]) == json.loads(body),
                "an idempotency ledger row does not preserve its key's HTTP response",
            )

        after_race_counts, after_race_snapshot = _snapshot_all_application_tables(database_path)
        required_tables = {
            "users",
            "sessions",
            "tasks",
            "task_events",
            "task_done_occurrences",
            "task_done_idempotency",
            "task_postpone_idempotency",
        }
        _require(required_tables.issubset(after_race_snapshot), "race snapshot omitted application tables")
        _require(
            set(before_snapshot) == set(after_race_snapshot),
            "race changed the set of application tables",
        )
        expected_count_deltas = {
            "tasks": 1,
            "task_events": 2,
            "task_done_occurrences": 1,
            "task_done_idempotency": 2,
        }
        for table_name in before_counts:
            expected_delta = expected_count_deltas.get(table_name, 0)
            _require(
                after_race_counts.get(table_name, 0) - before_counts[table_name] == expected_delta,
                f"race wrote an unexpected number of rows in {table_name}",
            )

        for process_name, base_url, key, original_response in (
            ("first", first_base_url, idempotency_keys[0], responses[0]),
            ("second", second_base_url, idempotency_keys[1], responses[1]),
        ):
            replay_status, replay_body = _http_request(
                base_url,
                "POST",
                done_path,
                token=token,
                idempotency_key=key,
            )
            _require(
                (replay_status, replay_body) == original_response,
                f"{process_name} server replay changed its key's original status or raw response bytes",
            )
            replay_counts, replay_snapshot = _snapshot_all_application_tables(database_path)
            _require(
                replay_counts == after_race_counts and replay_snapshot == after_race_snapshot,
                f"{process_name} server replay mutated SQLite application tables",
            )

        for process, port in reversed(children):
            returncode, graceful, clean = _finish_child_process(
                process, port, graceful_timeout=10.0
            )
            _require(graceful, "distinct-key race server did not exit through graceful SIGTERM shutdown")
            _require(returncode == 0, "distinct-key race server did not exit normally")
            _require(clean, "distinct-key race child was not reaped or its TCP port remained open")
        children.clear()
    finally:
        gate.close()
        for process, port in reversed(children):
            _finish_child_process(process, port, graceful_timeout=5.0)



def _read_saved_done_response(
    database_path: Path,
    user_id: str,
    task_id: int,
    idempotency_key: str,
) -> tuple[int, bytes]:
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        ledger = connection.execute(
            "SELECT response_status, response_json FROM task_done_idempotency "
            "WHERE user_id = ? AND idempotency_key = ?",
            (user_id, idempotency_key),
        ).fetchall()
        occurrences = connection.execute(
            "SELECT response_status, response_json FROM task_done_occurrences "
            "WHERE user_id = ? AND source_task_id = ?",
            (user_id, task_id),
        ).fetchall()
    _require(len(ledger) == 1, "committed done response ledger row is missing or duplicated")
    _require(len(occurrences) == 1, "committed done occurrence row is missing or duplicated")
    status = int(ledger[0]["response_status"])
    response_json = str(ledger[0]["response_json"])
    response_body = response_json.encode("utf-8")
    _require(
        occurrences[0]["response_status"] == status
        and occurrences[0]["response_json"] == response_json,
        "occurrence and idempotency ledger did not persist the same exact response",
    )
    _require(
        json.dumps(json.loads(response_body), ensure_ascii=False).encode("utf-8")
        == response_body,
        "persisted response body is not the exact JSON serialization returned by send_json",
    )
    return status, response_body


def test_done_http_recovers_after_committed_response_is_lost(tmp_path: Path) -> None:
    database_path = tmp_path / "done-process-response-loss.sqlite3"
    database_url = f"sqlite:///{database_path}"
    _require(
        database_url.startswith("sqlite:////"),
        "response-loss database URL is not an absolute temporary SQLite URL",
    )
    _require(
        database_path.parent.resolve() == tmp_path.resolve(),
        "response-loss database is outside tmp_path",
    )

    user_id = f"response-loss-{uuid.uuid4().hex}"
    password = f"temporary-password-{uuid.uuid4().hex}"
    task_title = f"Daily response loss {uuid.uuid4().hex}"
    idempotency_key = str(uuid.uuid4())
    _require(
        uuid.UUID(idempotency_key).version == 4,
        "response-loss idempotency key is not UUIDv4",
    )

    bootstrap_path = tmp_path / "http_server_response_loss_child.py"
    response_gate = _PostCommitResponseGate(timeout=20.0)
    children: list[tuple[subprocess.Popen[str], int | None]] = []
    client_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="done-response-loss-client"
    )
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
        _require(register_status == HTTPStatus.OK, "real HTTP registration failed before response loss")
        login_status, login_body = _http_request(
            first_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        _require(login_status == HTTPStatus.OK, "real HTTP login failed before response loss")
        first_token = json.loads(login_body)["token"]
        _require(bool(first_token), "first process did not return a bearer session")

        create_status, create_body = _http_request(
            first_base_url,
            "POST",
            "/api/tasks",
            token=first_token,
            payload={"text": f"每天 {task_title}"},
        )
        _require(create_status == HTTPStatus.OK, "real HTTP recurring-task creation failed")
        created_tasks = json.loads(create_body)["tasks"]
        matching_tasks = [task for task in created_tasks if task.get("title") == task_title]
        _require(
            len(matching_tasks) == 1,
            "HTTP task creation did not create the response-loss task exactly once",
        )
        task_id = int(matching_tasks[0]["id"])
        _require(
            matching_tasks[0].get("recurrence") == "daily"
            and matching_tasks[0].get("status") == "todo",
            "response-loss source task is not a daily recurring todo",
        )

        def make_first_request() -> tuple[str, object]:
            try:
                response = _http_request(
                    first_base_url,
                    "POST",
                    f"/api/tasks/{task_id}/done",
                    token=first_token,
                    idempotency_key=idempotency_key,
                    timeout=25.0,
                )
            except HTTPError as exc:
                return "http_response", (exc.code, exc.read())
            except (URLError, OSError, http.client.HTTPException) as exc:
                return "connection_failure", type(exc).__name__
            return "http_response", response

        first_client_request = client_executor.submit(make_first_request)
        response_gate.assert_hit(first_pid)
        _require(
            first_process.poll() is None,
            "first process exited before the parent confirmed the post-commit gate",
        )
        first_process.kill()
        first_returncode = first_process.wait(timeout=5.0)
        _require(
            first_returncode == -signal.SIGKILL,
            "first server process was not terminated by the expected SIGKILL",
        )
        _first_cleanup_code, _first_graceful, first_clean = _finish_child_process(
            first_process, first_port, graceful_timeout=0.1
        )
        _require(
            first_clean,
            "SIGKILLed first child was not reaped or its HTTP port remained open",
        )
        response_gate.close()
        client_outcome = first_client_request.result(timeout=10.0)
        _require(
            client_outcome[0] == "connection_failure",
            "client received an HTTP response instead of only a connection failure",
        )

        saved_status, saved_body = _read_saved_done_response(
            database_path, user_id, task_id, idempotency_key
        )
        _require(saved_status == HTTPStatus.OK, "first /done response status was not committed as HTTP 200")
        _assert_first_done_state(
            database_path,
            user_id,
            task_id,
            idempotency_key,
            saved_status,
            saved_body,
        )
        committed_counts, committed_snapshot = _snapshot_all_application_tables(database_path)
        required_tables = {
            "users",
            "sessions",
            "tasks",
            "task_events",
            "task_done_occurrences",
            "task_done_idempotency",
            "task_postpone_idempotency",
        }
        _require(
            required_tables.issubset(committed_snapshot),
            "post-commit response-loss snapshot omitted required application tables",
        )
        _require(committed_counts.get("sessions") == 1, "first process did not create exactly one session")
        _require(committed_counts.get("tasks") == 2, "committed completion did not leave source and next tasks")
        _require(
            committed_counts.get("task_events", 0) >= 3
            and committed_counts.get("task_done_occurrences") == 1
            and committed_counts.get("task_done_idempotency") == 1,
            "completion ledger, event, occurrence, or recurring task state was not committed exactly once",
        )

        second_process, second_ready = _start_server_process(
            tmp_path, bootstrap_path, database_url
        )
        second_port = int(second_ready["port"])
        children.append((second_process, second_port))
        second_pid = int(second_ready["pid"])
        _require(first_pid != second_pid, "response replay reused the first OS process")
        second_base_url = f"http://127.0.0.1:{second_port}"
        _database_diagnostic(second_base_url, second_ready, database_url, database_path)

        second_login_status, second_login_body = _http_request(
            second_base_url,
            "POST",
            "/api/login",
            payload={"user_id": user_id, "password": password},
        )
        _require(second_login_status == HTTPStatus.OK, "real HTTP re-login failed in second process")
        second_token = json.loads(second_login_body)["token"]
        _require(bool(second_token), "second process did not issue a bearer session")
        _require(second_token != first_token, "second process did not issue a distinct bearer session")
        me_status, me_body = _http_request(
            second_base_url, "GET", "/api/me", token=second_token
        )
        _require(
            me_status == HTTPStatus.OK and json.loads(me_body) == {"user_id": user_id},
            "second-process session did not authenticate as the registered user",
        )

        after_login_counts, after_login_snapshot = _snapshot_all_application_tables(database_path)
        _require(
            set(after_login_snapshot) == set(committed_snapshot),
            "application table set changed after second-process re-login",
        )
        for table_name in committed_snapshot:
            if table_name != "sessions":
                _require(
                    after_login_snapshot[table_name] == committed_snapshot[table_name],
                    f"second-process login changed committed business rows in {table_name}",
                )
        previous_sessions = committed_snapshot["sessions"]
        current_sessions = after_login_snapshot["sessions"]
        _require(
            after_login_counts.get("sessions") == committed_counts.get("sessions", 0) + 1,
            "second-process login did not add exactly one session row",
        )
        _require(
            current_sessions[:-1] == previous_sessions
            and len(current_sessions) == len(previous_sessions) + 1
            and current_sessions[-1][1] == user_id,
            "second login changed an old session or added a session for another user",
        )

        before_replay_counts, before_replay_snapshot = _snapshot_all_application_tables(database_path)
        _require(
            before_replay_counts == after_login_counts
            and before_replay_snapshot == after_login_snapshot,
            "database changed between second-process login and idempotent replay",
        )
        replay_status, replay_body = _http_request(
            second_base_url,
            "POST",
            f"/api/tasks/{task_id}/done",
            token=second_token,
            idempotency_key=idempotency_key,
        )
        _require(replay_status == saved_status, "replay changed the persisted HTTP status")
        _require(replay_body == saved_body, "replay did not return the persisted raw body byte-for-byte")

        after_replay_counts, after_replay_snapshot = _snapshot_all_application_tables(database_path)
        _require(
            after_replay_counts == before_replay_counts
            and after_replay_snapshot == before_replay_snapshot,
            "response-loss replay added or changed application rows",
        )
        _require(
            after_replay_snapshot["sessions"] == current_sessions,
            "response replay added, removed, or changed a session row",
        )

        second_returncode, second_graceful, second_clean = _finish_child_process(
            second_process, second_port, graceful_timeout=10.0
        )
        _require(second_graceful, "second response-replay process did not exit through SIGTERM shutdown")
        _require(second_returncode == 0, "second response-replay process did not exit normally")
        _require(second_clean, "second child was not reaped or its HTTP port stayed open")
        print(
            "verified post-commit/pre-response loss recovery across independent processes: "
            f"PID {first_pid} was SIGKILLed at send_json, then PID {second_pid} replayed "
            "the saved status and raw body without mutating business rows"
        )
    finally:
        response_gate.close()
        client_executor.shutdown(wait=True, cancel_futures=True)
        for process, port in reversed(children):
            _finish_child_process(process, port, graceful_timeout=5.0)
