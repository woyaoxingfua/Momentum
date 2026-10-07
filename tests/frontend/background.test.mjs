import assert from "node:assert/strict";
import test from "node:test";

import {
  applyBackground,
  cssImageValue,
  isPublicImageUrl,
  normalizeBackgroundUrl,
  opacityToCss,
} from "../../src/momentum_agent/static/js/background.mjs";


test("normalizes valid absolute and relative HTTP image URLs", () => {
  assert.equal(
    normalizeBackgroundUrl("  https://cdn.example/image.png  "),
    "https://cdn.example/image.png",
  );
  assert.equal(
    normalizeBackgroundUrl("images/wallpaper.png", "https://app.example/settings"),
    "https://app.example/images/wallpaper.png",
  );
});

test("rejects executable schemes and embedded credentials", () => {
  assert.throws(() => normalizeBackgroundUrl("javascript:alert(1)"), /只支持 HTTP 或 HTTPS/);
  assert.throws(() => normalizeBackgroundUrl("data:image/png;base64,abc"), /只支持 HTTP 或 HTTPS/);
  assert.throws(() => normalizeBackgroundUrl("https://user:pass@example.com/a.png"), /不能包含账号或密码/);
});

test("allows public image hosts and rejects local-network URLs", () => {
  assert.equal(isPublicImageUrl("https://images.example.org/wallpaper.jpg"), true);
  assert.equal(isPublicImageUrl("http://127.0.0.1/photo.png"), false);
  assert.equal(isPublicImageUrl("http://192.168.1.10/photo.png"), false);
  assert.equal(isPublicImageUrl("http://localhost/photo.png"), false);
});

test("escapes URL characters before composing CSS url()", () => {
  const url = normalizeBackgroundUrl('https://example.com/a"b.png');
  assert.equal(url, "https://example.com/a%22b.png");
  assert.equal(cssImageValue(url), 'url("https://example.com/a%22b.png")');
});

test("clamps opacity to a safe 0–100 percent range", () => {
  assert.equal(opacityToCss("15"), 0.15);
  assert.equal(opacityToCss("-20"), 0);
  assert.equal(opacityToCss("120"), 1);
  assert.equal(opacityToCss("invalid"), 0.15);
});

test("applyBackground resets or safely applies CSS variables", () => {
  const values = new Map();
  const root = { style: { setProperty: (key, value) => values.set(key, value) } };
  applyBackground(root, "https://cdn.example/photo.jpg", "35");
  assert.equal(values.get("--bg-image"), 'url("https://cdn.example/photo.jpg")');
  assert.equal(values.get("--bg-opacity"), "0.35");
  applyBackground(root, "", 0);
  assert.equal(values.get("--bg-image"), "none");
  assert.equal(values.get("--bg-opacity"), "0");
  applyBackground(root, "blob:https://app.example/preview", "50");
  assert.equal(values.get("--bg-image"), 'url("blob:https://app.example/preview")');
});


test("desktop and mobile settings expose the bound background controls", async () => {
  const { readFile } = await import("node:fs/promises");
  const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
  for (const id of [
    "configBgUrl", "configBgFile", "configBgOpacity", "configBgApply", "configBgClear", "configBgStatus",
    "mobileConfigBgUrl", "mobileConfigBgFile", "mobileConfigBgOpacity", "mobileConfigBgApply", "mobileConfigBgClear", "mobileConfigBgStatus",
  ]) {
    assert.ok(html.includes(`id="${id}"`), `missing UI control ${id}`);
  }
});
