import assert from "node:assert/strict";
import test from "node:test";
import { BUILT_IN_THEMES, isSafePalette } from "../../src/momentum_agent/static/js/appearance.mjs";

const palette = {
  bg: "#f3f0e8", bg2: "#ebe6dc", surface: "#fffdf8", surface2: "#f7f2e8",
  surface3: "#eee7da", border: "#e0d8c8", border2: "#cfc5b3", text: "#252921",
  text2: "#5d6257", text3: "#8a8d80", accent: "#426b56", accent2: "#315441",
};

test("built-in themes provide several distinct choices", () => {
  assert.deepEqual(BUILT_IN_THEMES, ["paper", "ink", "sage", "midnight"]);
});

test("custom theme accepts only a complete allowlisted hex-color palette", () => {
  assert.equal(isSafePalette(palette), true);
  assert.equal(isSafePalette({ ...palette, script: "alert(1)" }), false);
  assert.equal(isSafePalette({ ...palette, accent: "url(javascript:alert(1))" }), false);
  assert.equal(isSafePalette({ ...palette, accent: "#fff" }), false);
  assert.equal(isSafePalette("<style>body{color:red}</style>"), false);
});
