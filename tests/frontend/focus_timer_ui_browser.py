#!/usr/bin/env python3
"""真实浏览器验收：登录后的专注计时 UI 全流程（开始 / 暂停 / 继续 / 结束）。

归档里明确记录着这个缺口：先前的浏览器验收只检查了登录入口，
「按你的要求没有提交密码、接管登录或绕过应用认证」，因此开始/暂停/恢复/停止的
登录后 UI 流程尚未在真实浏览器确认；HTTP API 验收与可控时钟测试不等同于它。

这里在隔离实例上真实注册并登录，走完这条 UI 流程，并验证核心语义：
暂停期间的时间不计入实际专注秒数（暂停前后各跑一段，暂停段明显更长）。

约束：随机端口、隔离 SQLite、绝不触碰真实库；登录凭据只存在于局部变量。
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
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SQLITE_PATH: Path | None = None


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


def focus_sessions(db_path: Path, user_id: str) -> list[dict]:
    from momentum_agent.storage import SQLiteTaskStore

    return SQLiteTaskStore(db_path).get_focus_sessions(user_id=user_id)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="momentum-focus-ui-"))
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
        user_id = f"focus-ui-{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(16)
        task_title = f"专注UI验证-{secrets.token_hex(3)}"
        with sync_playwright() as playwright:
            launch_kwargs: dict = {"headless": True}
            executable = os.environ.get("MOMENTUM_E2E_CHROMIUM")
            if executable:
                launch_kwargs["executable_path"] = executable
            if os.name != "nt":
                launch_kwargs["args"] = ["--no-sandbox"]
            browser = playwright.chromium.launch(**launch_kwargs)
            context = browser.new_context(viewport={"width": 1365, "height": 1000}, service_workers="allow")
            page = context.new_page()

            # 真实表单注册 + 登录
            page.goto(f"{origin}/login.html", wait_until="domcontentloaded")
            page.locator("#switchLink").click()
            page.locator("#userId").fill(user_id)
            page.locator("#displayName").fill("Focus UI E2E")
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_function("() => document.querySelector('#error')?.textContent?.includes('注册成功')", timeout=15_000)
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_url("**/app", timeout=15_000)
            page.wait_for_selector("#taskInput", timeout=15_000)
            if page.locator("#onboardingOverlay").is_visible():
                page.locator("#onboardingSkip").click()

            # 通过真实输入框建任务，再在专注面板里选中它
            page.locator("#taskInput").fill(task_title)
            page.locator("#addTaskButton").click()
            page.get_by_text(task_title, exact=True).first.wait_for(timeout=15_000)
            page.reload(wait_until="domcontentloaded")
            option_value = page.wait_for_function(
                """title => {
                     const select = document.querySelector('#focusTaskSelect');
                     const option = Array.from(select?.options || [])
                       .find((item) => item.textContent.startsWith(title));
                     return option ? option.value : null;
                   }""",
                arg=task_title,
                timeout=15_000,
            ).json_value()
            page.locator("#focusTaskSelect").select_option(value=option_value)

            # 开始
            page.locator("#focusStartBtn").click()
            page.wait_for_selector("#focusRunning:not(.hidden)", timeout=15_000)
            check("点击专注后进入计时界面", True)
            countdown = lambda: page.locator("#focusCountdown").inner_text().strip()
            first = countdown()
            page.wait_for_timeout(2400)
            second = countdown()
            check("计时界面在走动", second != first, f"{first} -> {second}")

            # 暂停：读数必须冻住，暂停段刻意比运行段长
            page.locator("#focusPauseBtn").click()
            pause_label = page.locator("#focusPauseBtn").inner_text().strip()
            check("暂停后按钮变为继续", pause_label == "继续", pause_label)
            paused_first = countdown()
            pause_started = time.monotonic()
            page.wait_for_timeout(3000)
            paused_second = countdown()
            pause_seconds = time.monotonic() - pause_started
            check("暂停期间计时冻结", paused_first == paused_second, f"{paused_first} == {paused_second}")

            # 继续
            page.locator("#focusPauseBtn").click()
            resume_label = page.locator("#focusPauseBtn").inner_text().strip()
            check("继续后按钮变回暂停", resume_label == "暂停", resume_label)
            run_started = time.monotonic()
            page.wait_for_timeout(2400)
            third = countdown()
            check("继续后重新走动", third != paused_second, f"{paused_second} -> {third}")
            running_seconds = (time.monotonic() - run_started) + 2.4

            # 结束
            page.locator("#focusStopBtn").click()
            page.wait_for_function(
                "() => document.querySelector('#focusRunning')?.classList.contains('hidden')",
                timeout=20_000,
            )
            check("结束后退出计时界面", True)
            if page.locator("#focusDoneBtn").is_visible():
                page.locator("#focusDoneBtn").click()
                page.wait_for_timeout(300)

            context.close()
            browser.close()
            browser = None

        sessions = [s for s in focus_sessions(db_path, user_id) if s.get("task_id") == int(option_value)]
        check("只产生一条专注记录", len(sessions) == 1, f"{len(sessions)} 条")
        actual = float(sessions[0]["actual_seconds"]) if sessions else -1
        outcome = sessions[0]["outcome"] if sessions else None
        # 运行总时长约 5 秒、暂停约 3 秒；若把暂停算进去总数会接近 8 秒
        check("实际秒数接近真实运行时长", running_seconds - 3 <= actual <= running_seconds + 3, f"actual={actual} running~{running_seconds:.1f}")
        check("暂停时间未被计入", actual < running_seconds + pause_seconds - 1, f"actual={actual} running+pause~{running_seconds + pause_seconds:.1f}")
        check("提前结束记为 stopped", outcome == "stopped", str(outcome))

        diagnostics = {
            "countdownTrace": [first, second, paused_first, paused_second, third],
            "pauseSeconds": round(pause_seconds, 1),
            "runningSeconds": round(running_seconds, 1),
            "actualSeconds": actual,
            "outcome": outcome,
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        }
        print("DIAGNOSTICS " + json.dumps(diagnostics, ensure_ascii=False))
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write("### Focus timer UI diagnostics" + chr(10) + chr(10))
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

