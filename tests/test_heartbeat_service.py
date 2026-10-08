"""心跳：服务层封装必须与 store 的唯一实现保持一致。"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from momentum_agent.services import heartbeat as hb
from momentum_agent.storage import create_task_store


@pytest.fixture
def store():
    return create_task_store("sqlite:///" + str(pathlib.Path(tempfile.mkdtemp()) / "hb.sqlite3"))


def test_set_config_goes_through_the_store(store):
    returned = hb.set_config(store, enabled=True, start_hour=99, end_hour=-5, interval_hours=99)
    stored = store.get_heartbeat_config(user_id="default")
    assert returned == stored
    assert stored["enabled"] is True
    assert (stored["start_hour"], stored["end_hour"], stored["interval_hours"]) == (23, 0, 24)


def test_update_last_heartbeat_is_visible_to_the_store(store):
    hb.set_config(store, enabled=True, start_hour=0, end_hour=23, interval_hours=1)
    updated = hb.update_last_heartbeat(store)
    assert updated["last_heartbeat_at"]
    assert store.get_heartbeat_config(user_id="default")["last_heartbeat_at"] == updated["last_heartbeat_at"]


def test_should_trigger_matches_the_store_decision(store):
    hb.set_config(store, enabled=True, start_hour=0, end_hour=23, interval_hours=1)
    assert hb.should_trigger(store) == store.should_trigger_heartbeat(user_id="default") is True
    hb.update_last_heartbeat(store)
    assert hb.should_trigger(store) == store.should_trigger_heartbeat(user_id="default") is False


def test_stats_counts_tasks_and_overdue(store):
    from datetime import datetime, timedelta, timezone

    store.create_task("待办")
    doing = store.create_task("进行中")
    store.start_task(doing.id)
    store.create_task("逾期", due_at=datetime.now(timezone.utc) - timedelta(days=1))
    store.create_task("今天到期", due_at=datetime.now(timezone.utc) + timedelta(hours=2))
    done = store.create_task("完成")
    store.complete_task_agent(done.id, user_id="default")

    counts = hb.stats(store)
    assert counts["total"] == 5
    assert counts["doing"] == 1
    assert counts["done"] == 1
    assert counts["overdue"] == 1
    assert counts["upcoming_24h"] == 1

