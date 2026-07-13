import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, color, stripAnsi, visibleWidth, wrapAnsiLine } from "../src/ansi.js";

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
