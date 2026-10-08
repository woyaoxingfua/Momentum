#!/usr/bin/env python3
"""真实浏览器验收：通知权限与「即将到期」提醒的实际投递。

归档里记着：通知权限与休眠/离线时的投递并未纳入浏览器 E2E。休眠唤醒需要在真机上等，
这里先把能在 CI 里确定复现的部分做掉：授予权限后，页面在不可见状态下轮询
/api/notifications/upcoming，并对 60 分钟内到期的任务真正构造 Notification。

做法：初始化脚本里包装 window.Notification 记录每次构造，并把 visibilityState 固定为 hidden
（notifications.js 在页面可见时刻意不弹通知），因此这条路径是确定性的而不是碰运气。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]

INIT_SCRIPT = """
(() => {
  const records = [];
  window.__notifications = records;
  const Original = window.Notification;
  function Wrapped(title, options) {
    records.push({ title: String(title), body: (options && options.body) || "", tag: (options && options.tag) || "" });
    try { return new Original(title, options); } catch (error) { return {}; }
  }
  Wrapped.permission = "granted";
  Wrapped.requestPermission = () => Promise.resolve("granted");
  try { window.Notification = Wrapped; } catch (error) {}
  try {
    Object.defineProperty(document, "visibilityState", { get: () => "hidden", configurable: true });
  } catch (error) {}
})();
"""


def choose_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port != 8765:
            return port


def wait_for_server(url: str, process: subprocess.Popen, timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"isolated server exited with {process.returncode}")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("isolated server did not become ready")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="momentum-notify-"))
    db_path = workdir / "isolated.sqlite3"
    port = choose_port()
    origin = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("MOMENTUM_DATABASE_URL", None)
    env["MOMENTUM_API_KEY"] = ""
    env["OPENAI_API_KEY"] = ""
    process = subprocess.Popen(
        [sys.executable, "-m", "momentum_agent", "--db", f"sqlite:///{db_path}",
         "serve", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(workdir), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))

    browser = None
    try:
        wait_for_server(f"{origin}/login.html", process)
        user_id = f"notify-{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(16)
        task_title = f"通知验证任务{secrets.token_hex(3)}"
        due_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0)
        with sync_playwright() as playwright:
            launch_kwargs: dict = {"headless": True}
            executable = os.environ.get("MOMENTUM_E2E_CHROMIUM")
            if executable:
                launch_kwargs["executable_path"] = executable
            if os.name != "nt":
                launch_kwargs["args"] = ["--no-sandbox"]
            browser = playwright.chromium.launch(**launch_kwargs)
            context = browser.new_context(viewport={"width": 1365, "height": 1000}, service_workers="allow")
            try:
                context.grant_permissions(["notifications"], origin=origin)
            except Exception:
                pass
            context.add_init_script(INIT_SCRIPT)
            page = context.new_page()

            # 注册 + 登录
            page.goto(f"{origin}/login.html", wait_until="domcontentloaded")
            page.locator("#switchLink").click()
            page.locator("#userId").fill(user_id)
            page.locator("#displayName").fill("Notify E2E")
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_function("() => document.querySelector('#error')?.textContent?.includes('注册成功')", timeout=15_000)
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_url("**/app", timeout=15_000)
            if page.locator("#onboardingOverlay").is_visible():
                page.locator("#onboardingSkip").click()

            # 用应用自己的 token 建一个 30 分钟后到期的任务
            created = page.evaluate(
                """async (title) => {
                     const token = localStorage.getItem("momentum_token");
                     const response = await fetch("/api/tasks", {
                       method: "POST",
                       headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
                       body: JSON.stringify({ text: title }),
                     });
                     return { status: response.status, body: await response.text() };
                   }""",
                task_title,
            )
            check("能建出通知验证任务", created["status"] == 200, str(created)[:120])
            # 该接口走自然语言解析、不接受显式 due_at；截止时间直接在隔离库里设定，
            # 保证落在「未来 60 分钟」这个窗口内，用例才是确定性的。
            import sqlite3

            with sqlite3.connect(db_path) as connection:
                connection.execute("UPDATE tasks SET due_at = ? WHERE title = ?", (due_at.isoformat(), task_title))
                connection.commit()


            # upcoming 接口本身（通知模块依赖它的契约）
            upcoming = page.evaluate(
                """async () => {
                     const token = localStorage.getItem("momentum_token");
                     const response = await fetch("/api/notifications/upcoming", {
                       headers: { "Authorization": `Bearer ${token}` },
                     });
                     return { status: response.status, body: await response.json() };
                   }"""
            )
            items = (upcoming.get("body") or {}).get("notifications") or []
            match = [item for item in items if item.get("title") == task_title]
            check("/api/notifications/upcoming 返回该任务", bool(match), json.dumps(items, ensure_ascii=False)[:140])
            if match:
                check("剩余分钟数在 60 分钟窗口内", 0 < int(match[0]["minutes_left"]) <= 60, str(match[0]["minutes_left"]))

            # 重新进入应用：此时页面被初始化为不可见，通知模块会立刻检查一次
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("() => (window.__notifications || []).length > 0", timeout=20_000)
            notifications = page.evaluate("() => window.__notifications")
            check("构造了原生通知", bool(notifications), json.dumps(notifications, ensure_ascii=False)[:160])
            if notifications:
                first = notifications[0]
                check("通知标题是「任务即将到期」", first["title"] == "任务即将到期", first["title"])
                check("通知正文包含任务标题", task_title in (first.get("body") or ""), first.get("body"))
            check("权限已授予（页面看到的 permission）", page.evaluate("() => window.Notification.permission") == "granted")
            context.close()
            browser.close()
            browser = None

        diagnostics = {
            "upcomingStatus": upcoming.get("status"),
            "upcomingCount": len(items),
            "notifications": notifications,
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        }
        print("DIAGNOSTICS " + json.dumps(diagnostics, ensure_ascii=False))
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write("### Notification delivery diagnostics" + chr(10) + chr(10))
                handle.write("~~~json" + chr(10))
                handle.write(json.dumps(diagnostics, ensure_ascii=False, indent=2))
                handle.write(chr(10) + "~~~" + chr(10))
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for _n, ok, _d in checks if ok)
    print("")
    print("================ 结果 ================")
    print(f"通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

