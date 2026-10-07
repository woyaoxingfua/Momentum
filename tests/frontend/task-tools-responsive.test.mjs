import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
const css = await readFile(new URL("../../src/momentum_agent/static/app.css", import.meta.url), "utf8");

test("narrow task toolbar gives search its own row and allows secondary actions to wrap", () => {
  assert.match(css, /@media\s*\(max-width:\s*560px\)\s*\{\s*\.heading-actions\s*\{\s*flex-wrap:\s*wrap;\s*overflow:\s*visible;/);
  assert.match(css, /\.heading-actions\s+\.search-input\s*\{\s*order:\s*-1;\s*flex:\s*0 0 100%;\s*width:\s*100%;\s*min-width:\s*0;/);
});

test("search, refresh, export, and import controls remain available", () => {
  for (const id of ["searchInput", "refreshButton", "exportButton", "importFile"]) {
    assert.ok(html.includes(`id="${id}"`), `missing task tool ${id}`);
  }
  assert.match(html, /<a href="\/stats\.html"[^>]*>仪表盘<\/a>/);
});
