"""备份导出/导入与新加的 chat_history 记忆键：不能因为历史落库而破坏备份。

对话历史现在存在 user_memory 的 chat_history（JSON 字符串）。而备份 v1 要求 memory 的键值都是字符串、
v2 要求 memory_updated_at 与 memory 键集合完全一致，且凭据类键必须被排除——
这些都是历史落库引入后需要回归保证的。
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from momentum_agent import agent_app
from momentum_agent.storage import SQLiteTaskStore


@pytest.fixture(autouse=True)
def clean_memory_cache():
    agent_app._conversation_history.clear()
    yield
    agent_app._conversation_history.clear()


@pytest.fixture
def store():
    return SQLiteTaskStore(pathlib.Path(tempfile.mkdtemp()) / "backup-history.sqlite3")


def seed_history(store, user_id="default", turns=3):
    history = []
    for index in range(turns):
        history.append({"role": "user", "content": f"问题 {index}"})
        history.append({"role": "assistant", "content": f"回答 {index}"})
    agent_app._save_history(user_id, history, store)
    return history


def test_export_includes_chat_history_as_a_string(store):
    seed_history(store)
    data = store.export_user_data()
    memory = data["memory"]
    assert "chat_history" in memory, sorted(memory)
    assert isinstance(memory["chat_history"], str)
    assert "问题 0" in memory["chat_history"]
    # v2 的键集合必须一致
    assert set(data["memory_updated_at"]) == set(memory)


def test_export_import_round_trips_chat_history(store):
    seed_history(store)
    data = store.export_user_data()

    target = SQLiteTaskStore(pathlib.Path(tempfile.mkdtemp()) / "target.sqlite3")
    imported = target.import_user_data(data)
    assert imported >= 0

    agent_app._conversation_history.clear()
    restored = agent_app._get_history("default", target)
    assert [turn["content"] for turn in restored][:2] == ["问题 0", "回答 0"], restored


def test_restore_user_data_round_trips_chat_history(store):
    seed_history(store)
    data = store.export_user_data()

    target = SQLiteTaskStore(pathlib.Path(tempfile.mkdtemp()) / "restore.sqlite3")
    summary = target.restore_user_data(data)
    assert isinstance(summary, dict)

    agent_app._conversation_history.clear()
    assert agent_app._get_history("default", target), "恢复后历史应当还在"


def test_credential_keys_are_still_excluded_with_history_present(store):
    store.set_memory("api_key", "super-secret", user_id="default")
    seed_history(store)
    data = store.export_user_data()
    assert "api_key" not in data["memory"], sorted(data["memory"])
    assert data["excluded_memory"]["count"] >= 1
    assert "chat_history" in data["memory"]


def test_large_history_does_not_break_backup(store):
    seed_history(store, turns=200)
    data = store.export_user_data()
    payload = json.dumps(data, ensure_ascii=False)
    assert "chat_history" in data["memory"]
    assert len(payload) > 1000
    target = SQLiteTaskStore(pathlib.Path(tempfile.mkdtemp()) / "large.sqlite3")
    target.import_user_data(data)
    agent_app._conversation_history.clear()
    assert len(agent_app._get_history("default", target)) > 0


def test_history_is_trimmed_to_the_cap_before_backup(store):
    seed_history(store, turns=200)
    stored = json.loads(store.get_memory(agent_app.CHAT_HISTORY_MEMORY_KEY, user_id="default"))
    assert len(stored) <= agent_app.MAX_HISTORY_ITEMS

