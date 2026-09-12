/**
 * Self-test harness for pi_extensions/naumi-analysis.js
 *
 * Loads the extension with a stubbed pi API, invokes each registered tool's
 * execute() against a fixture directory, and asserts the Python scanner
 * really ran and returned structured JSON evidence.
 *
 * Usage: NAUMI_PYTHON=<python> node pi_extensions/selftest.mjs <fixture-dir>
 */

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

const fixtureDir = process.argv[2];
assert.ok(fixtureDir, "用法: node selftest.mjs <fixture-dir>");

const registered = [];
const api = {
  registerTool: (tool) => registered.push(tool),
  registerCommand: () => {},
  on: () => {},
};

const moduleUrl = pathToFileURL(
  resolve(import.meta.dirname, "naumi-analysis.js")
).href;
const mod = await import(moduleUrl);
assert.equal(typeof mod.default, "function", "扩展必须 default export 一个工厂函数");
await mod.default(api);

assert.equal(registered.length, 3, "应注册 3 个工具");
for (const name of ["naumi_chaos", "naumi_scale", "naumi_state"]) {
  assert.ok(registered.some((tool) => tool.name === name), `缺少工具 ${name}`);
}

const python = process.env.NAUMI_PYTHON || "python3";
const probe = spawnSync(python, ["-c", "import naumi_agent"], { encoding: "utf-8" });
assert.equal(
  probe.status,
  0,
  `${python} 无法导入 naumi_agent（先安装：uv sync 或 pip install -e .）`
);

for (const tool of registered) {
  const params = { target: fixtureDir };
  if (tool.name === "naumi_scale") params.qps = 500;
  const result = await tool.execute("selftest-call", params, undefined);
  assert.ok(result?.content?.[0]?.text, `${tool.name} 必须返回文本内容`);
  const payload = JSON.parse(result.content[0].text);
  assert.equal(payload.ok, true, `${tool.name} 扫描应成功`);
  assert.equal(payload.mode, tool.name.replace("naumi_", ""));
  assert.ok(payload.files_scanned >= 1, `${tool.name} 应扫描到源码文件`);
  assert.ok(payload.report.length > 0, `${tool.name} 报告不能为空`);
  if (tool.name === "naumi_scale") assert.equal(payload.qps, 500);
  console.log(`PASS ${tool.name}: files=${payload.files_scanned}`);
}

// Missing target must fail closed with a clear message.
const chaos = registered.find((tool) => tool.name === "naumi_chaos");
const missing = await chaos.execute("selftest-missing", {}, undefined);
assert.equal(missing.isError, true, "缺少 target 时应返回错误");
console.log("PASS missing-target guard");

console.log("ALL PASS");
