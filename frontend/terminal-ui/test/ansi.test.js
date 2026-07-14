import test from "node:test";
import assert from "node:assert/strict";
import {
  ANSI,
  color,
  configureAnsiColors,
  sanitizeTerminalText,
  stripAnsi,
  visibleWidth,
  wrapAnsiLine,
} from "../src/ansi.js";

test("color negotiation disables only SGR styling", () => {
  try {
    configureAnsiColors(false);
    assert.equal(color(ANSI.green, "完成"), "完成");
    assert.notEqual(ANSI.clear, "");
    assert.notEqual(ANSI.altOn, "");
  } finally {
    configureAnsiColors(true);
  }

  assert.match(color(ANSI.green, "完成"), /\x1b\[32m/);
});

test("wrapAnsiLine keeps double-width CJK text within terminal width", () => {
  const lines = wrapAnsiLine("permission: bash_run  y=允许 n=拒绝", 34);

  assert(lines.every((line) => visibleWidth(line) <= 34));
  assert.equal(lines.map((line) => line.trim()).join(""), "permission: bash_run  y=允许 n=拒绝");
});

test("wrapAnsiLine keeps ANSI-colored CJK text within terminal width", () => {
  const lines = wrapAnsiLine(color(ANSI.yellow, "todo: 1/4 完成 | #2 继续写入非常长的前端文件并验证"), 24);

  assert(lines.every((line) => visibleWidth(line) <= 24));
});

test("stripAnsi removes keyboard disambiguation control sequences", () => {
  assert.equal(stripAnsi(`${ANSI.keyboardDisambiguateOn}正文${ANSI.keyboardDisambiguateOff}`), "正文");
});

test("sanitizeTerminalText removes untrusted CSI and OSC controls", () => {
  const raw = "safe\x1b[31mred\x1b[0m\x1b]8;;https://evil.test\x07link\x1b]8;;\x07";

  assert.equal(sanitizeTerminalText(raw), "saferedlink");
});

test("wrapAnsiLine resets and resumes active styles across lines", () => {
  const lines = wrapAnsiLine(color(ANSI.green, "新增内容新增内容"), 8);

  assert(lines.length > 1);
  assert(lines.every((line) => line.endsWith(ANSI.reset)));
  assert(lines.slice(1).every((line) => line.startsWith(ANSI.green)));
  assert.equal(lines.map(stripAnsi).join(""), "新增内容新增内容");
  assert(lines.every((line) => visibleWidth(line) <= 8));
});
