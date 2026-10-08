#!/usr/bin/env python3
"""真实浏览器验收：一次顺延 UI 操作只产生一次请求、只写一条记录。

归档里留着一个未解释的观察：此前某次模拟中「一次 UI 操作后 7 微秒出现第二个相同 POST，
导致截止日 +2 天并写两条 updated」，触发来源未知；同 key 重放不重复写只能证明幂等键有效，
不能证明这个症状已经消失。

当前实现有两道防线（postpone-action.mjs）：页面内 in-flight 锁 + localStorage 里持久化的
顺延意图（结果不确定时只允许显式复用原 key 重试）。这条用例在真实浏览器里数请求：
一次点击必须恰好一个 POST，截止日恰好 +1 天，恰好一条 updated 事件、一条幂等记录。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
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


def scalar(db_path: Path, sql: str, params: tuple = ()):
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(sql, params).fetchone()
    return row[0] if row else None


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="momentum-postpone-once-"))
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
    requests_seen: list[dict] = []
    try:
        wait_for_server(f"{origin}/login.html", process)
        user_id = f"postpone-once-{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(16)
        task_title = f"顺延一次验证{secrets.token_hex(3)}"
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

            def record_request(request):
                if request.method == "POST" and "/postpone" in request.url:
                    requests_seen.append({
                        "url": request.url,
                        "body": request.post_data,
                        "idempotency_key": request.headers.get("idempotency-key"),
                        "at": time.monotonic(),
                    })

            page.on("request", record_request)

            page.goto(f"{origin}/login.html", wait_until="domcontentloaded")
            page.locator("#switchLink").click()
            page.locator("#userId").fill(user_id)
            page.locator("#displayName").fill("Postpone Once")
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_function("() => document.querySelector('#error')?.textContent?.includes('注册成功')", timeout=15_000)
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_url("**/app", timeout=15_000)
            page.wait_for_selector("#taskInput", timeout=15_000)
            if page.locator("#onboardingOverlay").is_visible():
                page.locator("#onboardingSkip").click()

            # 用真实输入框建一个带截止日的任务（自然语言里的「明天」会被解析成截止日）
            page.locator("#taskInput").fill(f"{task_title} 明天下午三点")
            page.locator("#addTaskButton").click()
            page.wait_for_function(
                "() => document.querySelectorAll('[data-postpone]').length > 0",
                timeout=15_000,
            )
            task_id = page.evaluate("() => document.querySelector('[data-postpone]').getAttribute('data-postpone')")
            before_due = scalar(db_path, "SELECT due_at FROM tasks WHERE id = ?", (int(task_id),))
            check("任务带上了截止日", bool(before_due), str(before_due))

            # 打开顺延对话框并提交一次
            page.locator(f'[data-postpone="{task_id}"]').first.click()
            page.wait_for_selector("#postponeDialog[open]", timeout=10_000)
            page.locator("#postponeSubmitButton").click()
            page.wait_for_function(
                "() => { const dialog = document.querySelector('#postponeDialog'); return !dialog.open || dialog.querySelector('#postponeFeedback')?.textContent?.includes('顺延已确认'); }",
                timeout=15_000,
            )
            page.wait_for_timeout(800)  # 给任何潜在的第二个请求留出时间

            context.close()
            browser.close()
            browser = None

        post_count = len(requests_seen)
        check("一次点击只发出一个顺延请求", post_count == 1, f"{post_count} 个")
        if requests_seen:
            first = requests_seen[0]
            check("请求体是 {\"days\":1}", (first["body"] or "").replace(" ", "") == '{"days":1}', str(first["body"]))
            check("带上了 Idempotency-Key", bool(first["idempotency_key"]), str(first["idempotency_key"]))
            if post_count > 1:
                gap = requests_seen[1]["at"] - requests_seen[0]["at"]
                check("不存在同 tick 的第二个请求", False, f"间隔 {gap * 1_000_000:.1f} 微秒")

        after_due = scalar(db_path, "SELECT due_at FROM tasks WHERE id = ?", (int(task_id),))
        delta_days = (parse_dt(after_due) - parse_dt(before_due)).total_seconds() / 86400 if after_due and before_due else -1
        check("截止日恰好 +1 天", abs(delta_days - 1) < 1e-6, f"Δ={delta_days:.6f} 天")
        updated_events = scalar(db_path, "SELECT COUNT(*) FROM task_events WHERE event_type = 'updated'")
        check("只写一条 updated 事件", updated_events == 1, f"{updated_events} 条")
        ledger = scalar(db_path, "SELECT COUNT(*) FROM task_postpone_idempotency")
        check("只有一条顺延幂等记录", ledger == 1, f"{ledger} 条")

        diagnostics = {
            "postCount": post_count,
            "bodies": [r["body"] for r in requests_seen],
            "deltaDays": delta_days,
            "updatedEvents": updated_events,
            "ledgerRows": ledger,
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
        }
        print("DIAGNOSTICS " + json.dumps(diagnostics, ensure_ascii=False))
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write("### Postpone single-POST diagnostics" + chr(10) + chr(10))
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

