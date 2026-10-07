import assert from "node:assert/strict";
import test from "node:test";

import { renderMarkdown, sanitizeMarkdownUrl } from "../../src/momentum_agent/static/js/chat.js";

test("keeps ordinary http/https/mailto/tel and site-relative links", () => {
  assert.equal(sanitizeMarkdownUrl("https://example.com/a?b=1"), "https://example.com/a?b=1");
  assert.equal(sanitizeMarkdownUrl("http://example.com"), "http://example.com");
  assert.equal(sanitizeMarkdownUrl("mailto:a@b.com"), "mailto:a@b.com");
  assert.equal(sanitizeMarkdownUrl("tel:+8613800000000"), "tel:+8613800000000");
  assert.equal(sanitizeMarkdownUrl("/app?tab=today"), "/app?tab=today");
  assert.equal(sanitizeMarkdownUrl("#section"), "#section");
});

test("rejects executable and non-web schemes", () => {
  for (const bad of [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "  javascript:alert(1)  ",
    "data:text/html;base64,PHNjcmlwdD4=",
    "data:image/png;base64,AAAA",
    "vbscript:msgbox(1)",
    "file:///C:/Windows/System32/calc.exe",
    "blob:https://example.com/uuid",
  ]) {
    assert.equal(sanitizeMarkdownUrl(bad), "", `expected ${bad} to be rejected`);
  }
});

test("rejects schemes obfuscated with control characters", () => {
  assert.equal(sanitizeMarkdownUrl("java\tscript:alert(1)"), "");
  assert.equal(sanitizeMarkdownUrl("java\nscript:alert(1)"), "");
  assert.equal(sanitizeMarkdownUrl("\u0000javascript:alert(1)"), "");
});

test("renderMarkdown never emits a javascript: href or src", () => {
  const html = renderMarkdown("点[这里](javascript:alert(1))和![图](javascript:alert(2))");
  assert.equal(/javascript:/i.test(html), false, html);
  assert.equal(html.includes("<a "), false, html);
  assert.equal(html.includes("<img"), false, html);
  assert.equal(html.includes("这里"), true);
  assert.equal(html.includes("图"), true);
});

test("renderMarkdown emits safe links with noopener and keeps text", () => {
  const html = renderMarkdown("[文档](https://example.com/doc)");
  assert.equal(html.includes('<a target="_blank" rel="noopener noreferrer" href="https://example.com/doc">'), true, html);
  assert.equal(html.includes("文档</a>"), true, html);
});

test("renderMarkdown still escapes raw HTML in link labels", () => {
  const html = renderMarkdown("[<img src=x onerror=alert(1)>](https://example.com)");
  assert.equal(html.includes("<img src=x"), false, html);
  assert.equal(html.includes("&lt;img"), true, html);
});
