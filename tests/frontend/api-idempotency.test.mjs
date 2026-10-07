import assert from "node:assert/strict";
import test from "node:test";
import { requestJson } from "../../src/momentum_agent/static/js/api.js";

function jsonResponse(payload = { ok: true }) {
  return { status: 200, ok: true, json: async () => payload };
}

async function withBrowserMocks(run) {
  const originalStorage = globalThis.localStorage;
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const values = new Map([["momentum_token", "mock-auth-token"]]);
  globalThis.localStorage = {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  globalThis.window = { location: { href: "" } };
  try { return await run(); }
  finally {
    globalThis.localStorage = originalStorage;
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
}

test("requestJson merges Idempotency-Key with existing Authorization and Content-Type headers", async () => {
  await withBrowserMocks(async () => {
    let captured;
    globalThis.fetch = async (url, options) => {
      captured = { url, options };
      return jsonResponse();
    };
    await requestJson("/api/tasks/41/postpone", {
      method: "POST",
      headers: { "Idempotency-Key": "11111111-1111-4111-8111-111111111111" },
      body: '{"days":1}',
    });
    assert.equal(captured.options.headers.Authorization, "Bearer mock-auth-token");
    assert.equal(captured.options.headers["Content-Type"], "application/json");
    assert.equal(captured.options.headers["Idempotency-Key"], "11111111-1111-4111-8111-111111111111");
    assert.equal(captured.options.body, '{"days":1}');
  });
});

test("in-flight write dedup reuses the same idempotency key but never coalesces distinct intents", async () => {
  await withBrowserMocks(async () => {
    const calls = [];
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    globalThis.fetch = async (url, options) => {
      calls.push({ url, options });
      await gate;
      return jsonResponse();
    };
    const request = (key) => requestJson("/api/tasks/41/postpone", {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: '{"days":1}',
    });
    const one = request("11111111-1111-4111-8111-111111111111");
    const duplicate = request("11111111-1111-4111-8111-111111111111");
    const separateIntent = request("22222222-2222-4222-8222-222222222222");
    assert.equal(calls.length, 2);
    assert.deepEqual(calls.map(({ options }) => options.headers["Idempotency-Key"]), [
      "11111111-1111-4111-8111-111111111111",
      "22222222-2222-4222-8222-222222222222",
    ]);
    release();
    await Promise.all([one, duplicate, separateIntent]);
  });
});
