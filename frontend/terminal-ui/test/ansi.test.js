import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ANSI,
  color,
  configureAnsiColors,
  sanitizeTerminalText,
  stripAnsi,
  truncateAnsi,
  truncatePlain,
  visibleWidth,
  wrapAnsiLine,
} from "../src/ansi.js";

const WIDTH_CONTRACT = JSON.parse(
  readFileSync(new URL("../terminal-width-contract.json", import.meta.url), "utf8"),
);

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

test("shared width contract measures CJK, combining and emoji graphemes", () => {
  for (const fixture of WIDTH_CONTRACT.measurement_cases) {
    assert.equal(visibleWidth(fixture.text), fixture.width, fixture.id);
  }
});

test("shared width contract truncates only at grapheme boundaries", () => {
  for (const fixture of WIDTH_CONTRACT.truncation_cases) {
    const actual = truncatePlain(fixture.text, fixture.width);
    assert.equal(actual, fixture.expected, fixture.id);
    assert(visibleWidth(actual) <= fixture.width, fixture.id);
  }
});

test("shared width contract wraps graphemes and replaces impossible wide cells", () => {
  for (const fixture of WIDTH_CONTRACT.wrap_cases) {
    const actual = wrapAnsiLine(fixture.text, fixture.width);
    assert.deepEqual(actual, fixture.expected, fixture.id);
    assert(actual.every((line) => visibleWidth(line) <= fixture.width), fixture.id);
  }
});

test("ANSI truncation preserves styles without splitting ZWJ emoji", () => {
  const rendered = truncateAnsi(color(ANSI.green, "A👩‍💻B"), 3);

  assert.equal(stripAnsi(rendered), "A…");
  assert(rendered.startsWith(ANSI.green));
  assert(rendered.endsWith(ANSI.reset));
  assert.equal(visibleWidth(rendered), 2);
});

test("plain truncation strips accidental terminal controls", () => {
  assert.equal(truncatePlain(`${ANSI.red}红色正文${ANSI.reset}`, 5), "红色…");
});

test("ANSI wrapping keeps complete ZWJ emoji and closes every styled line", () => {
  const rendered = wrapAnsiLine(color(ANSI.cyan, "A👩‍💻B"), 3);

  assert.deepEqual(rendered.map(stripAnsi), ["A👩‍💻", "B"]);
  assert(rendered.every((line) => line.startsWith(ANSI.cyan)));
  assert(rendered.every((line) => line.endsWith(ANSI.reset)));
  assert(rendered.every((line) => visibleWidth(line) <= 3));
});
