#!/usr/bin/env python3
"""Real Chromium regression for focus-finish recovery.

Run from the repository root with:
    python3 tests/frontend/focus_recovery_browser.py

The test creates a unique, retained SQLite database and artifacts under /tmp.
It uses a fresh Playwright browser context and never reads or prints auth tokens
or passwords. It does not touch the historical database or ports.
"""
from __future__ import annotations

import json
import hashlib
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_ROOT = Path("/tmp/momentum-focus-recovery-e2e")


def choose_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port != 8765:
            return port


def wait_for_server(url: str, process: subprocess.Popen[bytes] | None, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"isolated app server exited with status {process.returncode}")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # startup race only
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"isolated app server did not become ready: {type(last_error).__name__}")


def db_session_count(db_path: Path, user_id: str, session_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT e.payload FROM task_events e
               JOIN tasks t ON t.id = e.task_id
               WHERE e.event_type = 'focus_session' AND t.user_id = ?""",
            (user_id,),
        ).fetchall()
    return sum(
        1
        for (raw,) in rows
        if (lambda value: isinstance(value, dict) and value.get("session_id") == session_id)(
            json.loads(raw or "{}")
        )
    )


def wait_for_text(page, selector: str, expected: str, timeout_ms: int = 12_000) -> None:
    page.wait_for_function(
        "({selector, expected}) => document.querySelector(selector)?.textContent?.includes(expected)",
        arg={"selector": selector, "expected": expected},
        timeout=timeout_ms,
    )


def inspect_browser_state(page, expected_payload: dict | None = None) -> dict:
    return page.evaluate(
        """async (expectedPayload) => {
          const userId = localStorage.getItem('momentum_user');
          const v2Key = `momentum_focus_snapshot_v2:${encodeURIComponent(userId || '')}`;
          const legacyKey = `momentum_focus_snapshot_v1:${encodeURIComponent(userId || '')}`;
          const raw = userId ? localStorage.getItem(v2Key) : null;
          let stored = null;
          try { stored = raw === null ? null : JSON.parse(raw); } catch (_) {}
          const module = await import('/js/focus-recovery.mjs');
          const loaded = userId ? new module.FocusRecoveryStore(userId, localStorage).load() : null;
          const sessionId = expectedPayload?.session_id || stored?.session_id || loaded?.session_id || null;
          const settledKey = sessionId
            ? `momentum_focus_settled_v1:${encodeURIComponent(userId || '')}:${encodeURIComponent(sessionId)}`
            : null;
          const settledRaw = settledKey ? localStorage.getItem(settledKey) : null;
          let settlement = null;
          try { settlement = settledRaw === null ? null : JSON.parse(settledRaw); } catch (_) {}
          const focusKeys = [];
          for (let index = 0; index < localStorage.length; index += 1) {
            const key = localStorage.key(index);
            if (key && key.startsWith('momentum_focus_')) focusKeys.push(key);
          }
          let registration = null;
          if ('serviceWorker' in navigator) {
            const reg = await navigator.serviceWorker.getRegistration();
            registration = reg ? {
              scope: reg.scope,
              installing: reg.installing?.state || null,
              waiting: reg.waiting?.state || null,
              activeState: reg.active?.state || null,
              activeScript: reg.active?.scriptURL || null,
              controller: !!navigator.serviceWorker.controller,
              controllerScript: navigator.serviceWorker.controller?.scriptURL || null,
            } : { scope: null, installing: null, waiting: null, activeState: null,
                  activeScript: null, controller: !!navigator.serviceWorker.controller,
                  controllerScript: navigator.serviceWorker.controller?.scriptURL || null };
          }
          const cacheNames = 'caches' in window ? (await caches.keys()).filter(name => name.startsWith('momentum-')) : [];
          const sameJson = (left, right) => left != null && right != null
            && JSON.stringify(left) === JSON.stringify(right);
          return {
            userKey: { key: 'momentum_user', exists: userId !== null },
            v2: {
              key: 'momentum_focus_snapshot_v2:{owner}', exists: raw !== null,
              ownerMatchesUser: !!stored && stored.user_id === userId,
              sessionMatchesLoaded: !!stored && !!loaded && stored.session_id === loaded.session_id,
              sessionMatchesFinish: !!stored && !!expectedPayload && stored.session_id === expectedPayload.session_id,
              finishPayloadMatchesRequest: !!stored && sameJson(stored.finish_payload, expectedPayload),
              finishPayloadExists: !!stored?.finish_payload,
              parseLoadReturnedSnapshot: !!loaded,
              embeddedOwnerCompare: !!stored && stored.user_id === userId,
            },
            legacy: { key: 'momentum_focus_snapshot_v1:{owner}', exists: localStorage.getItem(legacyKey) !== null },
            settled: {
              key: 'momentum_focus_settled_v1:{owner}:{session}', exists: settledRaw !== null,
              ownerMatchesUser: !!settlement && settlement.user_id === userId,
              sessionMatchesBrowserSession: !!settlement && settlement.session_id === sessionId,
              finishPayloadMatchesRequest: !!settlement && sameJson(settlement.finish_payload, expectedPayload),
            },
            focusKeysPresent: focusKeys.length,
            serviceWorker: registration,
            momentumCacheNames: cacheNames,
          };
        }""",
        expected_payload,
    )


def main() -> int:
    requested_run_dir = os.environ.get("MOMENTUM_E2E_RUN_DIR")
    run_dir = Path(requested_run_dir) if requested_run_dir else ARTIFACTS_ROOT / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "logs").mkdir()
    external_origin = os.environ.get("MOMENTUM_E2E_ORIGIN")
    process = None
    server_log = None
    if external_origin:
        parsed_origin = urlsplit(external_origin)
        if parsed_origin.scheme != "http" or parsed_origin.hostname != "127.0.0.1" or parsed_origin.port in (None, 8765):
            raise ValueError("external test origin must be http://127.0.0.1:<non-8765-port>")
        origin = external_origin.rstrip("/")
        port = parsed_origin.port
        db_override = os.environ.get("MOMENTUM_E2E_DB_PATH")
        if not db_override:
            raise ValueError("MOMENTUM_E2E_DB_PATH is required with an external origin")
        db_path = Path(db_override)
        if not db_path.is_file():
            raise FileNotFoundError("external isolated database does not exist")
    else:
        db_path = run_dir / "isolated.sqlite3"
        port = choose_port()
        origin = f"http://127.0.0.1:{port}"
        database_url = f"sqlite:///{db_path}"
        command = [
            sys.executable, "-m", "momentum_agent",
            "--db", database_url,
            "--log-dir", str(run_dir / "logs"),
            "serve", "--host", "127.0.0.1", "--port", str(port),
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("MOMENTUM_DATABASE_URL", None)
        server_log = (run_dir / "server.log").open("wb")
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)

    browser = None
    context = None
    try:
        wait_for_server(f"{origin}/login.html", process)
        generated_user_id = f"e2e-{secrets.token_hex(8)}"
        generated_password = secrets.token_urlsafe(24)
        task_title = f"隔离恢复验证-{secrets.token_hex(4)}"
        finish_bodies: list[str] = []
        first_failure_forwarded = False
        first_route_hit = False

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path="/usr/bin/chromium",
                args=["--no-sandbox"],
            )
            context = browser.new_context(viewport={"width": 1365, "height": 1000}, service_workers="allow")
            page = context.new_page()
            static_hashes: dict[str, list[dict]] = {}

            def record_static_response(response):
                path = urlsplit(response.url).path
                if path not in {"/js/focus.js", "/js/focus-recovery.mjs", "/sw.js"}:
                    return
                try:
                    body = response.body()
                    static_hashes.setdefault(path, []).append({
                        "status": response.status,
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "bytes": len(body),
                    })
                except Exception:
                    static_hashes.setdefault(path, []).append({"status": response.status, "sha256": None, "bytes": None})

            page.on("response", record_static_response)

            def route_finish(route):
                nonlocal first_failure_forwarded, first_route_hit
                first_route_hit = True
                raw_body = route.request.post_data or ""
                finish_bodies.append(raw_body)
                if len(finish_bodies) == 1:
                    # This response is synthetic and deliberately is not forwarded to the app server.
                    route.fulfill(
                        status=503,
                        content_type="application/json",
                        body='{"error":"isolated test injected one non-forwarded 503"}',
                    )
                    return
                first_failure_forwarded = True
                route.continue_()

            context.route("**/api/focus/finish", route_finish)

            # Register, then log in through the actual page form; credentials remain local variables.
            page.goto(f"{origin}/login.html", wait_until="domcontentloaded")
            page.locator("#switchLink").click()
            page.locator("#userId").fill(generated_user_id)
            page.locator("#displayName").fill("Focus E2E")
            page.locator("#password").fill(generated_password)
            page.locator("#submitBtn").click()
            wait_for_text(page, "#error", "注册成功，请登录")
            page.locator("#password").fill(generated_password)
            page.locator("#submitBtn").click()
            page.wait_for_url("**/app", timeout=15_000)
            page.wait_for_selector("#taskInput")
            if page.locator("#onboardingOverlay").is_visible():
                page.locator("#onboardingSkip").click()

            # Create a task through the real Momentum composer and select it in the focus UI.
            page.locator("#taskInput").fill(task_title)
            page.locator("#addTaskButton").click()
            page.get_by_text(task_title, exact=True).first.wait_for(timeout=15_000)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(
                "title => Array.from(document.querySelector('#focusTaskSelect')?.options || []).some(option => option.textContent === title)",
                arg=task_title,
                timeout=10_000,
            )
            page.locator("#focusTaskSelect").select_option(label=task_title)

            # Capture the actual coordinator's submit result in this Playwright-owned page realm.
            page.evaluate(
                """async () => {
                  const module = await import('/js/focus-recovery.mjs');
                  const prototype = module.FocusFinishCoordinator.prototype;
                  const original = prototype.submit;
                  window.__focusFinishObservations = [];
                  prototype.submit = function (...args) {
                    const promise = original.apply(this, args);
                    promise.then(result => window.__focusFinishObservations.push({
                      snapshotSettled: result.snapshotSettled,
                      outcome: result.outcome,
                    })).catch(() => window.__focusFinishObservations.push({ rejected: true }));
                    return promise;
                  };
                }"""
            )
            page.locator("#focusStartBtn").click()
            page.wait_for_selector("#focusRunning:not(.hidden)", timeout=15_000)
            page.wait_for_function(
                "() => !!localStorage.getItem(`momentum_focus_snapshot_v2:${encodeURIComponent(localStorage.getItem('momentum_user') || '')}`)"
            )
            # Let the real monotonic focus clock record a positive duration.
            page.wait_for_timeout(1600)
            page.locator("#focusStopBtn").click()
            page.wait_for_function("() => window.__focusFinishObservations?.length === 1", timeout=15_000)
            page.wait_for_function("() => document.querySelector('#focusStopBtn')?.textContent.includes('重试保存')", timeout=10_000)
            first_body = finish_bodies[0]
            first_payload = json.loads(first_body)
            user_id_in_browser = page.evaluate("() => localStorage.getItem('momentum_user')")
            assert user_id_in_browser == generated_user_id, "visible authenticated user must be the generated isolated test user"
            after_503 = inspect_browser_state(page, first_payload)
            assert first_route_hit and len(finish_bodies) == 1
            assert not first_failure_forwarded, "synthetic first 503 must not be sent to the app server"
            assert after_503["v2"]["exists"] and after_503["v2"]["finishPayloadExists"]
            assert after_503["v2"]["ownerMatchesUser"] and after_503["v2"]["sessionMatchesFinish"]
            assert after_503["v2"]["finishPayloadMatchesRequest"]
            assert not after_503["settled"]["exists"], "unknown/failed outcome must not write a settlement marker"
            assert not after_503["legacy"]["exists"]
            assert db_session_count(db_path, generated_user_id, first_payload["session_id"]) == 0
            failure_shot = run_dir / "first-503-retry-visible.png"
            page.screenshot(path=str(failure_shot), full_page=True)

            # The UI retry must reuse the persisted payload byte-for-byte; only this attempt reaches SQLite.
            page.locator("#focusStopBtn").click()
            page.wait_for_function("() => window.__focusFinishObservations?.length === 2", timeout=15_000)
            page.wait_for_selector("#focusIdle:not(.hidden)", timeout=15_000)
            page.wait_for_function(
                "() => window.__focusFinishObservations[1]?.snapshotSettled === true",
                timeout=10_000,
            )
            assert len(finish_bodies) == 2 and first_failure_forwarded
            assert finish_bodies[0] == finish_bodies[1], "retry request bytes must exactly equal the frozen first payload"
            assert json.loads(finish_bodies[1]) == first_payload
            assert db_session_count(db_path, generated_user_id, first_payload["session_id"]) == 1

            after_success = inspect_browser_state(page, first_payload)
            assert after_success["settled"]["exists"]
            assert after_success["settled"]["ownerMatchesUser"]
            assert after_success["settled"]["sessionMatchesBrowserSession"]
            assert after_success["settled"]["finishPayloadMatchesRequest"]
            assert not after_success["v2"]["exists"] and not after_success["legacy"]["exists"]
            assert not after_success["v2"]["parseLoadReturnedSnapshot"]
            observed = page.evaluate("() => window.__focusFinishObservations")
            assert observed == [{"rejected": True}, {"snapshotSettled": True, "outcome": first_payload["outcome"]}]

            # Observe SW state from standard page APIs; no browser SDK/DevTools access is used.
            page.evaluate("() => navigator.serviceWorker.ready.then(() => true)")
            try:
                page.wait_for_function(
                    "() => !!navigator.serviceWorker?.getRegistration && !!navigator.serviceWorker.controller",
                    timeout=5000,
                )
            except PlaywrightTimeoutError:
                pass  # Preserve actual registration/controller values in the report if unavailable.
            sw_after_success = after_success["serviceWorker"]
            assert "momentum-v6" in after_success["momentumCacheNames"], "fresh origin must have installed the v6 app cache"

            # Two real hard reloads in the same origin/context must not restore or resubmit the old finish.
            route_count_before_reloads = len(finish_bodies)
            reload_checks = []
            for _ in range(2):
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("#focusIdle")
                page.wait_for_timeout(500)
                visible = page.locator("#focusRecovery").is_visible()
                idle_visible = page.locator("#focusIdle").is_visible()
                current_storage = inspect_browser_state(page, first_payload)
                assert not visible, "settled recovery card must remain absent after reload"
                assert idle_visible
                assert not current_storage["v2"]["exists"] and not current_storage["legacy"]["exists"]
                assert current_storage["settled"]["exists"]
                reload_checks.append({"recoveryCardVisible": visible, "idleVisible": idle_visible})
            assert len(finish_bodies) == route_count_before_reloads == 2, "reload must not issue another finish request"
            assert db_session_count(db_path, generated_user_id, first_payload["session_id"]) == 1
            final_shot = run_dir / "settled-after-two-reloads.png"
            page.screenshot(path=str(final_shot), full_page=True)
            final_storage = inspect_browser_state(page, first_payload)
            final_sw = final_storage["serviceWorker"]

            report = {
                "result": "PASS",
                "origin": origin,
                "testSessionId": first_payload["session_id"],
                "uiVisibleOwnerMatchesLocalStorageUser": user_id_in_browser == generated_user_id,
                "staticResponseHashes": static_hashes,
                "isolatedDatabase": str(db_path),
                "freshOriginFirstInstall": "observed in fresh Chromium origin/context",
                "firstFinish": {"status": 503, "forwarded": False, "settlementMarkerExists": after_503["settled"]["exists"]},
                "retry": {"status": "real app response successful", "sameExactPayloadBytes": finish_bodies[0] == finish_bodies[1], "forwarded": first_failure_forwarded},
                "coordinator": observed,
                "storageAfterFirst503": {
                    "userKey": after_503["userKey"],
                    "v2Snapshot": after_503["v2"],
                    "settledKey": {"key": after_503["settled"]["key"], "exists": after_503["settled"]["exists"]},
                    "legacySnapshot": after_503["legacy"],
                },
                "storageAfterSuccess": {
                    "v2Snapshot": after_success["v2"],
                    "settledKey": after_success["settled"],
                    "legacySnapshot": after_success["legacy"],
                },
                "sqliteFocusSessionRows": db_session_count(db_path, generated_user_id, first_payload["session_id"]),
                "reloads": reload_checks,
                "finishAttemptsTotalIncludingBlockedFirst": len(finish_bodies),
                "serviceWorkerAfterSuccess": sw_after_success,
                "serviceWorkerAfterReloads": final_sw,
                "serviceWorkerCacheNames": after_success["momentumCacheNames"],
                "screenshots": {"first503": str(failure_shot), "afterTwoReloads": str(final_shot)},
                "credentials": "not recorded or printed",
            }
            report_path = run_dir / "report.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"report": str(report_path), **report}, ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"retained_test_directory={run_dir}", file=sys.stderr)
        return 1
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if server_log is not None:
            server_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
