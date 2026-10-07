"""sqlite:// URL 解析：平台差异必须有测试兜住。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from momentum_agent.storage.factory import sqlite_path_from_url


def test_memory_url():
    assert sqlite_path_from_url("sqlite:///:memory:") == ":memory:"


def test_posix_forms_are_unchanged():
    assert sqlite_path_from_url("sqlite:////tmp/momentum.db") == "//tmp/momentum.db"
    assert sqlite_path_from_url("sqlite:///tmp/momentum.db") == "/tmp/momentum.db"


def test_bare_paths_pass_through():
    assert sqlite_path_from_url(".momentum/tasks.db") == ".momentum/tasks.db"


def test_windows_drive_letter_url():
    assert sqlite_path_from_url("sqlite:///C:/data/tasks.db") == "C:/data/tasks.db"


@pytest.mark.skipif(os.name != "nt", reason="Windows 专用")
def test_rooted_windows_path_does_not_become_a_unc_path():
    # 调用方在 Windows 上用 Path("/tmp/...")，拼出来就是 sqlite:///\\tmp\\...
    resolved = sqlite_path_from_url("sqlite:///\\tmp\\momentum-e2e\\isolated.sqlite3")
    assert resolved == "\\tmp\\momentum-e2e\\isolated.sqlite3"
    text = str(Path(resolved))
    assert not text.startswith(os.sep + os.sep), text
    assert Path(resolved).parent == Path("\\tmp\\momentum-e2e")


def test_non_sqlite_url_is_rejected():
    with pytest.raises(ValueError):
        sqlite_path_from_url("mysql://user@host/db")

