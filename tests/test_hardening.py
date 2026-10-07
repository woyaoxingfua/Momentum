"""加固回归：优先级类型、登出空令牌、登录失败记录的有界性。"""
from __future__ import annotations

import json
from http import HTTPStatus
from unittest.mock import MagicMock

import pytest

from momentum_agent.models import Priority, TaskStatus
from momentum_agent.storage import MySQLTaskStore, SQLiteTaskStore


@pytest.fixture
def store(tmp_path):
    return SQLiteTaskStore(tmp_path / "hardening.db")


# ── 优先级：字符串不再在日志行里炸 AttributeError ────────────────

@pytest.mark.parametrize("value,expected", [("low", Priority.LOW), ("HIGH", Priority.HIGH), (" medium ", Priority.MEDIUM)])
def test_create_task_accepts_priority_strings(store, value, expected):
    task = store.create_task("写周报", priority=value)
    assert task.priority == expected
    assert store._get_task(task.id).priority == expected


def test_create_task_accepts_priority_enum_and_default(store):
    assert store.create_task("甲", priority=Priority.HIGH).priority == Priority.HIGH
    assert store.create_task("乙").priority == Priority.MEDIUM
    assert store._get_task(1).priority == Priority.HIGH


def test_create_task_rejects_unknown_priority_with_a_clear_error(store):
    with pytest.raises(ValueError) as error:
        store.create_task("写周报", priority="urgent")
    assert "无效优先级" in str(error.value)
    assert "urgent" in str(error.value)


def test_update_task_accepts_priority_strings(store):
    task = store.create_task("写周报")
    updated = store.update_task(task.id, priority="high", user_id="default")
    assert updated.priority == Priority.HIGH
    with pytest.raises(ValueError):
        store.update_task(task.id, priority="nope", user_id="default")


def test_both_backends_expose_the_same_priority_coercion():
    import inspect

    from momentum_agent.storage.sqlite import _coerce_priority

    assert _coerce_priority("high") is Priority.HIGH
    assert _coerce_priority(None) is Priority.MEDIUM
    for backend in (SQLiteTaskStore, MySQLTaskStore):
        signature = inspect.signature(backend.create_task)
        assert "priority" in signature.parameters
        assert inspect.signature(backend.logout_user).parameters["token"].annotation in (str, "str | None")


# ── 登出：空令牌不该崩 ──────────────────────────────────────────

def test_logout_user_tolerates_missing_token(store):
    store.logout_user(None)
    store.logout_user("")


def test_logout_user_still_removes_a_real_session(store):
    from momentum_agent.auth import hash_password

    store.register_user("alice", "Alice", hash_password("hardening-password"))
    token = store.login_user("alice", "hardening-password")
    assert store.validate_session(token) == "alice"
    store.logout_user(token)
    assert store.validate_session(token) is None


# ── 登录失败记录必须有界 ────────────────────────────────────────

class _MockHandler:
    def __init__(self, client_ip="127.0.0.1"):
        self._status = HTTPStatus.OK
        self._body = b""
        self.client_address = (client_ip, 12345)
        self.store = MagicMock()
        self.database_url = "sqlite:///:memory:"

    def send_json(self, payload, status=HTTPStatus.OK):
        self._status = status
        self._body = json.dumps(payload, ensure_ascii=False).encode()
        return payload

    def read_json(self):
        return {}


def test_login_attempt_records_stay_bounded():
    from momentum_agent.web import handlers as h

    h._login_attempts.clear()
    for index in range(h.MAX_TRACKED_LOGIN_CLIENTS + 64):
        h._record_login_failure(f"10.0.{index // 256}.{index % 256}", 0, now=1000.0 + index)
    assert len(h._login_attempts) <= h.MAX_TRACKED_LOGIN_CLIENTS
    h._login_attempts.clear()


def test_stale_login_attempt_records_are_pruned_but_active_lockouts_are_kept():
    from momentum_agent.web import handlers as h

    h._login_attempts.clear()
    h._record_login_failure("1.1.1.1", 0, now=1000.0)
    h._record_login_failure("2.2.2.2", h.MAX_LOGIN_ATTEMPTS - 1, now=1000.0)
    h._prune_login_attempts(now=1000.0 + h.LOGIN_LOCKOUT_SECONDS + 1)
    assert "1.1.1.1" not in h._login_attempts
    assert "2.2.2.2" not in h._login_attempts, "锁定早已过期，应一并清理"

    h._login_attempts.clear()
    h._record_login_failure("3.3.3.3", h.MAX_LOGIN_ATTEMPTS - 1, now=2000.0)
    h._prune_login_attempts(now=2000.0 + 10)
    assert "3.3.3.3" in h._login_attempts, "仍然生效的锁定不能被清掉"
    h._login_attempts.clear()


def test_five_failures_still_lock_the_client_out():
    from momentum_agent.web import handlers as h
    from momentum_agent.web.handlers import handle_login

    h._login_attempts.clear()
    handler = _MockHandler("192.168.5.5")
    handler.read_json = lambda: {"user_id": "alice", "password": "wrong"}
    handler.store.login_user.return_value = None
    for _ in range(h.MAX_LOGIN_ATTEMPTS - 1):
        handle_login(handler)
        assert handler._status == HTTPStatus.UNAUTHORIZED
    handle_login(handler)
    assert handler._status == HTTPStatus.TOO_MANY_REQUESTS
    assert "秒" in json.loads(handler._body)["error"]
    handle_login(handler)
    assert handler._status == HTTPStatus.TOO_MANY_REQUESTS, "锁定期内直接拒绝"
    h._login_attempts.clear()


def test_successful_login_clears_the_failure_record():
    from momentum_agent.web import handlers as h
    from momentum_agent.web.handlers import handle_login

    h._login_attempts.clear()
    handler = _MockHandler("192.168.6.6")
    handler.read_json = lambda: {"user_id": "alice", "password": "good"}
    handler.store.login_user.return_value = "token-value"
    handle_login(handler)
    assert handler._status == HTTPStatus.OK
    assert "192.168.6.6" not in h._login_attempts

