from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Barrier, get_ident
from uuid import uuid4

import pytest

from momentum_agent.agents.tools.task_tools import create_task_tools
from momentum_agent.models import TaskStatus
from momentum_agent.mcp_server import _invoke_function_tool
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.storage.mysql import MySQLTaskStore


DUE = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def user_store(tmp_path):
    store = SQLiteTaskStore(tmp_path / "agent-completion.sqlite3")
    user_id = f"agent-test-{uuid4().hex}"
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, "Agent test", "unused-test-hash", DUE.isoformat()),
        )
    return store, user_id


def _tool(store, user_id):
    return next(tool for tool in create_task_tools(store, user_id) if tool.name == "complete_task")


def _invoke_tool(tool, task_id):
    return asyncio.run(_invoke_function_tool(tool, {"task_id": task_id}))


def _events(store, task_id=None, event_type=None):
    clauses = []
    params = []
    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with store._connect() as conn:
        return conn.execute(
            f"SELECT id, task_id, event_type, payload FROM task_events {where} ORDER BY id", params
        ).fetchall()


def _count(store, table, where="", params=()):
    with store._connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where}", params).fetchone()
    return int(row["n"])


def _occurrences(store, user_id):
    with store._connect() as conn:
        return conn.execute(
            "SELECT * FROM task_done_occurrences WHERE user_id = ? ORDER BY source_done_event_id",
            (user_id,),
        ).fetchall()


def test_agent_tool_recurring_completion_and_ordered_repeat_create_one_occurrence(user_store):
    store, user_id = user_store
    task = store.create_task("Agent daily", due_at=DUE, recurrence="daily", user_id=user_id)
    tool = _tool(store, user_id)

    first_response = _invoke_tool(tool, task.id)
    second_response = _invoke_tool(tool, task.id)

    assert "已创建下一期任务 #" in first_response
    next_task_id = int(re.search(r"下一期任务 #(\d+)", first_response).group(1))
    assert second_response == f"任务 #{task.id} 已处于完成状态：Agent daily"
    assert "创建" not in second_response and "下一期" not in second_response
    assert store.get_task_for_user(task.id, user_id).status == TaskStatus.DONE
    done_events = [event for event in _events(store, task.id) if event["event_type"] == "status_changed"]
    assert len(done_events) == 1
    assert done_events[0]["payload"] == TaskStatus.DONE.value
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", (user_id, "Agent daily")) == 2
    occurrences = _occurrences(store, user_id)
    assert len(occurrences) == 1
    assert occurrences[0]["source_task_id"] == task.id
    assert occurrences[0]["source_done_event_id"] == done_events[0]["id"]
    assert occurrences[0]["next_task_id"] == next_task_id
    assert json.loads(occurrences[0]["response_json"])["message"].startswith("已创建下一期任务 #")
    # Agent 没有稳定 request ID，因此不伪造 idempotency ledger key。
    assert _count(store, "task_done_idempotency") == 0


def test_agent_concurrent_repeated_completion_creates_one_transition_and_next(user_store):
    store, user_id = user_store
    task = store.create_task("Concurrent daily", due_at=DUE, recurrence="daily", user_id=user_id)
    count = 8
    barrier = Barrier(count)

    def complete(_index):
        barrier.wait(timeout=10)
        return store.complete_task_agent(task.id, user_id=user_id)

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(complete, range(count)))

    assert sum(transitioned for _task, transitioned, _next in results) == 1
    assert sum(next_task is not None for _task, _transitioned, next_task in results) == 1
    assert len([event for event in _events(store, task.id) if event["event_type"] == "status_changed"]) == 1
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", (user_id, "Concurrent daily")) == 2
    assert len(_occurrences(store, user_id)) == 1


def test_agent_tool_concurrent_recurring_completion_creates_one_next_task(user_store):
    store, user_id = user_store
    task = store.create_task("Concurrent tool daily", due_at=DUE, recurrence="daily", user_id=user_id)
    tool = _tool(store, user_id)
    barrier = Barrier(2)

    def invoke():
        caller_id = get_ident()
        barrier.wait(timeout=10)
        return caller_id, _invoke_tool(tool, task.id)

    executor = ThreadPoolExecutor(max_workers=2)
    futures = [executor.submit(invoke) for _ in range(2)]
    try:
        results = [future.result(timeout=30) for future in futures]
    finally:
        barrier.abort()
        executor.shutdown(wait=True, cancel_futures=True)

    assert len({caller_id for caller_id, _response in results}) == 2
    responses = [response for _caller_id, response in results]
    assert len(responses) == 2
    create_responses = [response for response in responses if "已创建下一期任务 #" in response]
    no_op_response = f"任务 #{task.id} 已处于完成状态：Concurrent tool daily"
    assert len(create_responses) == 1
    assert responses.count(no_op_response) == 1
    next_task_match = re.search(r"已创建下一期任务 #(\d+)", create_responses[0])
    assert next_task_match is not None
    next_task_id = int(next_task_match.group(1))

    assert store.get_task_for_user(task.id, user_id).status == TaskStatus.DONE
    done_events = [
        event for event in _events(store, task.id)
        if event["event_type"] == "status_changed" and event["payload"] == TaskStatus.DONE.value
    ]
    assert len(done_events) == 1
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", (user_id, "Concurrent tool daily")) == 2
    next_task = store.get_task_for_user(next_task_id, user_id)
    assert next_task is not None
    assert next_task.status == TaskStatus.TODO
    assert next_task.recurrence == "daily"
    assert len(_events(store, next_task_id, "created")) == 1

    occurrences = _occurrences(store, user_id)
    assert len(occurrences) == 1
    assert occurrences[0]["source_task_id"] == task.id
    assert occurrences[0]["source_done_event_id"] == done_events[0]["id"]
    assert occurrences[0]["next_task_id"] == next_task_id
    assert _count(store, "task_done_idempotency") == 0


def test_agent_reopen_then_real_done_transition_creates_second_occurrence(user_store):
    store, user_id = user_store
    task = store.create_task("Reopen daily", due_at=DUE, recurrence="daily", user_id=user_id)
    first_task, first_transition, first_next = store.complete_task_agent(task.id, user_id=user_id)
    assert first_task.status == TaskStatus.DONE and first_transition and first_next is not None

    reopened = store.reopen_task(task.id, user_id=user_id)
    assert reopened.status == TaskStatus.TODO
    second_task, second_transition, second_next = store.complete_task_agent(task.id, user_id=user_id)

    assert second_task.status == TaskStatus.DONE and second_transition and second_next is not None
    done_events = [event for event in _events(store, task.id) if event["event_type"] == "status_changed" and event["payload"] == "done"]
    assert len(done_events) == 2
    assert len(_occurrences(store, user_id)) == 2
    assert [row["source_done_event_id"] for row in _occurrences(store, user_id)] == [event["id"] for event in done_events]
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", (user_id, "Reopen daily")) == 3


def test_agent_legacy_done_without_mapping_is_not_backfilled_or_recurred(user_store):
    store, user_id = user_store
    task = store.create_task("Legacy daily", due_at=DUE, recurrence="daily", user_id=user_id)
    store.update_status(task.id, TaskStatus.DONE, user_id=user_id)
    events_before = _events(store, task.id)
    tasks_before = _count(store, "tasks", "WHERE user_id = ?", (user_id,))
    tool = _tool(store, user_id)

    response = _invoke_tool(tool, task.id)

    assert response == f"任务 #{task.id} 已处于完成状态：Legacy daily"
    assert "下一期" not in response
    assert _events(store, task.id) == events_before
    assert _count(store, "tasks", "WHERE user_id = ?", (user_id,)) == tasks_before
    assert _occurrences(store, user_id) == []


def test_agent_parent_completion_creates_next_only_for_explicit_recurring_target(user_store):
    store, user_id = user_store
    parent = store.create_task("Explicit parent", due_at=DUE, recurrence="daily", user_id=user_id)
    child = store.create_task(
        "Cascaded child", due_at=DUE, recurrence="weekly", parent_task_id=parent.id, user_id=user_id
    )
    grandchild = store.create_task(
        "Cascaded grandchild", due_at=DUE, recurrence="monthly", parent_task_id=child.id, user_id=user_id
    )

    completed, transitioned, next_task = store.complete_task_agent(parent.id, user_id=user_id)

    assert transitioned and completed.status == TaskStatus.DONE and next_task is not None
    assert store.get_task_for_user(child.id, user_id).status == TaskStatus.DONE
    assert store.get_task_for_user(grandchild.id, user_id).status == TaskStatus.DONE
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", (user_id, "Explicit parent")) == 2
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", (user_id, "Cascaded child")) == 1
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", (user_id, "Cascaded grandchild")) == 1
    assert [row["source_task_id"] for row in _occurrences(store, user_id)] == [parent.id]
    assert len([event for event in _events(store, child.id) if event["event_type"] == "status_changed"]) == 1
    assert len([event for event in _events(store, grandchild.id) if event["event_type"] == "status_changed"]) == 1


def test_agent_child_completion_does_not_create_next_for_cascaded_recurring_parent(user_store):
    store, user_id = user_store
    parent = store.create_task("Auto parent", due_at=DUE, recurrence="daily", user_id=user_id)
    child = store.create_task(
        "Explicit child", due_at=DUE, recurrence="weekly", parent_task_id=parent.id, user_id=user_id
    )

    completed, transitioned, next_task = store.complete_task_agent(child.id, user_id=user_id)

    assert transitioned and completed.status == TaskStatus.DONE and next_task is not None
    assert store.get_task_for_user(parent.id, user_id).status == TaskStatus.DONE
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", (user_id, "Auto parent")) == 1
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", (user_id, "Explicit child")) == 2
    assert [row["source_task_id"] for row in _occurrences(store, user_id)] == [child.id]
    assert len([event for event in _events(store, parent.id) if event["event_type"] == "status_changed"]) == 1


@pytest.mark.parametrize("failure_point", ["next", "mapping"])
def test_agent_occurrence_completion_rolls_back_next_and_mapping_failures(user_store, failure_point):
    store, user_id = user_store
    title = f"rollback-{failure_point}"
    task = store.create_task(title, due_at=DUE, recurrence="daily", user_id=user_id)
    events_before = _events(store, task.id)
    trigger = "fail_agent_next" if failure_point == "next" else "fail_agent_mapping"
    with store._connect() as conn:
        if failure_point == "next":
            conn.execute(
                f"""CREATE TRIGGER {trigger} BEFORE INSERT ON tasks
                    WHEN NEW.title = '{title}' AND NEW.status = 'todo'
                    BEGIN SELECT RAISE(ABORT, 'injected next failure'); END"""
            )
        else:
            conn.execute(
                f"""CREATE TRIGGER {trigger} BEFORE INSERT ON task_done_occurrences
                    BEGIN SELECT RAISE(ABORT, 'injected mapping failure'); END"""
            )

    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.complete_task_agent(task.id, user_id=user_id)

    assert store.get_task_for_user(task.id, user_id).status == TaskStatus.TODO
    assert _events(store, task.id) == events_before
    assert _count(store, "tasks", "WHERE user_id = ? AND title = ?", (user_id, title)) == 1
    assert _occurrences(store, user_id) == []
    assert _count(store, "task_done_idempotency") == 0


def _mock_mysql_agent_store(monkeypatch):
    """Small in-memory MySQL driver simulation; it never opens a database connection."""
    store = MySQLTaskStore.__new__(MySQLTaskStore)
    state = {
        "tasks": {
            41: {
                "id": 41, "title": "mysql daily", "status": "todo", "priority": "medium",
                "due_at": DUE.isoformat(), "estimated_minutes": 20, "notes": None,
                "parent_task_id": None, "recurrence": "daily", "user_id": "mock-user",
                "created_at": DUE.isoformat(), "updated_at": DUE.isoformat(), "tags": None,
            }
        },
        "events": [], "occurrences": [], "queries": [], "next_task_id": 100, "next_event_id": 500,
        "commits": 0, "rollbacks": 0,
    }

    class Cursor:
        def __init__(self, *, one=None, rows=None, rowcount=0):
            self.one = dict(one) if isinstance(one, dict) else one
            self.rows = [dict(row) for row in (rows or [])]
            self.rowcount = rowcount

        def fetchone(self):
            return dict(self.one) if isinstance(self.one, dict) else self.one

        def fetchall(self):
            return [dict(row) for row in self.rows]

    class Connection:
        def __init__(self):
            self.snapshot = None
            self.last_id = 0

        def begin(self):
            self.snapshot = json.loads(json.dumps({
                "tasks": state["tasks"], "events": state["events"], "occurrences": state["occurrences"],
                "next_task_id": state["next_task_id"], "next_event_id": state["next_event_id"],
            }))

        def insert_id(self):
            return self.last_id

        def commit(self):
            if self.snapshot is not None:
                self.snapshot = None
                state["commits"] += 1

        def rollback(self):
            if self.snapshot is not None:
                for key, value in self.snapshot.items():
                    state[key] = value
                self.snapshot = None
                state["rollbacks"] += 1

    @contextmanager
    def fake_connect():
        conn = Connection()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def fake_execute(conn, sql, params=None):
        normalized = " ".join(sql.split())
        params = tuple(params or ())
        state["queries"].append((normalized, params))
        if normalized.startswith("SELECT parent_task_id FROM tasks WHERE id = %s AND user_id = %s"):
            task_id, owner = params
            row = state["tasks"].get(task_id)
            return Cursor(one={"parent_task_id": row["parent_task_id"]} if row and row["user_id"] == owner else None)
        if normalized.startswith("SELECT * FROM tasks WHERE id = %s AND user_id = %s"):
            task_id, owner = params
            row = state["tasks"].get(task_id)
            return Cursor(one=row if row and row["user_id"] == owner else None)
        if normalized.startswith("SELECT id FROM tasks WHERE id = %s AND user_id = %s FOR UPDATE"):
            task_id, owner = params
            row = state["tasks"].get(task_id)
            return Cursor(one={"id": task_id} if row and row["user_id"] == owner else None)
        if normalized.startswith("SELECT id FROM tasks WHERE parent_task_id = %s AND status != %s AND user_id = %s"):
            parent_id, done_status, owner = params
            children = [
                {"id": row["id"]} for row in state["tasks"].values()
                if row["parent_task_id"] == parent_id and row["status"] != done_status and row["user_id"] == owner
            ]
            return Cursor(rows=children)
        if normalized.startswith("UPDATE tasks SET status = %s"):
            status, updated_at, task_id, owner = params
            row = state["tasks"].get(task_id)
            if row and row["user_id"] == owner and row["status"] != status:
                row["status"] = status
                row["updated_at"] = updated_at
                return Cursor(rowcount=1)
            return Cursor()
        if normalized.startswith("INSERT INTO task_events"):
            state["events"].append(params)
            conn.last_id = state["next_event_id"]
            state["next_event_id"] += 1
            return Cursor(rowcount=1)
        if normalized.startswith("INSERT INTO tasks ("):
            (title, status, priority, due_at, estimated_minutes, notes, parent_task_id,
             recurrence, owner, created_at, updated_at, tags) = params
            task_id = state["next_task_id"]
            state["next_task_id"] += 1
            state["tasks"][task_id] = {
                "id": task_id, "title": title, "status": status, "priority": priority,
                "due_at": due_at, "estimated_minutes": estimated_minutes, "notes": notes,
                "parent_task_id": parent_task_id, "recurrence": recurrence, "user_id": owner,
                "created_at": created_at, "updated_at": updated_at, "tags": tags,
            }
            conn.last_id = task_id
            return Cursor(rowcount=1)
        if normalized.startswith("INSERT INTO task_done_occurrences"):
            state["occurrences"].append(params)
            return Cursor(rowcount=1)
        raise AssertionError(f"unexpected SQL in mock MySQL store: {normalized}")

    monkeypatch.setattr(store, "_connect", fake_connect)
    monkeypatch.setattr(store, "_execute", fake_execute)
    return store, state


def test_mysql_agent_completion_is_mocked_and_occurrence_safe_without_real_mysql(monkeypatch):
    store, state = _mock_mysql_agent_store(monkeypatch)

    first_task, transitioned, next_task = store.complete_task_agent(41, user_id="mock-user")
    second_task, transitioned_again, next_task_again = store.complete_task_agent(41, user_id="mock-user")

    assert transitioned and first_task.status == TaskStatus.DONE and next_task is not None
    assert not transitioned_again and second_task.status == TaskStatus.DONE and next_task_again is None
    assert next_task.id == 100
    assert len([event for event in state["events"] if event[1] == "status_changed"]) == 1
    assert len([event for event in state["events"] if event[1] == "created"]) == 1
    assert len(state["occurrences"]) == 1
    assert state["occurrences"][0][0:4] == ("mock-user", 41, 500, 100)
    assert not any("task_done_idempotency" in sql for sql, _params in state["queries"])
    assert state["commits"] == 2
    assert state["rollbacks"] == 0
