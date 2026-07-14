import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderMarkdownExcerpt, renderToolOutput } from "../src/render.js";

test("renders Markdown structure with stable semantic colors", () => {
  const rendered = renderMarkdownExcerpt([
    "# 标题",
    "- 普通 **重点** 和 `命令`",
    "> 引用",
    "[文档](https://example.test)",
  ].join("\n"), 120);

  assert(rendered[0].includes(ANSI.bold) && rendered[0].includes(ANSI.cyan));
  assert(rendered[1].includes(`${ANSI.yellow}命令${ANSI.reset}`));
  assert(rendered[2].includes(ANSI.dim));
  assert(rendered[3].includes(ANSI.blue));
  assert.deepEqual(rendered.map(stripAnsi), [
    "# 标题",
    "- 普通 **重点** 和 `命令`",
    "> 引用",
    "[文档](https://example.test)",
  ]);
});

test("renders inline and block LaTeX without losing source", () => {
  const source = "内联 $E=mc^2$\n$$\\int_0^1 x^2 dx$$\n\\(a+b\\)\n\\[c=d\\]\n未闭合 $x";
  const rendered = renderMarkdownExcerpt(source, 120);

  assert(rendered[0].includes(ANSI.magenta));
  assert(rendered[1].includes(ANSI.magenta));
  assert(rendered[2].includes(ANSI.magenta));
  assert(rendered[3].includes(ANSI.magenta));
  assert.equal(stripAnsi(rendered[4]), "未闭合 $x");
  assert.equal(rendered.map(stripAnsi).join("\n"), source);
});

test("code tokenizer keeps strings and comments isolated", () => {
  const rendered = renderMarkdownExcerpt([
    "```python",
    "def area(radius=2):",
    "    return \"if 42\"  # comment",
    "```",
  ].join("\n"), 120);

  assert(rendered.some((line) => line.includes(`${ANSI.blue}area${ANSI.reset}`)));
  assert(rendered.some((line) => line.includes(`${ANSI.green}\"if 42\"${ANSI.reset}`)));
  assert(rendered.some((line) => line.includes(`${ANSI.dim}# comment${ANSI.reset}`)));
  const stringLine = rendered.find((line) => stripAnsi(line).includes("return"));
  assert(!stringLine.includes(`${ANSI.cyan}if${ANSI.reset}`));
  assert(!stringLine.includes(`${ANSI.magenta}42${ANSI.reset}`));
});

test("diff renderer distinguishes headers hunks changes and conflicts", () => {
  const rendered = renderToolOutput([
    "diff --git a/a.py b/a.py",
    "--- a/a.py",
    "+++ b/a.py",
    "@@ -1 +1 @@",
    "-old",
    "+new",
    "<<<<<<< HEAD",
  ].join("\n"), 120, { format: "diff" });

  assert(rendered[0].includes(ANSI.bold) && rendered[0].includes(ANSI.cyan));
  assert(rendered[1].includes(ANSI.cyan));
  assert(rendered[2].includes(ANSI.cyan));
  assert(rendered[3].includes(ANSI.magenta));
  assert(rendered[4].includes(ANSI.red));
  assert(rendered[5].includes(ANSI.green));
  assert(rendered[6].includes(ANSI.bold) && rendered[6].includes(ANSI.red));
});

test("semantic rendering sanitizes controls and keeps CJK lines bounded", () => {
  const rendered = renderMarkdownExcerpt(
    "## 很长的中文标题用于验证折行不会串色\x1b]8;;https://evil.test\x07隐藏控制\x1b]8;;\x07",
    18,
  );

  assert(rendered.length > 1);
  assert(rendered.every((line) => visibleWidth(line) <= 18));
  assert(rendered.every((line) => line.endsWith(ANSI.reset)));
  assert.equal(rendered.map(stripAnsi).join(""), "## 很长的中文标题用于验证折行不会串色隐藏控制");
});
