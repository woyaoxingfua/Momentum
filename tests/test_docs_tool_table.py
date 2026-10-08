"""防止文档与代码漂移：README 的工具清单必须和 tools/* 的真实数量一致。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
TOOLS_DIR = ROOT / "src" / "momentum_agent" / "agents" / "tools"

# README 表格里的类别名 -> 对应模块
CATEGORY_TO_MODULE = {
    "任务": "task_tools",
    "子任务": "subtask_tools",
    "关系 / 依赖": "relation_tools",
    "心跳": "heartbeat_tools",
    "洞察": "insight_tools",
    "专注": "focus_tools",
    "天气": "weather_tools",
    "扩展": "extra_tools",
}


def tool_names_in(module_stem: str) -> list[str]:
    tree = ast.parse((TOOLS_DIR / f"{module_stem}.py").read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if getattr(decorator, "id", None) == "function_tool":
                    names.append(node.name)
    return names


def readme_table() -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|", line)
        if match and match.group(1) in CATEGORY_TO_MODULE:
            counts[match.group(1)] = int(match.group(2))
    return counts


def test_readme_tool_counts_match_the_code():
    documented = readme_table()
    assert set(documented) == set(CATEGORY_TO_MODULE), f"README 工具表缺少类别：{set(CATEGORY_TO_MODULE) - set(documented)}"
    actual = {name: len(tool_names_in(module)) for name, module in CATEGORY_TO_MODULE.items()}
    assert documented == actual, f"README 工具数与代码不一致：README={documented} 代码={actual}"


def test_readme_total_tool_claim_matches_the_code():
    total = sum(len(tool_names_in(module)) for module in CATEGORY_TO_MODULE.values())
    text = README.read_text(encoding="utf-8")
    assert f"全部 {total} 个工具" in text, f"README 未声明「全部 {total} 个工具」"
    assert f"**{total} 个工具**" in text, f"README 摘要未声明 {total} 个工具"
    assert f"# {total} 个 function_tool 工厂" in text, f"目录树注释未同步为 {total}"


def test_mcp_exposes_every_documented_tool():
    import pathlib
    import tempfile

    from momentum_agent.mcp_server import build_all_tools
    from momentum_agent.storage import create_task_store

    url = "sqlite:///" + str(pathlib.Path(tempfile.mkdtemp()) / "docs.sqlite3")
    tools = build_all_tools(create_task_store(url), "default")
    names = {getattr(tool, "name", str(tool)) for tool in tools}
    documented = sum(len(tool_names_in(module)) for module in CATEGORY_TO_MODULE.values())
    assert len(names) == documented, f"MCP 实际暴露 {len(names)} 个，文档写 {documented} 个"
    for module in CATEGORY_TO_MODULE.values():
        for name in tool_names_in(module):
            assert name in names, f"{name} 没有真正暴露给 MCP"

