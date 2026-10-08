#!/usr/bin/env python3
"""真实浏览器验收：审批面板把被拦下的破坏性操作交还给用户决定。

流程：真实注册登录 → 造一条待确认操作（直接写隔离库的 pending_approvals，等价于 agent 被门禁拦下）
→ 刷新后断言面板出现、显示摘要 → 点「批准」→ 断言面板消失且任务真的变成 dropped。
再补一条：点「取消」时任务保持 todo。
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
from datetime import datetime, timezone
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


def seed_pending(db_path: Path, user_id: str, task_id: int, approval_id: str) -> None:
    payload = [{"id": approval_id, "tool": "drop_task", "arguments": {"task_id": task_id},
                "summary": f"放弃任务 #{task_id}「浏览器验收任务」",
                "created_at": datetime.now(timezone.utc).isoformat()}]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO user_memory (user_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, "pending_approvals", json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()


def task_status(db_path: Path, task_id: int) -> str:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row[0] if row else "missing"


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="momentum-approval-ui-"))
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
        user_id = f"approval-ui-{secrets.token_hex(4)}"
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
            page.locator("#displayName").fill("Approval UI")
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_function("() => document.querySelector('#error')?.textContent?.includes('注册成功')", timeout=15_000)
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_url("**/app", timeout=15_000)
            if page.locator("#onboardingOverlay").is_visible():
                page.locator("#onboardingSkip").click()

            # 用应用自己的 token 建任务，然后在隔离库里登记一条待确认操作
            created = page.evaluate(
                """async (title) => {
                     const token = localStorage.getItem("momentum_token");
                     const response = await fetch("/api/tasks", {
                       method: "POST",
                       headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
                       body: JSON.stringify({ text: title }),
                     });
                     return await response.json();
                   }""",
                f"浏览器验收任务-{secrets.token_hex(3)}",
            )
            task_id = created["tasks"][0]["id"]
            check("建出了待审批任务", isinstance(task_id, int), str(task_id))
            seed_pending(db_path, user_id, task_id, "ui0001")

            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#taskInput", timeout=15_000)
            page.wait_for_selector("#approvalsPanel:not([hidden])", timeout=15_000)
            check("待确认面板出现", True)
            text = page.locator("#approvalsPanel").inner_text()
            check("面板显示操作摘要", "放弃任务" in text, text[:120])
            check("批准前任务仍是待办", task_status(db_path, task_id) == "todo", task_status(db_path, task_id))

            page.locator(".approvals-approve").first.click()
            page.wait_for_function(
                "() => document.querySelector('#approvalsPanel')?.hasAttribute('hidden')",
                timeout=15_000,
            )
            check("批准后面板消失", True)
            check("批准后任务被放弃", task_status(db_path, task_id) == "dropped", task_status(db_path, task_id))

            # 再来一条，验证取消路径
            second = page.evaluate(
                """async (title) => {
                     const token = localStorage.getItem("momentum_token");
                     const response = await fetch("/api/tasks", {
                       method: "POST",
                       headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
                       body: JSON.stringify({ text: title }),
                     });
                     return await response.json();
                   }""",
                f"浏览器验收任务B-{secrets.token_hex(3)}",
            )
            second_task_id = second["tasks"][0]["id"]
            seed_pending(db_path, user_id, second_task_id, "ui0002")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#approvalsPanel:not([hidden])", timeout=15_000)
            page.locator(".approvals-reject").first.click()
            page.wait_for_function(
                "() => document.querySelector('#approvalsPanel')?.hasAttribute('hidden')",
                timeout=15_000,
            )
            check("取消后任务保持待办", task_status(db_path, second_task_id) == "todo", task_status(db_path, second_task_id))
            context.close()
            browser.close()
            browser = None

        diagnostics = {"checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks]}
        print("DIAGNOSTICS " + json.dumps(diagnostics, ensure_ascii=False))
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write("### Approval panel diagnostics" + chr(10) + chr(10))
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

