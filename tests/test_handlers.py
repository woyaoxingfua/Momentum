"""Test web handlers — auth, rate limiting, security validations."""

import json
from http import HTTPStatus
from unittest.mock import MagicMock

import pytest


class MockHandler:
    """Minimal mock for MomentumHandler."""

    def __init__(self):
        self._status = HTTPStatus.OK
        self._body = b""
        self._headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.store = MagicMock()
        self.database_url = "sqlite:///:memory:"
        self.rfile = MagicMock()

    def send_json(self, payload, status=HTTPStatus.OK):
        self._status = status
        self._body = json.dumps(payload, ensure_ascii=False).encode()
        return payload

    def read_json(self):
        return {}


class TestHandleRegister:
    def test_register_requires_password(self):
        from momentum_agent.web.handlers import handle_register

        handler = MockHandler()
        handler.read_json = lambda: {"user_id": "alice", "password": ""}

        handle_register(handler)
        assert handler._status == HTTPStatus.BAD_REQUEST

    def test_register_requires_username(self):
        from momentum_agent.web.handlers import handle_register

        handler = MockHandler()
        handler.read_json = lambda: {"user_id": "", "password": "password123"}

        handle_register(handler)
        assert handler._status == HTTPStatus.BAD_REQUEST

    def test_register_password_minimum_length(self):
        from momentum_agent.web.handlers import handle_register

        handler = MockHandler()
        handler.read_json = lambda: {"user_id": "alice", "password": "1234567"}

        handle_register(handler)
        assert handler._status == HTTPStatus.BAD_REQUEST
        assert "8" in str(handler._body)  # Error mentions "8位"

    def test_register_username_minimum_length(self):
        from momentum_agent.web.handlers import handle_register

        handler = MockHandler()
        handler.read_json = lambda: {"user_id": "a", "password": "password123"}

        handle_register(handler)
        assert handler._status == HTTPStatus.BAD_REQUEST

    def test_register_username_maximum_length(self):
        from momentum_agent.web.handlers import handle_register

        handler = MockHandler()
        handler.read_json = lambda: {"user_id": "a" * 100, "password": "password123"}

        handle_register(handler)
        assert handler._status == HTTPStatus.BAD_REQUEST

    def test_register_success(self):
        from momentum_agent.web.handlers import handle_register

        handler = MockHandler()
        handler.read_json = lambda: {"user_id": "alice", "password": "password123", "display_name": "Alice"}
        handler.store.register_user.return_value = None

        handle_register(handler)
        assert handler._status == HTTPStatus.OK

    def test_register_duplicate_user(self):
        from momentum_agent.web.handlers import handle_register

        handler = MockHandler()
        handler.read_json = lambda: {"user_id": "alice", "password": "password123"}
        handler.store.register_user.side_effect = Exception("UNIQUE constraint failed")

        handle_register(handler)
        assert handler._status == HTTPStatus.CONFLICT


class TestHandleLogin:
    def test_login_requires_fields(self):
        from momentum_agent.web.handlers import handle_login

        handler = MockHandler()
        handler.read_json = lambda: {"user_id": "", "password": ""}

        handle_login(handler)
        assert handler._status == HTTPStatus.BAD_REQUEST

    def test_login_wrong_password(self):
        from momentum_agent.web.handlers import handle_login, _login_attempts

        handler = MockHandler()
        handler.read_json = lambda: {"user_id": "alice", "password": "wrong"}
        handler.store.login_user.return_value = None  # Login fails

        # Clear any previous lockouts for this IP
        client_ip = handler.client_address[0]
        _login_attempts.pop(client_ip, None)

        handle_login(handler)
        assert handler._status == HTTPStatus.UNAUTHORIZED

    def test_login_success(self):
        from momentum_agent.web.handlers import handle_login, _login_attempts

        handler = MockHandler()
        handler.read_json = lambda: {"user_id": "alice", "password": "password123"}
        handler.store.login_user.return_value = "valid_token_abc123"

        client_ip = handler.client_address[0]
        _login_attempts.pop(client_ip, None)

        handle_login(handler)
        assert handler._status == HTTPStatus.OK
        body = json.loads(handler._body)
        assert body["token"] == "valid_token_abc123"
        assert body["user_id"] == "alice"

    def test_login_lockout_after_5_attempts(self):
        from momentum_agent.web import handlers as h
        from momentum_agent.web.handlers import handle_login
        import time

        # Clear state first
        client_ip = "192.168.1.100"
        if client_ip in h._login_attempts:
            del h._login_attempts[client_ip]

        # Simulate 4 previous failed attempts (so 5th will trigger lockout)
        h._login_attempts[client_ip] = (4, 0.0)

        handler = MockHandler()
        handler.client_address = (client_ip, 12345)
        handler.read_json = lambda: {"user_id": "alice", "password": "wrong"}
        handler.store.login_user.return_value = None

        handle_login(handler)
        # 4 attempts + 1 = 5 >= 5 → should lock out
        body = json.loads(handler._body)
        assert "秒" in body["error"]
        assert handler._status == HTTPStatus.TOO_MANY_REQUESTS

        # Cleanup
        del h._login_attempts[client_ip]


class TestHandleChangePassword:
    def test_change_password_requires_both(self):
        from momentum_agent.web.handlers import handle_change_password

        handler = MockHandler()
        handler.read_json = lambda: {"old_password": "", "new_password": "newpass123"}

        handle_change_password(handler, "alice")
        assert handler._status == HTTPStatus.BAD_REQUEST

    def test_change_password_minimum_length(self):
        from momentum_agent.web.handlers import handle_change_password

        handler = MockHandler()
        handler.read_json = lambda: {"old_password": "oldpass123", "new_password": "short"}

        handle_change_password(handler, "alice")
        assert handler._status == HTTPStatus.BAD_REQUEST
        assert "8" in str(handler._body)


class TestSecurityRateLimiting:
    def test_login_rate_limit_module_vars_exist(self):
        from momentum_agent.web import handlers

        assert hasattr(handlers, "_login_attempts")
        assert hasattr(handlers, "MAX_LOGIN_ATTEMPTS")
        assert handlers.MAX_LOGIN_ATTEMPTS == 5

    def test_login_rate_limit_lockout_time(self):
        from momentum_agent.web import handlers

        assert handlers.LOGIN_LOCKOUT_SECONDS >= 300


class TestRequestBodySizeLimit:
    def test_server_has_max_body_limit(self):
        from momentum_agent.web.server import MAX_REQUEST_BODY

        assert MAX_REQUEST_BODY == 2 * 1024 * 1024
