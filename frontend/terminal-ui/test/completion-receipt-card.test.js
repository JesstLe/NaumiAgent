import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, color, configureAnsiColors, stripAnsi, visibleWidth } from "../src/ansi.js";
import { CompletionReceiptCard } from "../src/components/completion-receipt-card.js";
import { renderComponent } from "../src/components/core.js";

test("completion receipt colors every Git change status by semantic meaning", () => {
  const rendered = renderReceipt({
    changes: [
      change("added"),
      change("untracked"),
      change("deleted"),
      change("removed_untracked"),
      change("modified"),
      change("renamed"),
      change("copied"),
      change("conflicted"),
      change("restored"),
    ],
  });
  const output = rendered.join("\n");
  const plain = stripAnsi(output);

  assert(output.includes(color(ANSI.green, "新增 2 个文件")));
  assert(output.includes(color(ANSI.red, "删除 2 个文件")));
  assert(output.includes(color(ANSI.yellow, "修改 1 个文件")));
  assert(output.includes(color(ANSI.cyan, "重命名 1 个文件")));
  assert(output.includes(color(ANSI.cyan, "复制 1 个文件")));
  assert(output.includes(color(`${ANSI.bold}${ANSI.red}`, "冲突 1 个文件")));
  assert(rendered.find((line) => stripAnsi(line).includes("还原")).includes(ANSI.blue));
  assert.match(plain, /影响 · 删除 2 个文件 · 新增 2 个文件 · 修改 1 个文件/);
  assert(rendered.every((line) => visibleWidth(line) <= 100));
});

test("completion receipt colors branch and dirty divergence independently", () => {
  const output = renderReceipt({
    changes: [change("modified")],
    git_state: {
      available: true,
      branch: "codex/colors",
      dirty: true,
      ahead: 2,
      behind: 3,
    },
  }).join("\n");

  assert(output.includes(color(ANSI.cyan, "Git codex/colors")));
  assert(output.includes(color(ANSI.yellow, "工作区有改动")));
  assert(output.includes(color(ANSI.green, "领先 2")));
  assert(output.includes(color(ANSI.red, "落后 3")));
});

test("completion receipt colors a clean Git workspace green", () => {
  const output = renderReceipt({
    changes: [],
    outcome: "failed",
    git_state: {
      available: true,
      branch: "main",
      dirty: false,
      ahead: 0,
      behind: 1,
    },
  }).join("\n");

  assert(output.includes(color(ANSI.green, "工作区干净")));
  assert(output.includes(color(ANSI.red, "落后 1")));
});

test("completion receipt renders verified Harness counts and de-duplicates evidence", () => {
  const output = renderReceipt({}, {
    status: "completed_verified",
    checks: [
      { id: "unit", status: "passed" },
      { id: "lint", status: "passed" },
    ],
    criteria: [
      { id: "tests", status: "satisfied", evidence_ids: ["check:unit", "file:a"] },
      { id: "quality", status: "satisfied", evidence_ids: ["check:unit"] },
    ],
  }).join("\n");

  assert(output.includes(color(ANSI.green, "Harness 已验证")));
  assert.match(stripAnsi(output), /Harness 已验证 · 检查 2\/2 · 准则 2\/2 · 证据 2/);
});

test("completion receipt distinguishes Harness infrastructure errors from failed checks", () => {
  const output = renderReceipt({}, {
    status: "completed_unverified",
    checks: [
      { id: "unit", status: "failed" },
      { id: "integration", status: "infrastructure_error" },
      { id: "policy", status: "blocked_by_policy" },
    ],
    criteria: [{ id: "tests", status: "unsatisfied", evidence_ids: [] }],
  }).join("\n");
  const plain = stripAnsi(output);

  assert(output.includes(color(ANSI.yellow, "Harness 未验证")));
  assert(output.includes(color(ANSI.red, "检查失败 · unit")));
  assert(output.includes(color(ANSI.yellow, "基础设施异常 · integration")));
  assert.doesNotMatch(plain, /检查失败 · integration/);
  assert.match(plain, /另有 1 项未通过检查/);
  assert.match(plain, /准则未满足 · 0\/1/);
});

test("completion receipt renders blocked Harness warnings with bounded detail", () => {
  const output = renderReceipt({}, {
    status: "blocked",
    checks: [{ id: "required", status: "missing" }],
    criteria: [],
    warnings: ["缺少受信检查", "工作区已变化", "第三条提示不直接展开"],
  }).join("\n");
  const plain = stripAnsi(output);

  assert(output.includes(color(ANSI.red, "Harness 阻塞")));
  assert(output.includes(color(ANSI.yellow, "缺少检查 · required")));
  assert.match(plain, /Harness 警告 · 缺少受信检查/);
  assert.match(plain, /Harness 警告 · 工作区已变化/);
  assert.match(plain, /另有 1 条 Harness 警告/);
  assert.doesNotMatch(plain, /第三条提示不直接展开/);
});

test("combined completion receipt stays bounded at 80 120 and 200 columns", () => {
  const harnessReceipt = {
    run_id: "run-bounded-detail",
    revision: 1,
    status: "completed_unverified",
    checks: [
      { id: "包含中文字符的定向集成测试", status: "infrastructure_error" },
      { id: "policy-check", status: "blocked_by_policy" },
    ],
    criteria: [{ id: "验收准则", status: "unsatisfied", evidence_ids: [] }],
    warnings: ["这是用于验证中文宽字符折行不会越过完成回执边框的较长提示。"],
  };

  for (const width of [80, 120, 200]) {
    const rendered = renderReceipt({
      run_id: "run-bounded-detail",
      summary: "完成回执包含通用运行事实和权威 Harness 结果，窄屏时仍应保持清晰。",
    }, harnessReceipt, width);
    assert(rendered.every((line) => visibleWidth(line) <= width));
    assert.match(rendered.map(stripAnsi).join("\n"), /Harness 未验证/);
    assert.match(rendered.map(stripAnsi).join("\n"), /Ctrl\+O 查看详情/);
  }
});

test("generic completion receipt remains compatible without a Harness peer", () => {
  const plain = renderReceipt().map(stripAnsi).join("\n");
  assert.doesNotMatch(plain, /Harness/);
  assert.match(plain, /完成回执/);
  assert.match(plain, /操作 · \/copy receipt receipt-test/);
});

test("paired Harness receipt exposes exact detail command and direct shortcut", () => {
  const plain = renderReceipt({
    run_id: "run-detail-1",
  }, {
    run_id: "run-detail-1",
    revision: 2,
    status: "completed_verified",
    checks: [],
    criteria: [],
  }).map(stripAnsi).join("\n");

  assert.match(plain, /操作 · Ctrl\+O 查看详情 · \/harness detail run-detail-1/);
  assert.match(plain, /操作 · \/copy receipt receipt-test/);
});

test("mismatched or unversioned Harness receipt does not advertise detail", () => {
  for (const harnessReceipt of [
    { run_id: "other-run", revision: 1, status: "completed_verified" },
    { run_id: "run-detail-1", revision: 0, status: "completed_verified" },
  ]) {
    const plain = renderReceipt(
      { run_id: "run-detail-1" },
      harnessReceipt,
    ).map(stripAnsi).join("\n");
    assert.doesNotMatch(plain, /Ctrl\+O|\/harness detail/);
  }
});

test("completion receipt copy action strips terminal controls from receipt id", () => {
  const output = renderReceipt({
    receipt_id: "receipt-\u001b]8;;https://evil.invalid\u0007link\u001b]8;;\u0007-\u001b[31mred",
  }).join("\n");
  const plain = stripAnsi(output);

  assert.doesNotMatch(output, /\u001b\]8/);
  assert.doesNotMatch(plain, /evil\.invalid/);
  assert.match(plain, /\/copy receipt receipt-link-red/);
});

test("completion receipt copy action quotes receipt ids with spaces", () => {
  const plain = renderReceipt({
    receipt_id: "receipt with spaces",
  }).map(stripAnsi).join("\n");

  assert.match(plain, /\/copy receipt 'receipt with spaces'/);
});

test("Harness meaning remains explicit when terminal colors are disabled", () => {
  try {
    configureAnsiColors(false);
    const output = renderReceipt({}, {
      status: "completed_unverified",
      checks: [{ id: "integration", status: "infrastructure_error" }],
      criteria: [],
      warnings: [],
    }).join("\n");
    assert.doesNotMatch(output, /\x1b\[/);
    assert.match(output, /Harness 未验证/);
    assert.match(output, /基础设施异常 · integration/);
  } finally {
    configureAnsiColors(true);
  }
});

function renderReceipt(overrides = {}, harnessReceipt = null, width = 100) {
  return renderComponent(CompletionReceiptCard({
    receipt: {
      receipt_id: "receipt-test",
      outcome: "partial",
      summary: "语义着色验证。",
      changes: [],
      validations: [{ command: "node --test", status: "passed", passed: 1 }],
      unverified: [],
      approvals: [],
      risks: [],
      git_state: {
        available: true,
        branch: "main",
        dirty: true,
        ahead: 0,
        behind: 0,
      },
      next_actions: [],
      duration_ms: 10,
      ...overrides,
    },
    harnessReceipt,
  }), { width });
}

function change(status) {
  return { path: `src/${status}.py`, status, scope: "task" };
}
