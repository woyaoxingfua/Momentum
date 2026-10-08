#!/usr/bin/env python3
"""真实浏览器验收：流式生成中的「停止」按钮。

归档的 RunState/取消项在客户端侧能先交付的就是这一块：生成过程中可以中止，
而不是只能等它说完。这里用一个会慢慢吐字的假 provider 制造长回答，
点「停止」后断言：气泡标记为已停止、按钮隐藏、输入框恢复可用。
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
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from stub_provider import _StubHandler

ROOT = Path(__file__).resolve().parents[2]


def choose_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port != 8765:
            return port


def wait_for(url: str, timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("server did not become ready")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="momentum-chat-cancel-"))
    db_path = workdir / "isolated.sqlite3"
    app_port = choose_port()
    stub_port = choose_port()
    origin = f"http://127.0.0.1:{app_port}"

    # 假 provider：把一句话拆成很多片，并逐片之间停顿，制造「正在生成」的窗口。
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), _StubHandler)
    stub.requests = []
    long_text = "这是一段很长的回答" * 20
    stub.script = [{"text": long_text, "pieces": ["…" + long_text[i:i + 4] for i in range(0, len(long_text), 4)], "delay": 0.15}]
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("MOMENTUM_DATABASE_URL", None)
    env["MOMENTUM_API_KEY"] = "stub-key"
    env["MOMENTUM_BASE_URL"] = f"http://127.0.0.1:{stub_port}/v1"
    env["MOMENTUM_MODEL"] = "stub-model"
    env["MOMENTUM_PROVIDER"] = "openai"
    env["MOMENTUM_DISABLE_TRACING"] = "true"
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENAI_BASE_URL", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "momentum_agent", "--db", f"sqlite:///{db_path}",
         "serve", "--host", "127.0.0.1", "--port", str(app_port)],
        cwd=str(workdir), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))

    browser = None
    try:
        wait_for(f"{origin}/login.html")
        user_id = f"cancel-{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(16)
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

            page.goto(f"{origin}/login.html", wait_until="domcontentloaded")
            page.locator("#switchLink").click()
            page.locator("#userId").fill(user_id)
            page.locator("#displayName").fill("Cancel E2E")
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_function("() => document.querySelector('#error')?.textContent?.includes('注册成功')", timeout=15_000)
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_url("**/app", timeout=15_000)
            if page.locator("#onboardingOverlay").is_visible():
                page.locator("#onboardingSkip").click()

            check("默认看不到停止按钮", page.locator("#chatCancelButton").is_hidden())
            page.locator("#chatInput").fill("给我一段很长的回答")
            page.locator("#chatForm button[type=submit]").click()
            page.wait_for_selector("#chatCancelButton:not([hidden])", timeout=20_000)
            check("生成过程中出现停止按钮", True)
            page.wait_for_timeout(1200)
            partial = page.locator("#chatLog .message.agent").last.inner_text()
            check("停止前已经开始输出内容", len(partial) > 0, partial[:40])

            page.locator("#chatCancelButton").click()
            page.wait_for_function(
                "() => document.querySelector('#chatLog .message.agent:last-child')?.textContent?.includes('已停止生成')",
                timeout=15_000,
            )
            stopped = page.locator("#chatLog .message.agent").last.inner_text()
            check("气泡标记为已停止生成", "已停止生成" in stopped, stopped[-40:])
            check("停止后按钮重新隐藏", page.locator("#chatCancelButton").is_hidden())
            check("输入框恢复可用", page.locator("#chatInput").is_enabled())

            # 停止之后再发一条仍然可用（状态被正确清理）
            page.locator("#chatInput").fill("再问一句")
            page.locator("#chatForm button[type=submit]").click()
            page.wait_for_selector("#chatCancelButton:not([hidden])", timeout=20_000)
            check("停止后仍能发起新的生成", True)
            page.locator("#chatCancelButton").click()
            page.wait_for_timeout(500)
            context.close()
            browser.close()
            browser = None

        diagnostics = {"checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks]}
        print("DIAGNOSTICS " + json.dumps(diagnostics, ensure_ascii=False))
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write("### Chat cancel diagnostics" + chr(10) + chr(10))
                handle.write("~~~json" + chr(10))
                handle.write(json.dumps(diagnostics, ensure_ascii=False, indent=2))
                handle.write(chr(10) + "~~~" + chr(10))
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        stub.shutdown()
        stub.server_close()
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

