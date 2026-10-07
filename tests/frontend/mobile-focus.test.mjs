import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
const css = await readFile(new URL("../../src/momentum_agent/static/app.css", import.meta.url), "utf8");

test("mobile navigation provides a direct, accessible jump to the existing focus timer", () => {
  assert.match(html, /<a class="mobile-nav-item" href="#focusTimer" aria-label="专注">[\s\S]*?<span>专注<\/span>[\s\S]*?<\/a>/);
  assert.equal((html.match(/id="focusTimer"/g) || []).length, 1, "the anchor targets one existing timer");
});

test("mobile focus controls reserve space above the fixed nav and respect the safe area", () => {
  assert.match(css, /--safe-bottom:\s*env\(safe-area-inset-bottom,\s*0px\)/);
  assert.match(css, /\.mobile-nav\s*\{\s*display:\s*none;\s*position:\s*fixed;\s*bottom:\s*0;[\s\S]*?padding-bottom:\s*calc\(var\(--space-1\) \+ var\(--safe-bottom\)\);/);
  assert.match(css, /\.shell\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\);\s*gap:\s*0;\s*padding:\s*0 0 calc\(84px \+ var\(--safe-bottom\)\);/);
  assert.match(html, /<select id="focusTaskSelect">[\s\S]*?<\/select>/);
  assert.match(html, /<button id="focusStartBtn"[^>]*>专注<\/button>/);
});

test("narrow layouts show only the focus card from the otherwise-hidden coach rail", () => {
  assert.match(css, /@media\s*\(max-width:\s*960px\)[\s\S]*?\.coach\s*\{[\s\S]*?display:\s*block[\s\S]*?\}\s*\.coach\s*>\s*:not\(\.focus-timer\)\s*\{\s*display:\s*none\s*!important;/);
  assert.match(css, /\.focus-timer\s*\{\s*margin:\s*0 18px 20px;/);
});
