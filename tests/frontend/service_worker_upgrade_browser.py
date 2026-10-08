#!/usr/bin/env python3
"""真实浏览器验收：已装过旧版 Service Worker 的「老上下文」升级路径。

review 里没解释清楚的 Focus 快照 #1 发生在「旧上下文」里；那类问题的载体是
Service Worker / Cache Storage 的升级路径。这里在隔离副本上真实验证：
  1. 先用当前代码装好 SW 与缓存；
  2. 在副本里改一个被预缓存的静态资源并升 CACHE_NAME；
  3. 只做普通刷新（模拟老用户），断言最终拿到新缓存、新资源，且旧缓存被清理。

运行（仓库根目录）：.venv/Scripts/python tests/frontend/service_worker_upgrade_browser.py
约束：随机端口、隔离 SQLite、离线（清空 API Key），绝不触碰真实库与 8765。
"""
from __future__ import annotations

import json
import os
import re
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
MARKER = "/* sw-upgrade-probe-marker */"


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
    workdir = Path(tempfile.mkdtemp(prefix="momentum-sw-upgrade-"))
    src_copy = workdir / "src"
    shutil.copytree(ROOT / "src" / "momentum_agent", src_copy / "momentum_agent")
    static = src_copy / "momentum_agent" / "static"
    db = workdir / "isolated.sqlite3"
    port = choose_port()
    origin = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_copy) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("MOMENTUM_DATABASE_URL", None)
    env["MOMENTUM_API_KEY"] = ""
    env["OPENAI_API_KEY"] = ""
    process = subprocess.Popen(
        [sys.executable, "-m", "momentum_agent", "--db", f"sqlite:///{db}",
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
        with sync_playwright() as playwright:
            launch_kwargs: dict = {"headless": True}
            if os.environ.get("MOMENTUM_E2E_CHROMIUM"):
                launch_kwargs["executable_path"] = os.environ["MOMENTUM_E2E_CHROMIUM"]
            if os.name != "nt":
                launch_kwargs["args"] = ["--no-sandbox"]
            browser = playwright.chromium.launch(**launch_kwargs)
            context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="allow")
            page = context.new_page()
            user = f"sw-{secrets.token_hex(4)}"
            password = secrets.token_urlsafe(16)
            page.goto(f"{origin}/login.html", wait_until="domcontentloaded")
            page.locator("#switchLink").click()
            page.locator("#userId").fill(user)
            page.locator("#displayName").fill("SW Upgrade")
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_function("() => document.querySelector('#error')?.textContent?.includes('注册成功')", timeout=15_000)
            page.locator("#password").fill(password)
            page.locator("#submitBtn").click()
            page.wait_for_url("**/app", timeout=15_000)
            if page.locator("#onboardingOverlay").is_visible():
                page.locator("#onboardingSkip").click()

            # 等首次安装的 SW 真正受控，再读出当前的 CACHE_NAME。
            page.wait_for_function("() => !!navigator.serviceWorker?.controller", timeout=20_000)
            v1_cache = page.evaluate("""async () => {
                const names = await caches.keys();
                return names.filter((n) => n.startsWith("momentum-")).sort();
            }""")
            sw_source = context.request.get(f"{origin}/sw.js").text()
            match = re.search(r'CACHE_NAME\s*=\s*"([^"]+)"', sw_source)
            first_cache = match.group(1) if match else ""
            check("首次访问安装了应用缓存", first_cache in v1_cache, f"{first_cache} in {v1_cache}")

            # 模拟「发布新版本」：改一个被预缓存的资源 + 升缓存名。
            next_cache = first_cache.replace("v", "v", 1)
            number = int(re.sub(r"\D", "", first_cache) or "0")
            next_cache = f"momentum-v{number + 1}"
            sw_file = static / "sw.js"
            sw_file.write_text(sw_file.read_text(encoding="utf-8").replace(first_cache, next_cache), encoding="utf-8")
            api_js = static / "js" / "api.js"
            api_js.write_text(MARKER + "\n" + api_js.read_text(encoding="utf-8"), encoding="utf-8")
            # 让 Last-Modified 明确晚于原文件：CI 上仓库刚 checkout，
            # 若改写与原始 mtime 落在同一秒，服务端可能回 304，浏览器就会以为脚本没变，
            # 从而永远发现不了新版本（本地因为检出较久所以看不出来）。
            future = time.time() + 2
            for target in (sw_file, api_js):
                os.utime(target, (future, future))

            # 先确认服务端提供的就是副本里的新资源，否则后面的断言会误导排查方向。
            probe_body = urlopen(f"{origin}/js/api.js?cachebust={secrets.token_hex(4)}", timeout=10).read().decode("utf-8", "replace")
            server_serves_marker = MARKER in probe_body
            check("服务端提供的是副本中的新资源", server_serves_marker, f"marker={server_serves_marker}")

            # 老用户只会普通刷新几次：断言最终拿到新缓存与新资源，且旧缓存被清掉。
            seen_cache: list[str] = []
            marker_seen = False
            reloads_used = 0
            # 显式触发一次更新检查（不等待 install 结束，否则会让紧随其后的 reload 卡住）；
            # 仅靠刷新触发会被浏览器节流：同一 SW 在短时间内只会真正检查一次。
            page.evaluate(
                """() => { navigator.serviceWorker.getRegistration().then((registration) => {"""
                """  if (registration) registration.update();"""
                """}); }"""
            )
            for _ in range(10):
                reloads_used += 1
                page.reload(wait_until="domcontentloaded")
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    page.wait_for_timeout(500)
                    seen_cache = page.evaluate("""async () => {
                        const names = await caches.keys();
                        return names.filter((n) => n.startsWith("momentum-")).sort();
                    }""")
                    if next_cache in seen_cache:
                        break
                page.evaluate("() => navigator.serviceWorker.ready.then(() => true)")
                seen_cache = page.evaluate("""async () => {
                    const names = await caches.keys();
                    return names.filter((n) => n.startsWith("momentum-")).sort();
                }""")
                marker_seen = page.evaluate("""async () => {
                    const response = await fetch("/js/api.js", { cache: "no-store" });
                    const text = await response.text();
                    return text.includes("sw-upgrade-probe-marker");
                }""")
                if next_cache in seen_cache and marker_seen:
                    break
            check("刷新后启用了新版本缓存", next_cache in seen_cache, str(seen_cache))
            check("新资源内容已经生效", marker_seen)
            check("旧版本缓存已被清理", first_cache not in seen_cache, str(seen_cache))
            sw_state = page.evaluate("""async () => {
                const registration = await navigator.serviceWorker.getRegistration();
                return {
                    active: registration?.active?.state || null,
                    waiting: registration?.waiting?.state || null,
                    installing: registration?.installing?.state || null,
                    controlled: !!navigator.serviceWorker.controller,
                };
            }""")
            check("没有卡在 waiting 的旧 SW", sw_state["waiting"] is None and sw_state["active"] == "activated", json.dumps(sw_state))
            diagnostics = {
                "firstCache": first_cache,
                "nextCache": next_cache,
                "observedCaches": seen_cache,
                "markerSeen": marker_seen,
                "serverServesMarker": server_serves_marker,
                "swState": sw_state,
                "reloadsUsed": reloads_used,
                "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
            }
            print("DIAGNOSTICS " + json.dumps(diagnostics, ensure_ascii=False))
            summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as handle:
                    handle.write("### Service Worker upgrade diagnostics" + chr(10) + chr(10))
                    handle.write("~~~json" + chr(10))
                    handle.write(json.dumps(diagnostics, ensure_ascii=False, indent=2))
                    handle.write(chr(10) + "~~~" + chr(10))
            context.close()
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
        shutil.rmtree(workdir, ignore_errors=True)

    failed = [name for name, ok, _ in checks if not ok]
    print("\n================ 结果 ================")
    print(f"通过 {len(checks) - len(failed)}/{len(checks)}")
    for name in failed:
        print("  FAILED:", name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
