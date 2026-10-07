import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
const css = await readFile(new URL("../../src/momentum_agent/static/app.css", import.meta.url), "utf8");

function extractDiv(source, openingTag) {
  const start = source.indexOf(openingTag);
  assert.notEqual(start, -1, `expected markup: ${openingTag}`);

  const tags = /<\/?div\b[^>]*>/gi;
  tags.lastIndex = start;
  let depth = 0;
  let match;
  while ((match = tags.exec(source))) {
    if (/^<\/div/i.test(match[0])) depth -= 1;
    else if (!/\/>$/.test(match[0])) depth += 1;
    if (depth === 0) return source.slice(start, tags.lastIndex);
  }
  throw new Error(`unclosed div: ${openingTag}`);
}

function extractCssBlock(source, marker) {
  const markerIndex = source.lastIndexOf(marker);
  assert.notEqual(markerIndex, -1, `expected CSS marker: ${marker}`);
  const openingBrace = source.indexOf("{", markerIndex);
  assert.notEqual(openingBrace, -1, `expected CSS block after: ${marker}`);

  let depth = 0;
  for (let index = openingBrace; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(openingBrace + 1, index);
    }
  }
  throw new Error(`unclosed CSS block: ${marker}`);
}

const toolbarMarkup = extractDiv(html, '<div class="heading-actions">');
const mobileToolbarCss = extractCssBlock(css, "/* Mobile task toolbar:");

test("mobile task toolbar wraps at 600px and gives search its own shrinkable full-width row", () => {
  assert.match(css.slice(css.lastIndexOf("/* Mobile task toolbar:")), /@media\s*\(max-width:\s*600px\)/);
  assert.match(mobileToolbarCss, /\.heading-actions\s*\{[^}]*width:\s*100%;[^}]*flex-wrap:\s*wrap;[^}]*overflow:\s*visible;/s);
  assert.doesNotMatch(mobileToolbarCss, /overflow-x:\s*(?:auto|hidden)|display:\s*none/);
  assert.match(mobileToolbarCss, /\.heading-actions\s+\.search-input\s*\{[^}]*order:\s*-1;[^}]*flex:\s*0\s+1\s+100%;[^}]*width:\s*100%;[^}]*min-width:\s*0;[^}]*max-width:\s*100%;/s);
  assert.match(mobileToolbarCss, /\.heading-actions\s*>\s*\.due-on-filter-chip\s*\{[^}]*max-width:\s*100%;[^}]*flex-wrap:\s*wrap;[^}]*white-space:\s*normal;/s);
});

test("all existing task toolbar controls remain present inside the wrapping toolbar", () => {
  const controls = [
    ['href="/stats.html"', "dashboard link"],
    ['id="dueDateFilterButton"', "due-date filter"],
    ['id="dueOnFilterChip"', "date filter chip"],
    ['id="clearDueOnFilterButton"', "clear date filter"],
    ['id="unestimatedDueFilterChip"', "unestimated filter chip"],
    ['id="clearUnestimatedDueFilterButton"', "clear unestimated filter"],
    ['id="searchInput"', "search field"],
    ['id="refreshButton"', "refresh button"],
    ['id="exportButton"', "export button"],
  ];
  for (const [fragment, label] of controls) {
    assert.ok(toolbarMarkup.includes(fragment), `${label} remains in the task toolbar`);
  }
  assert.match(toolbarMarkup, /<label class="import-label"[^>]*>[\s\S]*?<input id="importFile"[^>]*hidden[\s\S]*?<\/label>/);
});

test("desktop toolbar and search base dimensions remain unchanged", () => {
  const baseStyles = css.slice(0, css.indexOf("/* Responsive */"));
  assert.match(baseStyles, /\.heading-actions\s*\{\s*display:\s*flex;[\s\S]*?align-items:\s*center;[\s\S]*?flex-shrink:\s*0;\s*\}/);
  assert.match(baseStyles, /\.search-input\s*\{\s*min-width:\s*160px;\s*width:\s*200px;\s*\}/);
});
