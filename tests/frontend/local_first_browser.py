#!/usr/bin/env python3
"""真实 Chromium 验收：本地任务附件（IndexedDB）与历史常完成任务。

运行方式（仓库根目录）：
    .venv/Scripts/python tests/frontend/local_first_browser.py

约束（与 focus_recovery_browser.py 一致）：
  - 只用临时目录里的隔离 SQLite；随机端口且绝不用 8765；绝不触碰历史数据库。
  - 不打印任何 token 或密码；注册用随机账号。
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import uuid
import zlib
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_ROOT = Path(os.environ.get("MOMENTUM_E2E_ROOT", str(Path(os.environ.get("TEMP", "/tmp")) / "momentum-local-first-e2e")))


def choose_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port != 8765:
            return port


def make_png(width: int = 2, height: int = 2) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def api(origin: str, method: str, path: str, payload: dict | None = None, token: str | None = None, headers: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {"Content-Type": "application/json"}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    if headers:
        request_headers.update(headers)
    request = Request(origin + path, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8") or "{}")


def wait_for_server(url: str, process: subprocess.Popen, timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"isolated server exited with {process.returncode}")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last = exc
        time.sleep(0.1)
    raise RuntimeError(f"isolated server not ready: {type(last).__name__}")


def login(page, origin: str, user_id: str, password: str) -> None:
    page.goto(f"{origin}/login.html", wait_until="domcontentloaded")
    page.locator("#userId").fill(user_id)
    page.locator("#password").fill(password)
    page.locator("#submitBtn").click()
    page.wait_for_url(lambda url: url.rstrip("/").endswith("/app"), timeout=20_000)
    page.wait_for_selector("#tasks", timeout=20_000)
    dismiss_onboarding(page)


def dismiss_onboarding(page) -> None:
    """首次登录会显示引导浮层，它会拦截点击；按真实用户路径点「跳过」。"""
    overlay = page.locator("#onboardingOverlay")
    try:
        if overlay.is_visible(timeout=3_000):
            page.locator("#onboardingSkip").click()
            page.wait_for_function(
                "() => { const el = document.getElementById('onboardingOverlay');"
                " return !el || el.style.display === 'none'; }",
                timeout=5_000,
            )
    except Exception:
        pass


def create_task(origin: str, token: str, title: str) -> int:
    """POST /api/tasks 返回的是整个待办列表，必须取同名任务里 id 最大的那个新任务。"""
    status, payload = api(origin, "POST", "/api/tasks", {"text": title}, token)
    assert status == 200, (status, payload)
    tasks = payload.get("tasks") or []
    match = [int(task["id"]) for task in tasks if task.get("title") == title]
    assert match, payload
    return max(match)


def complete_task(origin: str, token: str, task_id: int) -> None:
    status, payload = api(origin, "POST", f"/api/tasks/{task_id}/done", {}, token,
                          headers={"Idempotency-Key": str(uuid.uuid4())})
    assert status == 200, (status, payload)


def main() -> int:
    run_dir = ARTIFACTS_ROOT / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    db_path = run_dir / "isolated.sqlite3"
    port = choose_port()
    origin = f"http://127.0.0.1:{port}"
    database_url = f"sqlite:///{db_path}"
    command = [sys.executable, "-m", "momentum_agent", "--db", database_url,
               "--log-dir", str(run_dir / "logs"), "serve", "--host", "127.0.0.1", "--port", str(port)]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("MOMENTUM_DATABASE_URL", None)
    # 隔离验收必须离线：项目根的 .env 里有真实 key，这里显式清空，
    # 让服务端走本地 regex 回退，避免任何外部模型调用与不确定性。
    env["MOMENTUM_API_KEY"] = ""
    env["OPENAI_API_KEY"] = ""
    env["MOMENTUM_DISABLE_TRACING"] = "true"
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    log = (run_dir / "server.log").open("wb")
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))

    browser = None
    try:
        wait_for_server(f"{origin}/login.html", process)
        user_a = f"e2e-a-{secrets.token_hex(6)}"
        user_b = f"e2e-b-{secrets.token_hex(6)}"
        password = secrets.token_urlsafe(18)
        for user in (user_a, user_b):
            status, payload = api(origin, "POST", "/api/register",
                                  {"user_id": user, "display_name": user, "password": password})
            assert status == 200, (status, payload)
        status, payload = api(origin, "POST", "/api/login", {"user_id": user_a, "password": password})
        token_a = payload["token"]
        status, payload = api(origin, "POST", "/api/login", {"user_id": user_b, "password": password})
        token_b = payload["token"]

        shared_title = f"常完成任务-{secrets.token_hex(3)}"
        first_id = create_task(origin, token_a, shared_title)
        second_id = create_task(origin, token_a, shared_title)
        complete_task(origin, token_a, first_id)
        complete_task(origin, token_a, second_id)
        attach_id = create_task(origin, token_a, f"附件任务-{secrets.token_hex(3)}")
        b_task_id = create_task(origin, token_b, f"B的附件任务-{secrets.token_hex(3)}")

        with sync_playwright() as playwright:
            launch_kwargs = {"headless": True}
            if os.name != "nt":
                # CI 容器里通常需要关闭沙箱；Windows 本地跑不需要。
                launch_kwargs["args"] = ["--no-sandbox"]
            if os.environ.get("MOMENTUM_E2E_CHROMIUM"):
                launch_kwargs["executable_path"] = os.environ["MOMENTUM_E2E_CHROMIUM"]
            browser = playwright.chromium.launch(**launch_kwargs)
            context = browser.new_context(viewport={"width": 1365, "height": 1000}, service_workers="allow")
            page = context.new_page()
            login(page, origin, user_a, password)

            print("\n[A] 本地任务附件（IndexedDB）")
            dismiss_onboarding(page)
            page.wait_for_selector(f'[data-attachments="{attach_id}"]', timeout=15_000)
            page.locator(f'[data-attachments="{attach_id}"]').click()
            page.wait_for_selector("#attachmentDialog[open]", timeout=10_000)
            notice = page.locator("#attachmentNotice").inner_text()
            check("对话框展示本地-only 说明", "当前浏览器" in notice and "未加密" in notice, notice[:40])

            network: list[str] = []
            page.on("request", lambda request: network.append(request.method + " " + request.url))
            png = make_png(4, 4)
            page.set_input_files("#attachmentFile", files=[{"name": "shot.png", "mimeType": "image/png", "buffer": png}])
            page.wait_for_selector(".attachment-row", timeout=10_000)
            name = page.locator(".attachment-name").first.inner_text()
            check("附件行显示文件名（文本）", name == "shot.png", name)
            check("附件不触发任何 API 上传", not [u for u in network if "/api/" in u], str(network[:3]))

            page.locator("#attachmentCloseButton").click()
            page.reload(wait_until="domcontentloaded")
            dismiss_onboarding(page)
            page.wait_for_selector(f'[data-attachments="{attach_id}"]', timeout=15_000)
            page.locator(f'[data-attachments="{attach_id}"]').click()
            page.wait_for_selector(".attachment-row", timeout=10_000)
            check("刷新后附件仍在（IndexedDB 持久化）", page.locator(".attachment-row").count() == 1)

            oversized = b"x" * (9 * 1024 * 1024)
            page.set_input_files("#attachmentFile", files=[{"name": "big.png", "mimeType": "image/png", "buffer": oversized}])
            page.wait_for_function("() => document.querySelector('#attachmentStatus')?.textContent?.includes('8 MiB')", timeout=10_000)
            check("超过 8 MiB 被拒绝且有提示", page.locator(".attachment-row").count() == 1, page.locator("#attachmentStatus").inner_text()[:36])

            page.set_input_files("#attachmentFile", files=[{"name": "keep.png", "mimeType": "image/png", "buffer": make_png(3, 3)}])
            page.wait_for_function("() => document.querySelectorAll('.attachment-row').length === 2", timeout=10_000)
            page.locator(".attachment-actions button.danger").first.click()
            page.wait_for_function("() => document.querySelectorAll('.attachment-row').length === 1", timeout=10_000)
            remaining = page.locator(".attachment-name").first.inner_text()
            check("删除只删掉目标附件，另一个仍在", remaining == "keep.png", remaining)

            print("\n[B] 历史常完成任务（再来一个）")
            page.reload(wait_until="domcontentloaded")
            dismiss_onboarding(page)
            page.wait_for_selector("[data-attachments]", timeout=15_000)
            page.locator(".frequent-completed-entry button").first.click()
            page.wait_for_selector(".frequent-completed-item", timeout=10_000)
            summary = page.locator(".frequent-completed-meta").first.inner_text()
            check("展示完成次数与最近时间", "完成 2 次" in summary, summary)
            page.locator(".frequent-completed-item button").first.click()
            page.wait_for_function("() => document.querySelector('#tasks')?.textContent?.includes('已创建任务') || document.querySelector('.frequent-completed-status')?.textContent?.includes('已创建任务')", timeout=15_000)
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT status, due_at, recurrence, notes, parent_task_id, priority, estimated_minutes, tags "
                    "FROM tasks WHERE title = ? AND status = 'todo'", (shared_title,)).fetchall()
            check("再来一个新建了 todo", len(row) == 1, str(row))
            if row:
                status_value, due_at, recurrence, notes, parent, priority, minutes, tags = row[0]
                check("只复制标题/优先级/估时/标签",
                      due_at is None and recurrence is None and notes is None and parent is None,
                      f"due={due_at} rec={recurrence} notes={notes} parent={parent}")
                check("复制了来源的优先级与估时", priority in ("low", "medium", "high") and minutes is None,
                      f"priority={priority} minutes={minutes} tags={tags}")

            print("\n[C] 账号隔离")
            if page.locator("#attachmentDialog[open]").count():
                page.locator("#attachmentCloseButton").click()
            context.close()
            context_b = browser.new_context(viewport={"width": 1365, "height": 1000})
            page_b = context_b.new_page()
            login(page_b, origin, user_b, password)
            dismiss_onboarding(page_b)
            page_b.wait_for_selector(f'[data-attachments="{b_task_id}"]', timeout=15_000)
            page_b.locator(f'[data-attachments="{b_task_id}"]').click()
            page_b.wait_for_selector("#attachmentDialog[open]", timeout=10_000)
            page_b.wait_for_selector(".attachment-empty", timeout=10_000)
            check("另一账号看不到附件", page_b.locator(".attachment-row").count() == 0)
            page_b.locator("#attachmentCloseButton").click()
            page_b.wait_for_selector("#attachmentDialog[open]", state="detached", timeout=5_000)
            page_b.locator(".frequent-completed-entry button").first.click()
            page_b.wait_for_timeout(600)
            check("另一账号看不到常完成任务", page_b.locator(".frequent-completed-item").count() == 0)
            context_b.close()
            browser.close()
            browser = None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()
        log.close()

    failed = [name for name, ok, _ in results if not ok]
    print("\n================ 结果 ================")
    print(f"通过 {len(results) - len(failed)}/{len(results)}")
    for name in failed:
        print("  FAILED:", name)
    print("artifacts:", run_dir)
    print("isolated db:", db_path)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
