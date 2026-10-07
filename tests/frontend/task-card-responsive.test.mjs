import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const css = await readFile(new URL("../../src/momentum_agent/static/app.css", import.meta.url), "utf8");
const tasks = await readFile(new URL("../../src/momentum_agent/static/js/tasks.js", import.meta.url), "utf8");

test("narrow task cards reserve a full flexible title column and move all actions below it", () => {
  assert.match(css, /@media\s*\(max-width:\s*560px\)\s*\{\s*\.task\s*\{\s*grid-template-columns:\s*18px\s+minmax\(0,\s*1fr\);\s*\}\s*\.task\s+\.task-body\s*\{\s*grid-column:\s*2;\s*min-width:\s*0;/);
  assert.match(css, /\.task\s+\.task-actions\s*\{\s*grid-column:\s*2;\s*width:\s*100%;\s*justify-content:\s*flex-start;/);
  assert.match(css, /\.task-title\s*\{[^}]*word-break:\s*break-word;/s);
});

test("task title text is rendered whole and status actions remain present", () => {
  assert.match(tasks, /<div class="task-title">\$\{escapeHtml\(task\.title\)\}<\/div>/);
  for (const action of ["data-edit", "data-start", "data-done", "data-postpone", "data-drop", "data-reopen"]) {
    assert.ok(tasks.includes(action), `task action ${action} must remain available`);
  }
});

test("desktop task grid remains three columns outside the narrow breakpoint", () => {
  assert.match(css, /\.task\s*\{\s*display:\s*grid;\s*grid-template-columns:\s*auto\s+1fr\s+auto;/);
});

