"""Setup Wizard — 终端 TUI 配置向导。

用法：
  momentum-agent init                          # 交互模式（默认）
  momentum-agent init --non-interactive        # 用全部默认值+已有配置，不提问
  momentum-agent init --db URL                 # 跳过 DB 选择步
  momentum-agent init --skip-db-check           # 跳过 DB 连接测试

依赖 questionary 和 rich（可选依赖组 [wizard]）。
未安装时会给出友好提示。
"""
from __future__ import annotations

import os
import re
import socket
import urllib.parse as urlparse
from dataclasses import dataclass, field
from pathlib import Path

from .logger import get_logger

log = get_logger("wizard")

# ═══════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════


@dataclass
class WizardResult:
    """向导跑完的结果汇总。"""

    database_url: str | None = None
    default_password_changed: bool = False
    new_admin_user: str | None = None
    env_updates: dict[str, str] = field(default_factory=dict)
    user_config_updates: dict[str, str] = field(default_factory=dict)
    web_host: str | None = None
    web_port: int | None = None
    mcp_host: str | None = None
    mcp_port: int | None = None
    mcp_sse_enabled: bool = False
    started_server: bool = False


# ═══════════════════════════════════════════════════════════════════
# .env 文件读写
# ═══════════════════════════════════════════════════════════════════

_ENV_HEADER = """# ============================================
# Momentum Task Agent - 环境配置
# 由 momentum-agent init 生成
# ============================================

"""


def _env_path() -> Path:
    return Path.cwd() / ".env"


def read_env_file() -> dict[str, str]:
    """读取 .env 为 dict（KEY=VALUE），文件不存在返回空。"""
    path = _env_path()
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def write_env_file(updates: dict[str, str]) -> None:
    """更新 .env：保留注释和未涉及的行，更新/追加指定 key。

    空字符串值的 key 会被跳过（用于「回车保留」）。
    """
    if not updates:
        return
    path = _env_path()

    # 过滤掉空值（回车保留的项）
    effective = {k: v for k, v in updates.items() if v}

    if not path.exists():
        # 新建
        content = _ENV_HEADER
        for k, v in effective.items():
            content += f"{k}={v}\n"
        path.write_text(content, encoding="utf-8")
        return

    # 已存在：逐行处理
    existing_lines = path.read_text(encoding="utf-8").splitlines()
    updated_keys: set[str] = set()
    new_lines: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" not in stripped:
            new_lines.append(line)
            continue
        key, _, _ = stripped.partition("=")
        key = key.strip()
        if key in effective:
            new_lines.append(f"{key}={effective[key]}")
            updated_keys.add(key)
        else:
            new_lines.append(line)

    # 追加新 key
    for k, v in effective.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════
# 校验工具
# ═══════════════════════════════════════════════════════════════════


def is_port_available(host: str, port: int) -> bool:
    """检测端口是否可用。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.bind((host, port))
        return True
    except (OSError, socket.error):
        return False


def parse_db_url(url: str) -> dict:
    """解析 DB URL 为各组成部分。"""
    import urllib.parse as up

    parsed = up.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "" and not url.startswith(("sqlite", "mysql", "azure")):
        # 裸路径，视为 sqlite
        return {"scheme": "sqlite", "path": url}
    if scheme == "sqlite":
        path = parsed.path
        if path.lstrip("/") == ":memory:":
            return {"scheme": "sqlite", "path": ":memory:"}
        return {"scheme": "sqlite", "path": path}
    if scheme in ("mysql", "azure"):
        # password may be URL-encoded; unquote it
        password = up.unquote(parsed.password or "")
        return {
            "scheme": scheme,
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 3306,
            "user": parsed.username or "root",
            "password": password,
            "database": (parsed.path or "/").lstrip("/"),
        }
    return {"scheme": "unknown", "raw": url}


def check_db_connection(url: str) -> tuple[bool, str]:
    """测试 DB 连接 + 初始化 schema。返回 (是否成功, 消息)。"""
    try:
        from .storage import create_task_store

        store = create_task_store(url)
        # 触发 schema 初始化（_ensure_default_user 在 connect 时执行）
        # 用 list_tasks 之类的轻量调用验证连接
        store.list_tasks(None, user_id="default")
        return True, "连接成功，已初始化 schema"
    except Exception as exc:
        return False, f"连接失败：{exc}"


def test_ai_provider(provider: str, api_key: str, base_url: str, model: str) -> tuple[bool, str]:
    """测试 AI provider 调用。"""
    import asyncio

    try:
        from openai import AsyncOpenAI

        async def _ping():
            client = AsyncOpenAI(api_key=api_key or "dummy", base_url=base_url or None)
            try:
                await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": "ping"}],
                        max_tokens=5,
                    ),
                    timeout=10,
                )
            finally:
                await client.close()

        asyncio.run(_ping())
        return True, f"响应正常（{model}）"
    except Exception as exc:
        return False, f"调用失败：{exc}"


def test_ollama(base_url: str) -> tuple[bool, list[str]]:
    """探测 Ollama 服务并返回模型列表。"""
    import urllib.request

    try:
        # base_url 形如 http://localhost:11434/v1
        tags_url = base_url.rstrip("/").removesuffix("/v1") + "/api/tags"
        with urllib.request.urlopen(tags_url, timeout=5) as resp:
            import json

            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", [])]
        return True, models
    except Exception:
        return False, []


# ═══════════════════════════════════════════════════════════════════
# 向导步骤
# ═══════════════════════════════════════════════════════════════════


def _import_questionary():
    """惰性导入 questionary，未装时给出友好提示。"""
    try:
        import questionary

        return questionary
    except ImportError:
        print(
            "✗ 配置向导需要 questionary 库。请运行：\n"
            "  pip install -e '.[wizard]'"
        )
        raise


def _import_rich():
    try:
        from rich.console import Console
        from rich.table import Table

        return Console(), Table
    except ImportError:
        return None, None


def step_welcome(console) -> None:
    """欢迎页。"""
    console.print()
    console.print("[bold cyan]═══ Momentum 配置向导 ═══[/bold cyan]")
    console.print()
    console.print("这个向导会引导你完成 Momentum 的全量配置。")
    console.print("每一步都会显示当前值，[green]回车保留原值[/green]，输入新值则覆盖。")
    console.print("全部完成后会生成 .env 和 momentum.config.json。")
    console.print()
    console.print("[yellow]提示[/yellow]：想跳过某项直接回车即可；想中止按 Ctrl+C。")
    console.print()


def step_database(questionary, console, *, existing_url: str | None, skip_db_check: bool) -> tuple[str, bool, bool]:
    """DB 后端步骤。返回 (url, schema_initialized, needs_password_change)。"""
    console.print("\n[bold]━━ 1/11 数据库后端 ━━[/bold]")

    # 预填：解析现有 URL
    existing = parse_db_url(existing_url) if existing_url else {}
    existing_scheme = existing.get("scheme", "")

    choices = [
        questionary.Choice("SQLite（本地文件，零配置）", value="sqlite"),
        questionary.Choice("MySQL（远程/独立服务器）", value="mysql"),
        questionary.Choice("Azure MySQL（自动 SSL）", value="azure"),
    ]
    default_choice_idx = {"sqlite": 0, "mysql": 1, "azure": 2}.get(existing_scheme, 0)
    backend = questionary.select(
        "选择数据库后端：",
        choices=choices,
        default=choices[default_choice_idx] if existing_scheme else choices[0],
    ).ask()
    if backend is None:
        raise KeyboardInterrupt

    if backend == "sqlite":
        default_path = existing.get("path", ".momentum/tasks.db")
        path = questionary.text(
            "数据库文件路径：",
            default=default_path,
        ).ask()
        if path is None:
            raise KeyboardInterrupt
        # 规范化为 sqlite:///path
        if not path.startswith("sqlite://"):
            if path == ":memory:":
                url = "sqlite:///:memory:"
                console.print("[yellow]⚠ :memory: 仅用于测试，数据不持久[/yellow]")
            else:
                abs_path = str(Path(path).expanduser().resolve())
                url = f"sqlite:///{abs_path}"
        else:
            url = path
    else:
        scheme = backend
        default_host = existing.get("host", "localhost")
        default_port = str(existing.get("port", 3306))
        default_user = existing.get("user", "root")
        default_db = existing.get("database", "momentum")

        host = questionary.text("主机：", default=default_host).ask()
        port_str = questionary.text("端口：", default=default_port).ask()
        user = questionary.text("用户名：", default=default_user).ask()
        # 密码不回填
        password = questionary.password(
            "密码" + ("（已配置，回车保留；输入新值覆盖）" if existing.get("password") else "：")
        ).ask()
        if not password and existing.get("password"):
            password = existing["password"]
        database = questionary.text("数据库名：", default=default_db).ask()
        if any(x is None for x in [host, port_str, user, password, database]):
            raise KeyboardInterrupt

        # URL encode 密码
        pwd_enc = urlparse.quote(password, safe="")
        url = f"{scheme}://{user}:{pwd_enc}@{host}:{port_str}/{database}"

    # 测连接
    if skip_db_check:
        console.print("[yellow]⚠ 跳过连接测试[/yellow]")
        return url, False, False

    console.print("测试连接…")
    ok, msg = check_db_connection(url)
    if ok:
        console.print(f"[green]✓ {msg}[/green]")
        return url, True, True
    else:
        console.print(f"[red]✗ {msg}[/red]")
        retry = questionary.confirm("重试？", default=True).ask()
        if retry:
            return step_database(
                questionary, console,
                existing_url=url, skip_db_check=False,
            )
        console.print("[yellow]⚠ 跳过，此项保留为输入值但未验证[/yellow]")
        return url, False, False


def step_security(questionary, console, store, *, needs_change: bool) -> tuple[bool, str | None]:
    """安全步骤：强制改 default/momentum 弱口令。返回 (是否改了密码, 新管理员 user_id)。"""
    console.print("\n[bold]━━ 2/11 安全：默认账户 ━━[/bold]")

    new_admin: str | None = None

    if needs_change:
        # 检测弱口令
        try:
            token = store.login_user("default", "momentum")
            weak = token is not None
        except Exception:
            weak = False

        if weak:
            console.print("[red]⚠ 检测到默认账户 default 仍使用弱口令 'momentum'，必须修改[/red]")
            while True:
                pwd1 = questionary.password("新口令（≥8 位）:").ask()
                if pwd1 is None:
                    raise KeyboardInterrupt
                if len(pwd1) < 8:
                    console.print("[red]✗ 长度不足 8 位，重试[/red]")
                    continue
                pwd2 = questionary.password("再次输入:").ask()
                if pwd1 != pwd2:
                    console.print("[red]✗ 两次不一致，重试[/red]")
                    continue
                break

            ok = store.change_password("default", "momentum", pwd1)
            if ok:
                console.print("[green]✓ 已更新 default 账户口令[/green]")
            else:
                console.print("[red]✗ 更新失败（旧口令不匹配？）[/red]")
        else:
            console.print("[green]✓ default 账户已不是弱口令，跳过[/green]")

        # 可选注册新管理员
        create_admin = questionary.confirm(
            "是否注册一个新管理员账户？（推荐生产环境）", default=False
        ).ask()
        if create_admin:
            admin_id = questionary.text("管理员用户名：").ask()
            display_name = questionary.text("显示名：", default=admin_id or "").ask()
            while True:
                pwd1 = questionary.password("管理员口令（≥8 位）:").ask()
                if pwd1 is None or len(pwd1) < 8:
                    console.print("[red]✗ 长度不足 8 位或取消，重试[/red]")
                    continue
                pwd2 = questionary.password("再次输入:").ask()
                if pwd1 != pwd2:
                    console.print("[red]✗ 两次不一致，重试[/red]")
                    continue
                break
            from .auth import hash_password

            if admin_id:
                try:
                    store.register_user(admin_id, display_name or admin_id, hash_password(pwd1))
                    console.print(f"[green]✓ 已注册管理员 {admin_id}，请用新账户登录[/green]")
                    new_admin = admin_id
                except Exception as exc:
                    console.print(f"[red]✗ 注册失败：{exc}[/red]")
    else:
        console.print("[green]✓ schema 已存在或无弱口令风险，跳过[/green]")

    return needs_change and weak if 'weak' in dir() else False, new_admin


def step_ai_provider(questionary, console, store, user_id: str, existing_config: dict) -> dict[str, str]:
    """AI Provider 步骤。返回要写入 user_memory 的更新。"""
    console.print("\n[bold]━━ 3/11 AI 提供商 ━━[/bold]")

    existing_provider = existing_config.get("provider")
    choices = [
        questionary.Choice("OpenAI 兼容（DeepSeek / OpenAI / 第三方）", value="openai"),
        questionary.Choice("Ollama（本地模型）", value="ollama"),
        questionary.Choice("跳过（先用本地解析，稍后再配）", value="none"),
    ]
    default_idx = {"openai": 0, "ollama": 1, "none": 2}.get(existing_provider, 0)
    choice = questionary.select(
        "选择 AI 提供商：",
        choices=choices,
        default=choices[default_idx] if existing_provider else choices[0],
    ).ask()
    if choice is None:
        raise KeyboardInterrupt

    updates: dict[str, str] = {"provider": choice}

    if choice == "none":
        console.print("[yellow]AI 功能将降级到本地解析，稍后可在 Web 偏好设置里补全[/yellow]")
        return updates

    if choice == "ollama":
        default_base = existing_config.get("api_base") or "http://localhost:11434"
        base_url = questionary.text("Ollama 服务地址：", default=default_base).ask()
        if base_url is None:
            raise KeyboardInterrupt
        # 补全 /v1
        if not base_url.rstrip("/").endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"

        # 探测模型列表
        ok, models = test_ollama(base_url)
        if ok and models:
            console.print(f"[green]✓ 发现 {len(models)} 个模型[/green]")
            model = questionary.select(
                "选择模型：", choices=[questionary.Choice(m, value=m) for m in models],
            ).ask()
        else:
            console.print("[yellow]⚠ 探测失败，手动输入模型名[/yellow]")
            model = questionary.text("模型名：", default=existing_config.get("model", "llama3.2")).ask()
        if model is None:
            raise KeyboardInterrupt

        updates["api_base"] = base_url
        updates["api_key"] = "ollama"
        updates["model"] = model
        return updates

    # openai 分支
    # 快捷预设
    presets = {
        "DeepSeek": {"base": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
        "OpenAI 官方": {"base": "https://api.openai.com/v1", "model": "gpt-4.1-mini"},
        "自定义": None,
    }
    preset_choice = questionary.select(
        "选择预设：",
        choices=[questionary.Choice(k, value=k) for k in presets],
        default=questionary.Choice("DeepSeek", value="DeepSeek"),
    ).ask()

    if preset_choice == "自定义" or preset_choice is None:
        base_url = questionary.text(
            "API Base URL：",
            default=existing_config.get("api_base", ""),
        ).ask()
        model = questionary.text(
            "模型名：",
            default=existing_config.get("model", "gpt-4.1-mini"),
        ).ask()
    else:
        preset = presets[preset_choice]
        base_url = questionary.text("API Base URL：", default=preset["base"]).ask()
        model = questionary.text("模型名：", default=preset["model"]).ask()
    if base_url is None or model is None:
        raise KeyboardInterrupt

    # API Key 不回填
    has_existing_key = bool(existing_config.get("api_key"))
    api_key = questionary.password(
        "API Key" + ("（已配置，回车保留；输入新值覆盖）: " if has_existing_key else ": ")
    ).ask()
    if not api_key and has_existing_key:
        api_key = existing_config["api_key"]
    if api_key is None:
        raise KeyboardInterrupt

    # 测试调用
    test = questionary.confirm("现在测试一次 API 调用？", default=True).ask()
    if test:
        console.print("测试中…")
        ok, msg = test_ai_provider(choice, api_key, base_url, model)
        if ok:
            console.print(f"[green]✓ {msg}[/green]")
        else:
            console.print(f"[red]✗ {msg}[/red]")
            if not questionary.confirm("仍然保留这些配置？", default=True).ask():
                return step_ai_provider(questionary, console, store, user_id, existing_config)

    updates["api_key"] = api_key
    updates["api_base"] = base_url
    updates["model"] = model
    return updates


def step_preferences(questionary, console, existing: dict) -> dict[str, str]:
    """工作偏好步骤。"""
    console.print("\n[bold]━━ 4/11 工作偏好 ━━[/bold]")

    vision = questionary.confirm(
        "启用图片视觉识别？（上传图片建任务）",
        default=existing.get("vision_enabled") == "true",
    ).ask()

    cap = questionary.text(
        "每日可用时间（分钟）：",
        default=existing.get("daily_capacity_minutes", "240"),
    ).ask()
    # 校验数字
    while cap and not cap.isdigit():
        console.print("[red]✗ 必须是数字[/red]")
        cap = questionary.text("每日可用时间（分钟）：", default="240").ask()

    wh_start = questionary.text(
        "工作开始时间：", default=existing.get("working_hours_start", "09:00"),
    ).ask()
    wh_end = questionary.text(
        "工作结束时间：", default=existing.get("working_hours_end", "18:00"),
    ).ask()

    if any(x is None for x in [cap, wh_start, wh_end]):
        raise KeyboardInterrupt

    return {
        "vision_enabled": "true" if vision else "false",
        "daily_capacity_minutes": cap,
        "working_hours_start": wh_start,
        "working_hours_end": wh_end,
    }


def step_location(questionary, console, existing: dict) -> dict[str, str]:
    """位置步骤。"""
    console.print("\n[bold]━━ 5/11 默认位置 ━━[/bold]")
    city = questionary.text(
        "默认城市：", default=existing.get("user_location", "北京"),
    ).ask()
    if city is None:
        raise KeyboardInterrupt
    return {"user_location": city}


def step_heartbeat(questionary, console, store, user_id: str, existing: dict) -> dict[str, str]:
    """心跳步骤。"""
    console.print("\n[bold]━━ 6/11 心跳提醒 ━━[/bold]")

    # 解析现有 heartbeat_config（JSON）
    import json

    hb: dict = {}
    if existing.get("heartbeat_config"):
        try:
            hb = json.loads(existing["heartbeat_config"])
        except (json.JSONDecodeError, TypeError):
            hb = {}

    enabled = questionary.confirm(
        "启用主动提醒？", default=hb.get("enabled", False),
    ).ask()

    if not enabled:
        return {"heartbeat_config": json.dumps({
            "enabled": False,
            "start_hour": hb.get("start_hour", 9),
            "end_hour": hb.get("end_hour", 21),
            "interval_hours": hb.get("interval_hours", 4),
        })}

    start_hour = questionary.text(
        "提醒开始小时（0-23）：", default=str(hb.get("start_hour", 9)),
    ).ask()
    end_hour = questionary.text(
        "提醒结束小时（0-23）：", default=str(hb.get("end_hour", 21)),
    ).ask()
    interval = questionary.text(
        "两次提醒最小间隔（小时，1-24）：", default=str(hb.get("interval_hours", 4)),
    ).ask()

    if any(x is None for x in [start_hour, end_hour, interval]):
        raise KeyboardInterrupt

    # clamp
    try:
        sh = max(0, min(23, int(start_hour)))
        eh = max(0, min(23, int(end_hour)))
        iv = max(1, min(24, int(interval)))
    except ValueError:
        sh, eh, iv = 9, 21, 4

    config = {
        "enabled": True,
        "start_hour": sh,
        "end_hour": eh,
        "interval_hours": iv,
        "last_heartbeat_at": hb.get("last_heartbeat_at"),
    }
    return {"heartbeat_config": json.dumps(config)}


def step_web_server(questionary, console, existing_host: str, existing_port: int) -> tuple[str, int]:
    """Web 服务步骤。"""
    console.print("\n[bold]━━ 7/11 Web 服务 ━━[/bold]")

    host_choices = [
        questionary.Choice("127.0.0.1（仅本机）", value="127.0.0.1"),
        questionary.Choice("0.0.0.0（局域网可访问）", value="0.0.0.0"),
        questionary.Choice("自定义", value="custom"),
    ]
    # 选默认
    default_h = next((c for c in host_choices if c.value == existing_host), host_choices[0])
    host_choice = questionary.select("监听地址：", choices=host_choices, default=default_h).ask()
    if host_choice == "custom":
        host = questionary.text("自定义地址：", default=existing_host).ask()
    else:
        host = host_choice
    if host is None:
        raise KeyboardInterrupt

    port_str = questionary.text("监听端口：", default=str(existing_port)).ask()
    if port_str is None:
        raise KeyboardInterrupt
    while not port_str.isdigit():
        console.print("[red]✗ 端口必须是数字[/red]")
        port_str = questionary.text("监听端口：", default="8765").ask()
    port = int(port_str)

    # 端口占用检测
    if not is_port_available(host, port):
        console.print(f"[yellow]⚠ 端口 {port} 已被占用[/yellow]")
        if questionary.confirm("换一个端口？", default=True).ask():
            return step_web_server(questionary, console, host, port + 1)
        console.print("[yellow]⚠ 保留此端口，启动时可能冲突[/yellow]")

    return host, port


def step_mcp(questionary, console, existing_host: str, existing_port: int) -> tuple[bool, str, int, str | None]:
    """MCP 步骤。返回 (enabled, host, port, api_key)。"""
    console.print("\n[bold]━━ 8/11 MCP Server ━━[/bold]")

    enabled = questionary.confirm(
        "是否启用 MCP Server 的 SSE 传输？（stdio 默认可用，无需配置）",
        default=False,
    ).ask()

    if not enabled:
        return False, existing_host, existing_port, None

    host_choices = [
        questionary.Choice("127.0.0.1（仅本机）", value="127.0.0.1"),
        questionary.Choice("0.0.0.0（局域网可访问）", value="0.0.0.0"),
    ]
    host_choice = questionary.select("SSE 监听地址：", choices=host_choices, default=host_choices[0]).ask()
    if host_choice is None:
        raise KeyboardInterrupt

    port_str = questionary.text("SSE 监听端口：", default=str(existing_port)).ask()
    if port_str is None or not port_str.isdigit():
        port_str = "8766"
    port = int(port_str)

    if not is_port_available(host_choice, port):
        console.print(f"[yellow]⚠ 端口 {port} 已被占用[/yellow]")

    use_auth = questionary.confirm("启用 API Key 鉴权？", default=True).ask()
    api_key: str | None = None
    if use_auth:
        while True:
            k1 = questionary.password("API Key:").ask()
            if k1 is None:
                api_key = None
                break
            k2 = questionary.password("再次输入:").ask()
            if k1 == k2 and k1:
                api_key = k1
                break
            console.print("[red]✗ 两次不一致或为空，重试[/red]")

    return True, host_choice, port, api_key


def step_logging(questionary, console, existing: dict) -> dict[str, str]:
    """日志步骤。"""
    console.print("\n[bold]━━ 9/11 日志 ━━[/bold]")

    level_choices = ["DEBUG", "INFO", "WARNING", "ERROR"]
    current_level = existing.get("MOMENTUM_LOG_LEVEL", "INFO")
    level = questionary.select(
        "日志级别：",
        choices=[questionary.Choice(l, value=l) for l in level_choices],
        default=questionary.Choice(current_level if current_level in level_choices else "INFO",
                                    value=current_level if current_level in level_choices else "INFO"),
    ).ask()

    dir_choices = [
        questionary.Choice("logs（项目目录下）", value="logs"),
        questionary.Choice("~/.momentum/logs（用户主目录）", value="~/.momentum/logs"),
        questionary.Choice("自定义", value="custom"),
    ]
    default_dir = existing.get("MOMENTUM_LOG_DIR", "logs")
    default_dir_choice = next((c for c in dir_choices if c.value == default_dir), dir_choices[0])
    dir_choice = questionary.select("日志目录：", choices=dir_choices, default=default_dir_choice).ask()
    if dir_choice == "custom":
        log_dir = questionary.text("自定义目录：", default=default_dir).ask()
    else:
        log_dir = dir_choice
    if log_dir is None:
        raise KeyboardInterrupt

    max_bytes_str = questionary.text(
        "单个日志文件上限（MB）：", default=str(int(existing.get("MOMENTUM_LOG_MAX_BYTES", "10485760")) // 1024 // 1024),
    ).ask()
    backups_str = questionary.text(
        "保留备份数：", default=existing.get("MOMENTUM_LOG_BACKUPS", "5"),
    ).ask()
    if any(x is None for x in [max_bytes_str, backups_str]):
        raise KeyboardInterrupt

    try:
        max_bytes = int(max_bytes_str) * 1024 * 1024
    except ValueError:
        max_bytes = 10485760
    try:
        backups = int(backups_str)
    except ValueError:
        backups = 5

    return {
        "MOMENTUM_LOG_LEVEL": level,
        "MOMENTUM_LOG_DIR": log_dir,
        "MOMENTUM_LOG_MAX_BYTES": str(max_bytes),
        "MOMENTUM_LOG_BACKUPS": str(backups),
    }


def step_advanced(questionary, console, existing: dict) -> dict[str, str]:
    """进阶选项（可选，默认跳过）。"""
    console.print("\n[bold]━━ 10/11 进阶选项（可选） ━━[/bold]")

    enter = questionary.confirm("配置进阶 AI 选项？（大多数场景不需要）", default=False).ask()
    if not enter:
        return {}

    updates: dict[str, str] = {}
    disable_tracing = questionary.confirm(
        "关闭 agents SDK 链路追踪？",
        default=existing.get("MOMENTUM_DISABLE_TRACING", "").lower() in ("1", "true", "yes", "on"),
    ).ask()
    updates["MOMENTUM_DISABLE_TRACING"] = "true" if disable_tracing else "false"

    thinking = questionary.select(
        "思考模式：",
        choices=[questionary.Choice("默认（不设置）", value=""),
                 questionary.Choice("enabled", value="enabled"),
                 questionary.Choice("disabled", value="disabled")],
        default=questionary.Choice("默认（不设置）", value=""),
    ).ask()
    if thinking:
        updates["MOMENTUM_THINKING"] = thinking

    effort_choices = ["", "minimal", "low", "medium", "high"]
    effort = questionary.select(
        "推理强度：",
        choices=[questionary.Choice("默认（不设置）" if not v else v, value=v) for v in effort_choices],
        default=questionary.Choice("默认（不设置）", value=""),
    ).ask()
    if effort:
        updates["MOMENTUM_REASONING_EFFORT"] = effort

    return updates


def step_preview(console, result: WizardResult) -> bool:
    """配置预览。返回是否确认写入。"""
    console.print("\n[bold]━━ 11/11 配置预览 ━━[/bold]")

    _, Table = _import_rich()
    if Table is None:
        # rich 不可用，简单打印
        print("\n将要写入的配置：")
        if result.database_url:
            print(f"  数据库 URL: {result.database_url}")
        for k, v in result.env_updates.items():
            display = "****" if "KEY" in k.upper() or "PASSWORD" in k.upper() else v
            print(f"  {k}: {display}")
        for k, v in result.user_config_updates.items():
            display = "****" if k == "api_key" else v
            print(f"  user_memory.{k}: {display}")
        if result.web_host:
            print(f"  Web: {result.web_host}:{result.web_port}")
        if result.mcp_sse_enabled:
            print(f"  MCP SSE: {result.mcp_host}:{result.mcp_port}")
        confirm = input("\n确认写入？[Y/n] ").strip().lower()
        return confirm != "n"

    table = Table(title="配置预览", show_header=True)
    table.add_column("项", style="cyan")
    table.add_column("值", style="green")

    if result.database_url:
        table.add_row("数据库 URL", result.database_url)
    for k, v in result.env_updates.items():
        display = "****" if "KEY" in k.upper() or "PASSWORD" in k.upper() else v
        table.add_row(k, display)
    for k, v in result.user_config_updates.items():
        display = "****" if k == "api_key" else v
        table.add_row(f"user_memory.{k}", display)
    if result.web_host:
        table.add_row("Web 服务", f"{result.web_host}:{result.web_port}")
    if result.mcp_sse_enabled:
        table.add_row("MCP SSE", f"{result.mcp_host}:{result.mcp_port}")

    console.print(table)

    from questionary import confirm as q_confirm

    return q_confirm("确认写入这些配置？", default=True).ask()


# ═══════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════


def run_wizard(
    *,
    db_url: str | None = None,
    non_interactive: bool = False,
    skip_db_check: bool = False,
) -> WizardResult:
    """启动配置向导。

    Args:
        db_url: 已指定的 DB URL（跳过 DB 选择步）
        non_interactive: 非交互模式（用全部默认值+已有配置）
        skip_db_check: 跳过 DB 连接测试
    """
    if non_interactive:
        return _run_non_interactive(db_url, skip_db_check)

    questionary = _import_questionary()
    console, _ = _import_rich()
    if console is None:
        # 退化：用 print
        class _Dummy:
            def print(self, *a, **kw):
                # 去掉 rich 标记
                msg = str(a[0]) if a else ""
                msg = re.sub(r"\[/?(bold|cyan|green|yellow|red)[^\]]*\]", "", msg)
                msg = re.sub(r"\[bold[^\]]*\]", "", msg)
                print(msg)
        console = _Dummy()

    if non_interactive:
        return _run_non_interactive(db_url, skip_db_check)

    step_welcome(console)

    # 读取现有配置
    existing_env = read_env_file()
    existing_db_url = db_url or existing_env.get("MOMENTUM_DATABASE_URL", ".momentum/tasks.db")

    result = WizardResult()

    # 1. DB
    final_url, schema_ok, needs_pw_change = step_database(
        questionary, console,
        existing_url=existing_db_url,
        skip_db_check=skip_db_check,
    )
    result.database_url = final_url
    result.env_updates["MOMENTUM_DATABASE_URL"] = final_url

    # 连接 store
    store = None
    user_config: dict[str, str] = {}
    try:
        from .storage import create_task_store

        store = create_task_store(final_url)
        from .config import get_current_user

        user_id = get_current_user()
        user_config = store.get_all_memory(user_id) or {}
    except Exception as exc:
        console.print(f"[yellow]⚠ 无法连接 DB 读取用户配置：{exc}[/yellow]")
        console.print("[yellow]  跳过依赖 store 的步骤[/yellow]")

    # 2. 安全
    if store:
        pw_changed, new_admin = step_security(
            questionary, console, store, needs_change=needs_pw_change,
        )
        result.default_password_changed = pw_changed
        result.new_admin_user = new_admin

    # 3. AI Provider
    if store:
        ai_updates = step_ai_provider(questionary, console, store, get_current_user(), user_config)
        result.user_config_updates.update(ai_updates)

    # 4. 工作偏好
    if store:
        pref_updates = step_preferences(questionary, console, user_config)
        result.user_config_updates.update(pref_updates)

    # 5. 位置
    if store:
        loc_updates = step_location(questionary, console, user_config)
        result.user_config_updates.update(loc_updates)

    # 6. 心跳
    if store:
        hb_updates = step_heartbeat(questionary, console, store, get_current_user(), user_config)
        result.user_config_updates.update(hb_updates)

    # 7. Web
    from .wizard_config import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT

    web_host, web_port = step_web_server(
        questionary, console,
        existing_host=existing_env.get("MOMENTUM_WEB_HOST", DEFAULT_WEB_HOST),
        existing_port=int(existing_env.get("MOMENTUM_WEB_PORT", DEFAULT_WEB_PORT)),
    )
    result.web_host = web_host
    result.web_port = web_port

    # 8. MCP
    from .wizard_config import DEFAULT_MCP_HOST, DEFAULT_MCP_PORT

    mcp_enabled, mcp_host, mcp_port, mcp_key = step_mcp(
        questionary, console,
        existing_host=existing_env.get("MOMENTUM_MCP_HOST", DEFAULT_MCP_HOST),
        existing_port=int(existing_env.get("MOMENTUM_MCP_PORT", DEFAULT_MCP_PORT)),
    )
    result.mcp_sse_enabled = mcp_enabled
    result.mcp_host = mcp_host
    result.mcp_port = mcp_port
    if mcp_enabled and mcp_key:
        result.env_updates["MOMENTUM_MCP_API_KEY"] = mcp_key

    # 9. 日志
    log_updates = step_logging(questionary, console, existing_env)
    result.env_updates.update(log_updates)

    # 10. 进阶
    adv_updates = step_advanced(questionary, console, existing_env)
    result.env_updates.update(adv_updates)

    # 11. 预览 + 确认
    if not step_preview(console, result):
        console.print("[yellow]已取消，未写入任何配置[/yellow]")
        return result

    # 写盘
    _persist(result, store)

    # 末尾问是否启动
    start = questionary.confirm("\n现在启动 momentum-agent serve？", default=True).ask()
    if start:
        result.started_server = True
        _start_serve(final_url, web_host, web_port)

    return result


def _persist(result: WizardResult, store) -> None:
    """把结果写入文件 / DB / config.json。"""
    # .env
    write_env_file(result.env_updates)

    # user_memory
    if store and result.user_config_updates:
        from .config import get_current_user

        user_id = get_current_user()
        for k, v in result.user_config_updates.items():
            try:
                store.set_memory(k, v, user_id=user_id)
            except Exception as exc:
                log.warning("写入 user_memory %s 失败：%s", k, exc)

    # momentum.config.json
    from . import wizard_config

    if result.web_host and result.web_port:
        wizard_config.set_web_config(result.web_host, result.web_port)
    if result.mcp_sse_enabled and result.mcp_host and result.mcp_port:
        wizard_config.set_mcp_config(result.mcp_host, result.mcp_port)

    print("\n✓ 配置已写入：")
    print(f"  - .env（{len(result.env_updates)} 项）")
    if store and result.user_config_updates:
        print(f"  - user_memory（{len(result.user_config_updates)} 项）")
    print(f"  - momentum.config.json（web/mcp 监听）")


def _start_serve(db_url: str, host: str, port: int) -> None:
    """启动 serve。"""
    from .web import run_server

    print(f"\n→ 启动 serve：http://{host}:{port}")
    try:
        run_server(db_url, host=host, port=port)
    except KeyboardInterrupt:
        print("\n已停止")


def _run_non_interactive(db_url: str | None, skip_db_check: bool) -> WizardResult:
    """非交互模式：用全部默认值+已有配置，不提问。"""
    print("非交互模式：使用默认值和已有配置")

    existing_env = read_env_file()
    final_url = db_url or existing_env.get("MOMENTUM_DATABASE_URL", ".momentum/tasks.db")

    result = WizardResult()
    result.database_url = final_url
    result.env_updates["MOMENTUM_DATABASE_URL"] = final_url

    # 连接 store 写入默认用户配置
    try:
        from .storage import create_task_store
        from .config import get_current_user

        store = create_task_store(final_url)
        user_id = get_current_user()
        existing = store.get_all_memory(user_id) or {}

        defaults = {
            "provider": "none",
            "vision_enabled": "false",
            "daily_capacity_minutes": "240",
            "working_hours_start": "09:00",
            "working_hours_end": "18:00",
            "user_location": "北京",
        }
        for k, default_val in defaults.items():
            if k not in existing:
                result.user_config_updates[k] = default_val
                try:
                    store.set_memory(k, default_val, user_id=user_id)
                except Exception as exc:
                    log.warning("写入 %s 失败：%s", k, exc)

        # 安全检查：弱口令强制改
        try:
            token = store.login_user("default", "momentum")
            if token:
                import secrets

                new_pwd = secrets.token_urlsafe(12)
                store.change_password("default", "momentum", new_pwd)
                result.default_password_changed = True
                print(f"⚠ 检测到弱口令，已自动改为随机口令：{new_pwd}")
                print("请立即记录此口令！")
        except Exception:
            pass
    except Exception as exc:
        print(f"⚠ 无法连接 DB：{exc}")

    # 默认日志
    result.env_updates.setdefault("MOMENTUM_LOG_LEVEL", "INFO")
    result.env_updates.setdefault("MOMENTUM_LOG_DIR", "logs")

    # 默认 web/mcp
    from .wizard_config import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT

    result.web_host = existing_env.get("MOMENTUM_WEB_HOST", DEFAULT_WEB_HOST)
    result.web_port = int(existing_env.get("MOMENTUM_WEB_PORT", DEFAULT_WEB_PORT))

    _persist(result, None)
    return result


__all__ = [
    "WizardResult",
    "run_wizard",
    "write_env_file",
    "read_env_file",
    "is_port_available",
    "parse_db_url",
    "check_db_connection",
    "test_ai_provider",
    "test_ollama",
]
