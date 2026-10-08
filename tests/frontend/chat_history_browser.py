#!/usr/bin/env python3
"""真实浏览器验收：刷新页面后对话历史仍然可见。

对话历史上一步已经落库（重启/多 worker 都不丢），但界面上看不到——
这条用例验证 app 启动时会从 /api/chat/history 回填，刷新后原来的问答还在。
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
    workdir = Path(tempfile.mkdtemp(prefix="momentum-chat-history-"))
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
        user_id = f"history-ui-{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(16)
        question = f"记录一下 明天下午三点交周报 {secrets.token_hex(2)}"
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
            page.locator("#displayName").fill("History E2E")
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_function("() => document.querySelector('#error')?.textContent?.includes('注册成功')", timeout=15_000)
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_url("**/app", timeout=15_000)
            if page.locator("#onboardingOverlay").is_visible():
                page.locator("#onboardingSkip").click()

            # 本地模式（未配置 provider）下对话会立即走本地解析，用于产生真实的一轮问答。
            page.locator("#chatInput").fill(question)
            page.locator("#chatForm button[type=submit], #chatForm button").first.click()
            page.wait_for_function(
                "() => document.querySelectorAll('#chatLog .message').length >= 2",
                timeout=20_000,
            )
            before = page.locator("#chatLog .message").count()
            check("对话产生了问答气泡", before >= 2, f"{before} 条")

            # 刷新：历史应从后端回填
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#taskInput", timeout=15_000)
            page.wait_for_function(
                "() => document.querySelectorAll('#chatLog .message').length >= 2",
                timeout=15_000,
            )
            after = page.locator("#chatLog .message").count()
            check("刷新后历史仍然可见", after >= 2, f"{after} 条")
            text = page.locator("#chatLog").inner_text()
            check("历史里包含原先的提问", "交周报" in text, text[:120])

            api = page.evaluate(
                """async () => {
                     const token = localStorage.getItem("momentum_token");
                     const response = await fetch("/api/chat/history", { headers: { "Authorization": `Bearer ${token}` } });
                     return { status: response.status, body: await response.json() };
                   }"""
            )
            turns = (api.get("body") or {}).get("turns") or []
            check("/api/chat/history 返回纯文本轮次", len(turns) >= 2, json.dumps(turns, ensure_ascii=False)[:120])
            check("轮次里没有工具结构", all(set(turn.keys()) == {"role", "content"} for turn in turns), str(turns[:1]))

            # 清空后界面不应再显示历史
            page.evaluate("""async () => {
                     const token = localStorage.getItem("momentum_token");
                     await fetch("/api/chat/clear", { method: "POST", headers: { "Authorization": `Bearer ${token}` } });
                   }""")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#taskInput", timeout=15_000)
            page.wait_for_timeout(1200)
            remaining = page.locator("#chatLog .message").count()
            check("清空后刷新不再回填历史", remaining == 0, f"{remaining} 条")
            context.close()
            browser.close()
            browser = None

        diagnostics = {"turns": turns, "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks]}
        print("DIAGNOSTICS " + json.dumps(diagnostics, ensure_ascii=False))
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write("### Chat history UI diagnostics" + chr(10) + chr(10))
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

