import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, color, stripAnsi, visibleWidth } from "../src/ansi.js";
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

function renderReceipt(overrides = {}) {
  return renderComponent(CompletionReceiptCard({
    receipt: {
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
  }), { width: 100 });
}

function change(status) {
  return { path: `src/${status}.py`, status, scope: "task" };
}
