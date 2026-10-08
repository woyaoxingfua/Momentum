"""工具注册表与包内导入的一致性守卫。

起因：agent_app._build_agent 从 .agents 包导入 create_extra_tools，但包 __init__ 没导出它，
导致真实 AI 会话直接 ImportError；同时 agents/agent.py 里还留着与 extra_tools 重复的 7 个工具
定义，其中一个从错误模块导入 local_review。这些问题共同的根因是「没有任何测试检查
包内导入符号与工具注册表本身」——既有 agents 测试都把 Runner/_build_agent mock 掉了。
"""
from __future__ import annotations

import ast
import collections
import importlib
import importlib.util
import os
import pathlib
import tempfile

import pytest

from momentum_agent import agent_app
from momentum_agent.config import ProviderConfig
from momentum_agent.storage import create_task_store

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
PACKAGE = SOURCE_ROOT / "momentum_agent"


def test_every_intra_project_import_symbol_resolves():
    """静态检查包内每一处 from ... import ...，包括函数体里的延迟导入。"""
    checked = 0
    broken: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(SOURCE_ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
            package = ".".join(parts)
        else:
            package = ".".join(parts[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target = node.module or ""
            if node.level:
                try:
                    target = importlib.util.resolve_name("." * node.level + target, package)
                except ImportError as exc:
                    broken.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} 相对导入越界 "
                        f"(level={node.level}, package={package}): {exc}"
                    )
                    continue
            if not target or target.split(".")[0] != "momentum_agent":
                continue
            try:
                module = importlib.import_module(target)
            except Exception as exc:  # pragma: no cover - 失败即报错
                broken.append(f"{path.relative_to(ROOT)}:{node.lineno} 无法导入 {target}: {exc}")
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                checked += 1
                if hasattr(module, alias.name):
                    continue
                # from package import submodule 也是合法的，即使包的 __init__ 没有导入它
                try:
                    is_submodule = importlib.util.find_spec(f"{target}.{alias.name}") is not None
                except (ImportError, AttributeError, ValueError):
                    is_submodule = False
                if not is_submodule:
                    broken.append(f"{path.relative_to(ROOT)}:{node.lineno} {target} 缺少 {alias.name}")
    assert checked > 100, f"扫描到的导入太少（{checked}），检查逻辑可能失效"
    assert not broken, "包内导入符号缺失：\n" + "\n".join(broken)


@pytest.fixture
def store():
    url = "sqlite:///" + str(pathlib.Path(tempfile.mkdtemp()) / "tools.sqlite3")
    return create_task_store(url)


def build_main_agent(store, monkeypatch):
    monkeypatch.setenv("MOMENTUM_API_KEY", "stub-key")
    monkeypatch.setenv("MOMENTUM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("MOMENTUM_MODEL", "stub-model")
    provider = ProviderConfig(
        api_key="stub-key", base_url="http://127.0.0.1:9/v1", model="stub-model",
        disable_tracing=True, thinking=None, reasoning_effort=None, provider="openai",
    )
    return agent_app._build_agent(store, provider, object(), user_id="tool-audit")


def test_main_agent_tool_names_are_unique(store, monkeypatch):
    agent = build_main_agent(store, monkeypatch)
    names = [getattr(tool, "name", str(tool)) for tool in agent.tools]
    duplicates = [name for name, count in collections.Counter(names).items() if count > 1]
    assert not duplicates, f"同名工具会让模型无法确定调用哪个：{duplicates}"
    assert len(names) >= 30, f"主 Agent 工具数量异常偏少：{len(names)}"


def test_batch_tools_are_reachable_from_the_agent(store, monkeypatch):
    """批量工具此前只存在于一份没人调用的旧实现里，必须保证它们真的挂在 Agent 上。"""
    agent = build_main_agent(store, monkeypatch)
    names = {getattr(tool, "name", str(tool)) for tool in agent.tools}
    assert {"batch_complete_tasks", "batch_start_tasks"} <= names, sorted(names)


def test_legacy_create_agent_tools_delegates_without_duplicates(store):
    from momentum_agent.agents import create_agent_tools

    tools = create_agent_tools(store, user_id="tool-audit")
    names = [getattr(tool, "name", str(tool)) for tool in tools]
    duplicates = [name for name, count in collections.Counter(names).items() if count > 1]
    assert not duplicates, duplicates
    assert len(names) > 0


def test_tool_names_are_unique_across_all_tool_modules():
    """跨模块也不允许同名工具，否则合并两套实现时会静默产生歧义。"""
    seen: dict[str, list[str]] = collections.defaultdict(list)
    for path in sorted((PACKAGE / "agents" / "tools").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("def ") and stripped.endswith(":") is False:
                continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for decorator in node.decorator_list:
                    name = getattr(decorator, "id", None) or getattr(getattr(decorator, "func", None), "id", None)
                    if name == "function_tool":
                        seen[node.name].append(path.name)
    duplicates = {name: files for name, files in seen.items() if len(files) > 1}
    assert not duplicates, f"同一工具在多处定义：{duplicates}"

