from __future__ import annotations

import os
import json
import re
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from momentum_agent.storage import SQLiteTaskStore


REPO_ROOT = Path(__file__).resolve().parents[1]
DUE = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def isolated_cli_tasks(tmp_path):
    database = tmp_path / "cli-recurring.sqlite3"
    user_id = f"cli-test-{uuid4().hex}"
    store = SQLiteTaskStore(database)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, "CLI recurring test", "unused-test-hash", DUE.isoformat()),
        )
    recurring = store.create_task(
        "CLI daily recurring",
        due_at=DUE,
        recurrence="daily",
        user_id=user_id,
    )
    ordinary = store.create_task("CLI ordinary task", user_id=user_id)
    return database, user_id, recurring, ordinary


def _run_cli(database: Path, user_id: str, log_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    src_path = str(REPO_ROOT / "src")
    env = {
        "PATH": os.defpath,
        "HOME": str(database.parent.resolve()),
        "LC_CTYPE": "C.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MOMENTUM_DATABASE_URL": str(database.resolve()),
        "MOMENTUM_USER": user_id,
        "PYTHONPATH": src_path,
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "momentum_agent.cli",
            "--db",
            str(database.resolve()),
            "--log-dir",
            str(log_dir),
            *args,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _db_rows(database: Path, query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def _count(database: Path, query: str, params: tuple = ()) -> int:
    return int(_db_rows(database, query, params)[0]["n"])


def _done_event_count(database: Path, task_id: int) -> int:
    return _count(
        database,
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND event_type = 'status_changed' AND payload = 'done'",
        (task_id,),
    )


def _same_title_task_count(database: Path, user_id: str, title: str) -> int:
    return _count(
        database,
        "SELECT COUNT(*) AS n FROM tasks WHERE user_id = ? AND title = ?",
        (user_id, title),
    )


def test_cli_done_creates_one_next_per_real_recurring_transition(isolated_cli_tasks, tmp_path):
    database, user_id, recurring, ordinary = isolated_cli_tasks
    assert database.exists()
    assert SQLiteTaskStore(database).db_path.resolve() == database.resolve()
    log_dir = tmp_path / "cli-logs"

    first = _run_cli(database, user_id, log_dir, "done", str(recurring.id))
    assert first.returncode == 0
    first_match = re.fullmatch(
        rf"已创建下一期任务 #(\d+)：{re.escape(recurring.title)}\s*", first.stdout
    )
    assert first_match, first.stdout
    first_next_id = int(first_match.group(1))
    assert _done_event_count(database, recurring.id) == 1
    assert _same_title_task_count(database, user_id, recurring.title) == 2
    assert _count(
        database,
        "SELECT COUNT(*) AS n FROM tasks WHERE user_id = ? AND title = ? AND id != ?",
        (user_id, recurring.title, recurring.id),
    ) == 1
    assert _count(
        database,
        "SELECT COUNT(*) AS n FROM task_done_occurrences WHERE user_id = ? AND source_task_id = ?",
        (user_id, recurring.id),
    ) == 1
    assert _db_rows(database, "SELECT status FROM tasks WHERE id = ?", (first_next_id,))[0]["status"] == "todo"

    repeated = _run_cli(database, user_id, log_dir, "done", str(recurring.id))
    assert repeated.returncode == 0
    assert repeated.stdout.strip() == f"任务 #{recurring.id} 已处于完成状态：{recurring.title}"
    assert "下一期" not in repeated.stdout and "创建" not in repeated.stdout
    assert _done_event_count(database, recurring.id) == 1
    assert _same_title_task_count(database, user_id, recurring.title) == 2
    assert _count(
        database,
        "SELECT COUNT(*) AS n FROM task_done_occurrences WHERE user_id = ? AND source_task_id = ?",
        (user_id, recurring.id),
    ) == 1

    reopened = _run_cli(database, user_id, log_dir, "reopen", str(recurring.id))
    assert reopened.returncode == 0
    assert reopened.stdout.strip() == f"已恢复任务 #{recurring.id}「{recurring.title}」"
    assert _done_event_count(database, recurring.id) == 1
    assert _same_title_task_count(database, user_id, recurring.title) == 2

    completed_again = _run_cli(database, user_id, log_dir, "done", str(recurring.id))
    assert completed_again.returncode == 0
    second_match = re.fullmatch(
        rf"已创建下一期任务 #(\d+)：{re.escape(recurring.title)}\s*", completed_again.stdout
    )
    assert second_match, completed_again.stdout
    second_next_id = int(second_match.group(1))
    assert second_next_id != first_next_id
    assert _done_event_count(database, recurring.id) == 2
    assert _same_title_task_count(database, user_id, recurring.title) == 3
    assert _count(
        database,
        "SELECT COUNT(*) AS n FROM task_done_occurrences WHERE user_id = ? AND source_task_id = ?",
        (user_id, recurring.id),
    ) == 2

    ordinary_done = _run_cli(database, user_id, log_dir, "done", str(ordinary.id))
    assert ordinary_done.returncode == 0
    assert ordinary_done.stdout.strip() == f"已完成任务 #{ordinary.id}：{ordinary.title}"
    assert _done_event_count(database, ordinary.id) == 1
    assert _same_title_task_count(database, user_id, ordinary.title) == 1
    assert _count(
        database,
        "SELECT COUNT(*) AS n FROM tasks WHERE user_id = ? AND title = ? AND id != ?",
        (user_id, ordinary.title, ordinary.id),
    ) == 0
    assert _count(
        database,
        "SELECT COUNT(*) AS n FROM tasks WHERE user_id = ?",
        (user_id,),
    ) == 4


def test_cli_done_cross_process_recurring_completion_is_atomic(tmp_path):
    database = tmp_path / "cross-process-recurring.sqlite3"
    user_id = f"cli-race-{uuid4().hex}"
    store = SQLiteTaskStore(database)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, "CLI cross-process test", "unused-test-hash", DUE.isoformat()),
        )
    recurring = store.create_task(
        "CLI cross-process daily recurring",
        due_at=DUE,
        recurrence="daily",
        user_id=user_id,
    )
    assert database.exists()
    assert store.db_path.resolve() == database.resolve()

    hook_dir = tmp_path / "python-startup-hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(
        """import json
import os
import socket

from momentum_agent import config as config_module
from momentum_agent.storage.sqlite import SQLiteTaskStore


def _dotenv_disabled(*args, **kwargs):
    raise RuntimeError("dotenv access is disabled in this isolated CLI test")


config_module.load_dotenv = _dotenv_disabled

_original_complete_task_agent = SQLiteTaskStore.complete_task_agent


def _gated_complete_task_agent(self, task_id, *, user_id):
    report = {
        "pid": os.getpid(),
        "db_path": str(self.db_path.resolve()),
        "user_id": user_id,
        "task_id": task_id,
    }
    with socket.create_connection(
        (os.environ["MOMENTUM_TEST_GATE_HOST"], int(os.environ["MOMENTUM_TEST_GATE_PORT"])),
        timeout=30,
    ) as gate:
        gate.settimeout(30)
        gate.sendall(json.dumps(report).encode("utf-8") + b"\\n")
        response = bytearray()
        while not response.endswith(b"\\n"):
            chunk = gate.recv(4096)
            if not chunk:
                raise RuntimeError("test gate closed before releasing CLI process")
            response.extend(chunk)
        if bytes(response) != b"release\\n":
            raise RuntimeError("test gate returned an unexpected release token")
    return _original_complete_task_agent(self, task_id, user_id=user_id)


SQLiteTaskStore.complete_task_agent = _gated_complete_task_agent
""",
        encoding="utf-8",
    )

    gate_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    gate_clients: list[socket.socket] = []
    processes: list[subprocess.Popen[str]] = []
    completed: list[tuple[int, str, str]] = []
    try:
        gate_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        gate_server.bind(("127.0.0.1", 0))
        gate_server.listen(2)
        gate_server.settimeout(20)
        gate_host, gate_port = gate_server.getsockname()

        for index in range(2):
            src_path = str(REPO_ROOT / "src")
            env = {
                "PATH": os.defpath,
                "HOME": str(tmp_path.resolve()),
                "LC_CTYPE": "C.UTF-8",
                "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "MOMENTUM_DATABASE_URL": str(database.resolve()),
                "MOMENTUM_USER": user_id,
                "MOMENTUM_TEST_GATE_HOST": str(gate_host),
                "MOMENTUM_TEST_GATE_PORT": str(gate_port),
                "PYTHONPATH": os.pathsep.join((str(hook_dir), src_path)),
            }
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "momentum_agent.cli",
                        "--db",
                        str(database.resolve()),
                        "--log-dir",
                        str(tmp_path / f"cli-logs-{index}"),
                        "done",
                        str(recurring.id),
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        reports = []
        for _ in range(2):
            client, _ = gate_server.accept()
            gate_clients.append(client)
            client.settimeout(20)
            line = bytearray()
            while not line.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    raise AssertionError("CLI process disconnected before reporting at the gate")
                line.extend(chunk)
            reports.append(json.loads(bytes(line).decode("utf-8")))

        assert len({report["pid"] for report in reports}) == 2
        assert {report["db_path"] for report in reports} == {str(database.resolve())}
        assert {report["user_id"] for report in reports} == {user_id}
        assert {report["task_id"] for report in reports} == {recurring.id}
        for client in gate_clients:
            client.sendall(b"release\n")

        for process in processes:
            stdout, stderr = process.communicate(timeout=35)
            completed.append((process.returncode, stdout, stderr))
    finally:
        gate_server.close()
        for client in gate_clients:
            client.close()
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()

    assert len(completed) == 2
    assert [result[0] for result in completed] == [0, 0], completed
    create_matches = [
        re.fullmatch(
            rf"已创建下一期任务 #(\d+)：{re.escape(recurring.title)}\s*",
            stdout,
        )
        for _, stdout, _ in completed
    ]
    assert sum(match is not None for match in create_matches) == 1, completed
    already_done = f"任务 #{recurring.id} 已处于完成状态：{recurring.title}"
    assert sum(stdout.strip() == already_done for _, stdout, _ in completed) == 1, completed
    created_match = next(match for match in create_matches if match is not None)
    next_task_id = int(created_match.group(1))

    assert _done_event_count(database, recurring.id) == 1
    occurrences = _db_rows(
        database,
        "SELECT next_task_id FROM task_done_occurrences WHERE user_id = ? AND source_task_id = ?",
        (user_id, recurring.id),
    )
    assert len(occurrences) == 1
    assert occurrences[0]["next_task_id"] == next_task_id
    next_tasks = _db_rows(
        database,
        "SELECT id, status, recurrence FROM tasks WHERE user_id = ? AND title = ? AND id != ?",
        (user_id, recurring.title, recurring.id),
    )
    assert len(next_tasks) == 1
    assert next_tasks[0]["id"] == next_task_id
    assert next_tasks[0]["status"] == "todo"
    assert next_tasks[0]["recurrence"] == "daily"

    def _user_counts() -> tuple[int, int, int]:
        return (
            _count(database, "SELECT COUNT(*) AS n FROM tasks WHERE user_id = ?", (user_id,)),
            _count(
                database,
                "SELECT COUNT(*) AS n FROM task_events e JOIN tasks t ON t.id = e.task_id WHERE t.user_id = ?",
                (user_id,),
            ),
            _count(
                database,
                "SELECT COUNT(*) AS n FROM task_done_occurrences WHERE user_id = ?",
                (user_id,),
            ),
        )

    counts_before_rerun = _user_counts()
    repeated = _run_cli(
        database,
        user_id,
        tmp_path / "cli-logs-sequential-rerun",
        "done",
        str(recurring.id),
    )
    assert repeated.returncode == 0, repeated.stderr
    assert repeated.stdout.strip() == already_done
    assert "下一期" not in repeated.stdout and "创建" not in repeated.stdout
    assert _user_counts() == counts_before_rerun
    assert _done_event_count(database, recurring.id) == 1
    assert _same_title_task_count(database, user_id, recurring.title) == 2
    assert _count(
        database,
        "SELECT COUNT(*) AS n FROM task_done_occurrences WHERE user_id = ? AND source_task_id = ?",
        (user_id, recurring.id),
    ) == 1



def test_cli_recurring_completion_post_commit_output_loss_is_idempotent(tmp_path):
    database = tmp_path / "post-commit-output-loss.sqlite3"
    user_id = f"cli-post-commit-{uuid4().hex}"
    store = SQLiteTaskStore(database)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, "CLI post-commit test", "unused-test-hash", DUE.isoformat()),
        )
    recurring = store.create_task(
        "CLI post-commit daily recurring",
        due_at=DUE,
        recurrence="daily",
        user_id=user_id,
    )
    assert database.exists()
    assert store.db_path.resolve() == database.resolve()

    marker_path = tmp_path / "post-commit-marker.json"
    crash_hook_dir = tmp_path / "post-commit-crash-hook"
    dotenv_guard_dir = tmp_path / "dotenv-fail-fast-hook"
    crash_hook_dir.mkdir()
    dotenv_guard_dir.mkdir()
    (dotenv_guard_dir / "sitecustomize.py").write_text(
        """from momentum_agent import config as config_module


def _dotenv_disabled(*args, **kwargs):
    raise RuntimeError("dotenv access is disabled in this isolated CLI test")


config_module.load_dotenv = _dotenv_disabled
""",
        encoding="utf-8",
    )
    (crash_hook_dir / "sitecustomize.py").write_text(
        "MARKER_PATH = "
        + repr(str(marker_path))
        + "\n"
        + """import json
import os

from momentum_agent import config as config_module
from momentum_agent.storage.sqlite import SQLiteTaskStore


def _dotenv_disabled(*args, **kwargs):
    raise RuntimeError("dotenv access is disabled in this isolated CLI test")


config_module.load_dotenv = _dotenv_disabled

_original_complete_task_agent = SQLiteTaskStore.complete_task_agent


def _commit_then_exit(self, task_id, *, user_id):
    result = _original_complete_task_agent(self, task_id, user_id=user_id)
    marker = {
        "pid": os.getpid(),
        "db_path": str(self.db_path.resolve()),
        "user_id": user_id,
        "task_id": task_id,
        "commit_marker": "original_method_returned_post_commit",
    }
    with open(MARKER_PATH, "w", encoding="utf-8") as marker_file:
        json.dump(marker, marker_file, sort_keys=True)
        marker_file.write("\\n")
        marker_file.flush()
        os.fsync(marker_file.fileno())
    os._exit(86)


SQLiteTaskStore.complete_task_agent = _commit_then_exit
""",
        encoding="utf-8",
    )

    processes: list[subprocess.Popen[str]] = []

    def _run_cli(log_dir: Path, hook_path: Path) -> subprocess.CompletedProcess[str]:
        src_path = str(REPO_ROOT / "src")
        env = {
            "PATH": os.defpath,
            "HOME": str(tmp_path.resolve()),
            "LC_CTYPE": "C.UTF-8",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "MOMENTUM_DATABASE_URL": str(database.resolve()),
            "MOMENTUM_USER": user_id,
            "PYTHONPATH": os.pathsep.join((str(hook_path), src_path)),
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "momentum_agent.cli",
                "--db",
                str(database.resolve()),
                "--log-dir",
                str(log_dir),
                "done",
                str(recurring.id),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired as timeout_error:
            partial_stdout = timeout_error.stdout or ""
            partial_stderr = timeout_error.stderr or ""
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired as kill_timeout_error:
                    process.wait(timeout=5)
                    stdout = kill_timeout_error.stdout or partial_stdout
                    stderr = kill_timeout_error.stderr or partial_stderr
            raise AssertionError(
                "CLI subprocess timed out; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            ) from timeout_error
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    def _state_snapshot() -> tuple[tuple, tuple, tuple, int, int, int]:
        tasks = tuple(
            tuple(row)
            for row in _db_rows(
                database,
                "SELECT id, title, status, recurrence FROM tasks WHERE user_id = ? ORDER BY id",
                (user_id,),
            )
        )
        events = tuple(
            tuple(row)
            for row in _db_rows(
                database,
                "SELECT e.task_id, e.event_type, e.payload "
                "FROM task_events e JOIN tasks t ON t.id = e.task_id "
                "WHERE t.user_id = ? ORDER BY e.rowid",
                (user_id,),
            )
        )
        occurrences = tuple(
            tuple(row)
            for row in _db_rows(
                database,
                "SELECT user_id, source_task_id, next_task_id "
                "FROM task_done_occurrences WHERE user_id = ? ORDER BY source_task_id",
                (user_id,),
            )
        )
        return (
            tasks,
            events,
            occurrences,
            len(tasks),
            len(events),
            len(occurrences),
        )

    try:
        first = _run_cli(tmp_path / "post-commit-crash-logs", crash_hook_dir)
        first_pid = processes[-1].pid
        assert first.returncode == 86, (first.returncode, first.stdout, first.stderr)
        assert first.stdout == "", first.stdout
        assert "已创建下一期任务" not in first.stdout

        assert marker_path.is_file(), "post-commit marker was not durably written"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert marker == {
            "pid": first_pid,
            "db_path": str(database.resolve()),
            "user_id": user_id,
            "task_id": recurring.id,
            "commit_marker": "original_method_returned_post_commit",
        }

        source = _db_rows(database, "SELECT status FROM tasks WHERE id = ?", (recurring.id,))
        assert len(source) == 1 and source[0]["status"] == "done"
        assert _done_event_count(database, recurring.id) == 1
        occurrences = _db_rows(
            database,
            "SELECT next_task_id FROM task_done_occurrences "
            "WHERE user_id = ? AND source_task_id = ?",
            (user_id, recurring.id),
        )
        assert len(occurrences) == 1
        next_tasks = _db_rows(
            database,
            "SELECT id, status, recurrence FROM tasks "
            "WHERE user_id = ? AND title = ? AND id != ?",
            (user_id, recurring.title, recurring.id),
        )
        assert len(next_tasks) == 1
        assert next_tasks[0]["status"] == "todo"
        assert next_tasks[0]["recurrence"] == "daily"
        assert occurrences[0]["next_task_id"] == next_tasks[0]["id"]

        before_retry = _state_snapshot()
        assert len(before_retry[0]) == 2
        assert len(before_retry[2]) == 1
        assert sum(
            event == (recurring.id, "status_changed", "done")
            for event in before_retry[1]
        ) == 1

        retry_pythonpath = os.pathsep.join((str(dotenv_guard_dir), str(REPO_ROOT / "src")))
        assert str(crash_hook_dir) not in retry_pythonpath.split(os.pathsep)
        repeated = _run_cli(tmp_path / "post-commit-retry-logs", dotenv_guard_dir)
        assert repeated.returncode == 0, (repeated.stdout, repeated.stderr)
        assert repeated.stdout.strip() == (
            f"任务 #{recurring.id} 已处于完成状态：{recurring.title}"
        )
        assert "下一期" not in repeated.stdout and "创建" not in repeated.stdout
        assert _state_snapshot() == before_retry
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
