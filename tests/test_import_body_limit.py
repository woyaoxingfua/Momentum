from __future__ import annotations

import json
import re
import socket
import sqlite3
import threading
from http.server import ThreadingHTTPServer

import pytest

from momentum_agent.auth import hash_password
from momentum_agent.storage import SQLiteTaskStore
from momentum_agent.web.server import (
    MAX_BACKUP_SIZE_BYTES,
    MAX_REQUEST_BODY,
    MomentumHandler,
    _store_cache,
)


def _database_snapshot(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            table: tuple(
                tuple(row)
                for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            )
            for table in tables
        }


@pytest.mark.parametrize(
    ("request_path", "max_body_size"),
    [
        ("/api/import", MAX_BACKUP_SIZE_BYTES),
        ("/api/tasks", MAX_REQUEST_BODY),
    ],
    ids=["backup-import-16-mib", "regular-json-api-2-mib"],
)
def test_oversized_json_request_sends_one_413_and_does_not_change_database(
    tmp_path, request_path, max_body_size
):
    database_path = tmp_path / f"oversized-{max_body_size}.sqlite3"
    database_url = str(database_path)
    store = SQLiteTaskStore(database_path)
    store.register_user("recipient", "Recipient", hash_password("oversized-test-password"))
    store.create_task("keep this task", user_id="recipient")
    store.set_memory("unchanged", "yes", user_id="recipient")
    token = store.login_user("recipient", "oversized-test-password")
    assert token

    before_snapshot = _database_snapshot(database_path)
    before_file = database_path.read_bytes()

    configured_handler = type(
        "OversizedImportTestHandler",
        (MomentumHandler,),
        {"database_url": database_url},
    )
    _store_cache.pop(database_url, None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), configured_handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        if request_path == "/api/import":
            payload = {"data": {"notes": "x" * max_body_size}}
        else:
            payload = {"text": "x" * max_body_size}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        assert len(body) > max_body_size
        request_headers = (
            f"POST {request_path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{httpd.server_port}\r\n"
            f"Authorization: Bearer {token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode("ascii")

        response_parts = []
        with socket.create_connection(("127.0.0.1", httpd.server_port), timeout=10) as client:
            client.settimeout(10)
            client.sendall(request_headers)
            try:
                client.sendall(body)
            except OSError:
                # The server is allowed to close as soon as Content-Length proves
                # the request is oversized; its single error response is still read below.
                pass
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response_parts.append(chunk)
        wire_response = b"".join(response_parts)

        status_lines = re.findall(rb"(?m)^HTTP/1\.1 \d{3}[^\r\n]*", wire_response)
        assert len(status_lines) == 1, wire_response[:2000]
        assert status_lines[0].startswith(b"HTTP/1.1 413 ")
        header_bytes, response_body = wire_response.split(b"\r\n\r\n", 1)
        response_headers = {}
        for line in header_bytes.split(b"\r\n")[1:]:
            name, value = line.split(b":", 1)
            response_headers[name.strip().lower()] = value.strip()
        assert response_headers[b"content-type"] == b"application/json; charset=utf-8"
        assert int(response_headers[b"content-length"]) == len(response_body)
        assert response_headers[b"connection"].lower() == b"close"
        payload = json.loads(response_body)
        assert "请求体过大" in payload["error"]
        assert f"{max_body_size // 1024 // 1024} MiB" in payload["error"]

        assert _database_snapshot(database_path) == before_snapshot
        assert database_path.read_bytes() == before_file
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        _store_cache.pop(database_url, None)
