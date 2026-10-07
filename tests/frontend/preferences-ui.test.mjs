import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
const backgroundJs = await readFile(new URL("../../src/momentum_agent/static/js/background.mjs", import.meta.url), "utf8");
const serviceWorkerJs = await readFile(new URL("../../src/momentum_agent/static/sw.js", import.meta.url), "utf8");
const css = await readFile(new URL("../../src/momentum_agent/static/app.css", import.meta.url), "utf8");

test("desktop and mobile settings expose city search, weather test, and theme choices", () => {
  for (const id of [
    "configCityInput", "configCityResults", "configCityTest", "configCitySave", "configCityStatus",
    "mobileConfigCityInput", "mobileConfigCityResults", "mobileConfigCityTest", "mobileConfigCitySave", "mobileConfigCityStatus",
    "configThemeSelect", "mobileConfigThemeSelect", "configThemeImportFile", "mobileConfigThemeImportFile",
  ]) assert.ok(html.includes(`id="${id}"`), `missing ${id}`);
  assert.match(html, /纸页 · 暖白/);
  assert.match(html, /苔绿 · 静谧/);
  assert.match(html, /暂不能同步云端/);
});

test("expanded desktop settings keep the fixed coach controls reachable by scrolling", () => {
  assert.match(css, /@media \(min-width: 961px\)/);
  assert.match(css, /\.coach \{[^}]*overflow-x: hidden;[^}]*overflow-y: auto;/s);
  assert.match(css, /\.coach > \.config-panel\[open\] \{[^}]*flex: 0 0 auto;[^}]*overflow: visible;/s);
  assert.match(css, /\.coach > \.config-panel\[open\] > summary \{[^}]*position: sticky;/s);
});

test("background image bytes use IndexedDB and cloud payload is metadata-only", () => {
  assert.match(backgroundJs, /indexedDBRef\.open\(DB_NAME/);
  assert.match(backgroundJs, /source, url, opacity/);
  assert.doesNotMatch(backgroundJs, /new FormData\(/);
  assert.doesNotMatch(backgroundJs, /readAsDataURL/);
  assert.match(backgroundJs, /图片内容不会上传/);
});

test("service worker refreshes and precaches the current preference and recovery modules", () => {
  assert.match(serviceWorkerJs, /momentum-v7/);
  for (const module of ["appearance.mjs", "background.mjs", "city.mjs", "focus-clock.mjs", "focus-recovery.mjs"]) {
    assert.ok(serviceWorkerJs.includes(`/js/${module}`), `missing cached module ${module}`);
  }
});
