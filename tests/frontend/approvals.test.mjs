import assert from "node:assert/strict";
import test from "node:test";

import {
  configureApprovals,
  formatApprovalLabel,
  loadPendingApprovals,
  approvePending,
  rejectPending,
} from "../../src/momentum_agent/static/js/approvals.mjs";

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.textContent = "";
    this.className = "";
    this.hidden = false;
    this.style = {};
    this.attributes = {};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  setAttribute(name, value) { this.attributes[name] = value; }
  async click() { return this.listeners.get("click")?.(); }
}

function setup(items, calls = []) {
  const panel = new FakeElement("section");
  const list = new FakeElement("ul");
  const request = async (url, options) => {
    calls.push({ url, options });
    if (url === "/api/approvals") return { approvals: items, gated_tools: ["drop_task"] };
    return { message: "ok" };
  };
  configureApprovals({
    elements: { approvalsPanel: panel, approvalsList: list },
    request,
    createElement: (tag) => new FakeElement(tag),
  });
  return { panel, list, calls };
}

test("formatApprovalLabel 优先用后端给的摘要", () => {
  assert.equal(formatApprovalLabel({ id: "a1", summary: "放弃任务 #3" }), "放弃任务 #3");
  assert.match(formatApprovalLabel({ id: "a1" }), /a1/);
});

test("有待确认项时渲染每一行并显示面板", async () => {
  const { panel, list } = setup([
    { id: "a1", summary: "放弃任务 #1" },
    { id: "a2", summary: "放弃任务 #2" },
  ]);
  const items = await loadPendingApprovals();
  assert.equal(items.length, 2);
  assert.equal(list.children.length, 2);
  assert.equal(list.children[0].attributes["data-approval-id"], "a1");
  assert.equal(panel.hidden, false);
  const labels = list.children[0].children.map((child) => child.textContent);
  assert.deepEqual(labels, ["放弃任务 #1", "批准", "取消"]);
});

test("没有待确认项时隐藏面板", async () => {
  const { panel, list } = setup([]);
  await loadPendingApprovals();
  assert.equal(list.children.length, 0);
  assert.equal(panel.hidden, true);
  assert.equal(panel.style.display, "none");
});

test("批准会 POST 该编号并刷新列表", async () => {
  const calls = [];
  setup([{ id: "a1", summary: "放弃任务 #1" }], calls);
  await approvePending("a1");
  const post = calls.find((call) => call.url === "/api/approvals/approve");
  assert.ok(post, "应当调用批准接口");
  assert.equal(post.options.method, "POST");
  assert.equal(JSON.parse(post.options.body).id, "a1");
  assert.ok(calls.some((call) => call.url === "/api/approvals"), "批准后应重新拉取列表");
});

test("取消会 POST 到 reject 接口", async () => {
  const calls = [];
  setup([{ id: "a2", summary: "放弃任务 #2" }], calls);
  await rejectPending("a2");
  const post = calls.find((call) => call.url === "/api/approvals/reject");
  assert.ok(post, "应当调用取消接口");
  assert.equal(JSON.parse(post.options.body).id, "a2");
});

test("接口失败时不抛异常、面板保持隐藏", async () => {
  const panel = new FakeElement("section");
  const list = new FakeElement("ul");
  configureApprovals({
    elements: { approvalsPanel: panel, approvalsList: list },
    request: async () => { throw new Error("offline"); },
    createElement: (tag) => new FakeElement(tag),
  });
  assert.deepEqual(await loadPendingApprovals(), []);
});
