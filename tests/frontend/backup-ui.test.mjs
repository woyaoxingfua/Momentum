import assert from "node:assert/strict";
import test from "node:test";
import {
  exportBackupData,
  formatExportError,
  formatImportError,
  formatImportSuccess,
  importBackupFile,
  MAX_BACKUP_REQUEST_BYTES,
  V2_RESTORE_CONFIRMATION,
} from "../../src/momentum_agent/static/js/backup.mjs";

function makeStatus() {
  return {
    textContent: "",
    hidden: true,
    classes: new Set(),
    classList: {
      toggle(name, force) {
        if (force) this.owner.classes.add(name);
        else this.owner.classes.delete(name);
      },
    },
  };
}

function attachStatusClassList(status) {
  status.classList.owner = status;
  return status;
}

function fileFor(value) {
  const text = JSON.stringify(value);
  return { size: Buffer.byteLength(text), text: async () => text };
}

function v2Backup() {
  return {
    version: "2.0",
    tasks: [],
    relations: [],
    events: [],
    memory: {},
  };
}

test("v1.0 submits {data} without the v2 confirmation and identifies compatibility merge", async () => {
  const status = attachStatusClassList(makeStatus());
  const calls = [];
  let confirmCalls = 0;
  let refreshCalls = 0;
  const payload = { message: "已导入 2 个任务。" };

  await importBackupFile(fileFor({ version: "1.0", tasks: [], memory: {} }), {
    status,
    requestJson: async (...args) => { calls.push(args); return payload; },
    confirmRestore: () => { confirmCalls += 1; return true; },
    afterImport: async () => { refreshCalls += 1; },
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/import");
  assert.equal(calls[0][1].method, "POST");
  assert.deepEqual(JSON.parse(calls[0][1].body), { data: { version: "1.0", tasks: [], memory: {} } });
  assert.equal(confirmCalls, 0);
  assert.equal(refreshCalls, 1);
  assert.match(status.textContent, /v1\.0 兼容合并已完成/);
  assert.doesNotMatch(status.textContent, /完整恢复/);
});

test("v1.0 reports skipped credential count and reason without echoing filtered keys or values", () => {
  const result = formatImportSuccess("1.0", {
    message: "已导入 2 个任务。",
    excluded_memory: {
      count: 2,
      reason: "user_memory 中键名标识为凭据的项目不会导出或导入，以避免备份携带 API key、口令或令牌。",
      filtered_items: {
        OPENAI_API_KEY: "secret-api-key-value",
        account_password: "secret-password-value",
      },
    },
  });

  assert.match(result, /v1\.0 兼容合并已完成/);
  assert.match(result, /敏感凭据已跳过：2 项/);
  assert.match(result, /不会导出或导入，以避免备份携带 API key、口令或令牌/);
  assert.doesNotMatch(result, /OPENAI_API_KEY|account_password|secret-api-key-value|secret-password-value/);
});

test("v1.0 with zero excluded credentials keeps the merge message without a skip notice", () => {
  const result = formatImportSuccess("1.0", {
    message: "已导入 2 个任务。",
    excluded_memory: {
      count: 0,
      reason: "user_memory 中键名标识为凭据的项目不会导出或导入，以避免备份携带 API key、口令或令牌。",
    },
  });

  assert.match(result, /v1\.0 兼容合并已完成/);
  assert.match(result, /已导入 2 个任务/);
  assert.doesNotMatch(result, /敏感凭据已跳过|user_memory 中键名标识为凭据/);
});

test("v2.0 cancellation explains empty target and restore scope without submitting", async () => {
  const status = attachStatusClassList(makeStatus());
  let requestCalls = 0;
  let prompt = "";

  await importBackupFile(fileFor(v2Backup()), {
    status,
    requestJson: async () => { requestCalls += 1; return {}; },
    confirmRestore: (message) => { prompt = message; return false; },
  });

  assert.equal(requestCalls, 0);
  assert.match(prompt, /v2\.0 完整恢复，不是合并导入/);
  assert.match(prompt, /当前登录用户的空数据域/);
  assert.match(prompt, /任务关系/);
  assert.match(prompt, /任务事件/);
  assert.match(prompt, /非凭据用户记忆/);
  assert.match(prompt, /数量未知，不代表为零/);
  assert.match(prompt, /凭据类记忆不会恢复/);
  assert.match(prompt, /16 MiB/);
  assert.match(status.textContent, /已取消.*数据未改动/);
});

test("v2.0 success explains unknown event count and exact excluded memory count/reason", () => {
  const result = formatImportSuccess("2.0", {
    message: "已恢复 4 个任务。",
    excluded_events: {
      count: null,
      reason: "事件没有 user_id，无法安全归属；数量未知，不代表为零。",
    },
    excluded_memory: {
      count: 3,
      reason: "3 个凭据类 memory 项目为保护密钥而排除。",
    },
  });

  assert.match(result, /v2\.0 完整恢复已完成/);
  assert.doesNotMatch(result, /合并导入/);
  assert.match(result, /事件日志未恢复：数量未知；这不表示为零/);
  assert.match(result, /无法安全归属/);
  assert.match(result, /凭据类记忆未恢复：3 项/);
  assert.match(result, /为保护密钥而排除/);
  assert.doesNotMatch(result, /事件[^\n]*数量：?0/);
});

test("v2.0 success treats null event count as unknown, never as zero", () => {
  const result = formatImportSuccess("2.0", {
    message: "已恢复 1 个任务。",
    excluded_events: { count: null, reason: "无法安全归属，数量未知，不代表为零。" },
    excluded_memory: { count: 0, reason: "凭据类记忆排除说明。" },
  });
  assert.match(result, /数量未知/);
  assert.match(result, /不表示为零/);
  assert.match(result, /凭据类记忆未恢复：0 项/);
});

test("409 and 400 failures clearly say no data changed and explain the rejection", () => {
  const conflict = formatImportError(Object.assign(new Error("目标用户数据域必须为空"), { status: 409 }));
  assert.match(conflict, /数据未改动/);
  assert.match(conflict, /数据域必须为空/);

  const invalid = formatImportError(Object.assign(new Error("导入失败：不支持的版本"), { status: 400 }));
  assert.match(invalid, /格式或版本错误/);
  assert.match(invalid, /未执行/);
  assert.match(invalid, /现有数据未改动/);
  assert.match(invalid, /不支持的版本/);
});

test("413 import feedback says file is too large and existing data was not changed", () => {
  const error = Object.assign(new Error("请求体超过备份上限。"), { status: 413 });
  const message = formatImportError(error);
  assert.match(message, /文件过大/);
  assert.match(message, /16 MiB/);
  assert.match(message, /数据未改动/);
  assert.match(formatExportError(error), /导出失败/);
  assert.match(formatExportError(error), /未生成或下载文件/);
});

test("export starts a download only after a valid successful JSON response", async () => {
  const status = attachStatusClassList(makeStatus());
  const events = [];
  const payload = { version: "2.0", tasks: [] };
  const returned = await exportBackupData({
    status,
    requestJson: async (url) => { events.push(["request", url]); return payload; },
    createObjectURL: (blob) => { events.push(["create", blob.type]); return "blob:complete"; },
    download: (url, name) => events.push(["download", url, name]),
    revokeObjectURL: (url) => events.push(["revoke", url]),
  });
  assert.deepEqual(returned, payload);
  assert.deepEqual(events, [
    ["request", "/api/export"],
    ["create", "application/json"],
    ["download", "blob:complete", "momentum-export.json"],
    ["revoke", "blob:complete"],
  ]);
  assert.match(status.textContent, /已生成并开始下载/);
});

test("export 413 and JSON error payloads never create or leave a download", async () => {
  for (const requestJson of [
    async () => { throw Object.assign(new Error("数据超过 16 MiB 上限。"), { status: 413 }); },
    async () => ({ error: "备份导出超出 16 MiB 上限。" }),
  ]) {
    const status = attachStatusClassList(makeStatus());
    let objectUrlCalls = 0;
    let downloadCalls = 0;
    let revokeCalls = 0;
    const result = await exportBackupData({
      status,
      requestJson,
      createObjectURL: () => { objectUrlCalls += 1; return "blob:partial"; },
      download: () => { downloadCalls += 1; },
      revokeObjectURL: () => { revokeCalls += 1; },
    });
    assert.equal(result, null);
    assert.equal(objectUrlCalls, 0);
    assert.equal(downloadCalls, 0);
    assert.equal(revokeCalls, 0);
    assert.match(status.textContent, /16 MiB/);
    assert.match(status.textContent, /未生成或下载文件/);
    assert.ok(status.classes.has("error-text"));
  }
});

test("invalid JSON and unsupported versions never reach the import endpoint", async () => {
  for (const [file, expected] of [
    [{ size: 8, text: async () => "{broken" }, /不是有效 JSON/],
    [fileFor({ version: "9.0" }), /不支持的备份版本/],
  ]) {
    const status = attachStatusClassList(makeStatus());
    let calls = 0;
    await importBackupFile(file, {
      status,
      requestJson: async () => { calls += 1; return {}; },
      confirmRestore: () => true,
    });
    assert.equal(calls, 0);
    assert.match(status.textContent, expected);
    assert.ok(status.classes.has("error-text"));
  }
});

test("files above the 16 MiB hard limit are rejected before POST", async () => {
  const status = attachStatusClassList(makeStatus());
  let calls = 0;
  await importBackupFile({ size: MAX_BACKUP_REQUEST_BYTES + 1, text: async () => "{}" }, {
    status,
    requestJson: async () => { calls += 1; return {}; },
  });
  assert.equal(calls, 0);
  assert.match(status.textContent, /16 MiB/);
  assert.match(status.textContent, /数据未改动/);
});

test("serialized import request bodies above 16 MiB are rejected before POST", async () => {
  const status = attachStatusClassList(makeStatus());
  let calls = 0;
  const text = JSON.stringify({ version: "1.0", tasks: [], memory: {}, padding: "x".repeat(MAX_BACKUP_REQUEST_BYTES) });
  await importBackupFile({ size: 10, text: async () => text }, {
    status,
    requestJson: async () => { calls += 1; return {}; },
  });
  assert.equal(calls, 0);
  assert.match(status.textContent, /16 MiB/);
  assert.match(status.textContent, /数据未改动/);
});
