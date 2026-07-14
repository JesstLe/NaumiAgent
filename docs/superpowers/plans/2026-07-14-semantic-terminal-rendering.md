# Semantic Terminal Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 NaumiAgent 新 Terminal UI 和 Textual TUI 建立一致的语义着色，让 Git 变化、Markdown、代码、数学公式和状态信息可以被快速辨认。

**Architecture:** 新 Terminal UI 新增纯函数语义渲染模块，先清理不可信终端控制序列，再按 diff、代码、Markdown 块和行内 token 分层渲染；完成回执直接消费结构化 Git 状态。Textual TUI 保留 Rich Markdown 主链路，增加数学 token 适配和结构化 Rich `Text` 完成回执，避免复制 JavaScript Markdown 解析器。

**Tech Stack:** Node.js ESM、ANSI SGR、Textual 8、Rich `Text`、markdown-it-py、mdit-py-plugins、pytest。

## Global Constraints

- 用户可见文案使用中文，代码注释使用英文，commit message 使用英文。
- 普通正文保持默认色，不做路径、数字或命令的激进猜测。
- Git 新增绿色、删除红色、修改黄色、重命名青色、冲突红色加粗。
- 数学表达式保留 LaTeX 源文本；终端不进行二维排版。
- 不增加第三方依赖，不改变消息协议或持久化格式。
- 所有生产代码必须先有能正确失败的定向测试。
- 只运行相关小模块测试，不运行全量 Python 或 Node 测试。
- 不派子 Agent。

---

### Task 1: ANSI 安全清理与跨行样式保持

**Files:**
- Modify: `frontend/terminal-ui/src/ansi.js`
- Test: `frontend/terminal-ui/test/ansi.test.js`

**Interfaces:**
- Produces: `sanitizeTerminalText(value: unknown): string`
- Preserves: `wrapAnsiLine(line: string, width: number): string[]`
- Consumed by: Task 2 semantic renderer

- [ ] **Step 1: Write the failing tests**

Add tests proving OSC/CSI input is removed and a long green span is reset and resumed across every wrapped line:

```js
import {
  ANSI, color, sanitizeTerminalText, stripAnsi, visibleWidth, wrapAnsiLine,
} from "../src/ansi.js";

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
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd frontend/terminal-ui
node --test test/ansi.test.js
```

Expected: FAIL because `sanitizeTerminalText` is not exported and wrapping does not resume active SGR state.

- [ ] **Step 3: Implement control sanitizing and stateful wrapping**

Implement `sanitizeTerminalText()` to remove OSC sequences, CSI sequences and C0 controls except newline/tab. Refactor `wrapAnsiLine()` to tokenize SGR sequences separately from visible characters, maintain an ordered active SGR list, append `ANSI.reset` at a forced break and prepend the active list to the next line. A reset clears the list; style codes are never counted by `visibleWidth()`.

Required implementation shape:

```js
const OSC_PATTERN = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
const CSI_PATTERN = /\x1b\[[0-?]*[ -/]*[@-~]/g;

export function sanitizeTerminalText(value) {
  return String(value ?? "")
    .replace(OSC_PATTERN, "")
    .replace(CSI_PATTERN, "")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");
}
```

The wrapping implementation must not strip trusted ANSI created by the renderer.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `cd frontend/terminal-ui && node --test test/ansi.test.js`

Expected: all ANSI tests pass with no warnings.

- [ ] **Step 5: Commit**

```bash
git add frontend/terminal-ui/src/ansi.js frontend/terminal-ui/test/ansi.test.js
git commit -m "fix: preserve semantic colors across terminal wraps"
```

---

### Task 2: Unified Markdown, code, diff, and math renderer

**Files:**
- Create: `frontend/terminal-ui/src/components/semantic-text.js`
- Modify: `frontend/terminal-ui/src/components/markdown.js`
- Modify: `frontend/terminal-ui/src/ansi.js`
- Test: `frontend/terminal-ui/test/semantic-rendering.test.js`
- Test: `frontend/terminal-ui/test/render.test.js`

**Interfaces:**
- Consumes: `sanitizeTerminalText`, `ANSI`, `color` from Task 1
- Produces: `renderSemanticInline(text): string`
- Produces: `renderSemanticMarkdownLine(line, context): string`
- Produces: `renderSemanticCodeLine(line, language): string`
- Produces: `renderSemanticDiffLine(line): string`
- Preserves: `renderMarkdownExcerpt()` and `renderToolOutput()` public APIs

- [ ] **Step 1: Write failing semantic rendering tests**

Create `semantic-rendering.test.js` with independent assertions for each semantic class:

```js
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
});

test("renders inline and block LaTeX without losing source", () => {
  const source = "内联 $E=mc^2$\n$$\\int_0^1 x^2 dx$$\n未闭合 $x";
  const rendered = renderMarkdownExcerpt(source, 120);
  assert(rendered[0].includes(ANSI.magenta));
  assert(rendered[1].includes(ANSI.magenta));
  assert.equal(stripAnsi(rendered[2]), "未闭合 $x");
});

test("code tokenizer keeps strings and comments isolated", () => {
  const rendered = renderMarkdownExcerpt(
    "```python\ndef area(radius=2):\n    return \"if 42\"  # comment\n```",
    120,
  );
  assert(rendered.some((line) => line.includes(`${ANSI.blue}area${ANSI.reset}`)));
  assert(rendered.some((line) => line.includes(`${ANSI.green}\"if 42\"${ANSI.reset}`)));
  assert(rendered.some((line) => line.includes(`${ANSI.dim}# comment${ANSI.reset}`)));
});

test("diff renderer distinguishes headers hunks changes and conflicts", () => {
  const rendered = renderToolOutput(
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n<<<<<<< HEAD",
    120,
    { format: "diff" },
  );
  assert(rendered[0].includes(ANSI.bold) && rendered[0].includes(ANSI.cyan));
  assert(rendered[3].includes(ANSI.magenta));
  assert(rendered[4].includes(ANSI.red));
  assert(rendered[5].includes(ANSI.green));
  assert(rendered[6].includes(ANSI.bold) && rendered[6].includes(ANSI.red));
});
```

Also add an assertion that `stripAnsi(rendered.join("\n"))` retains every visible input token and all lines remain within the requested CJK-aware width.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd frontend/terminal-ui
node --test test/semantic-rendering.test.js test/render.test.js
```

Expected: the new test file fails because semantic functions and colors do not exist; the existing render tests remain green.

- [ ] **Step 3: Implement the pure semantic module**

Create `semantic-text.js` with these boundaries:

```js
export function renderSemanticInline(text) { /* left-to-right token scan */ }
export function renderSemanticMarkdownLine(line, context = {}) { /* block class */ }
export function renderSemanticCodeLine(line, language = "") { /* lexical segments */ }
export function renderSemanticDiffLine(line) { /* diff line semantics */ }
```

Implementation requirements:

- sanitize untrusted input before tokenization;
- scan inline tokens left-to-right in priority order: escapes, backticks, links, math, strong, emphasis;
- preserve unmatched delimiters literally;
- style diff conflict markers before ordinary add/delete checks;
- style `diff --git`, file headers and metadata separately;
- tokenize strings and comments before applying one combined keyword/literal/number/function-name regex to plain code segments;
- never run a regex replacement across text that already contains ANSI sequences.

Remove `colorCodeLine()` and `colorDiffLine()` from `ansi.js`; change `markdown.js` to import the semantic functions. Track code fence language and whether the current fence is `diff`. Apply block-level Markdown rules only outside fences, then use existing fold and width logic.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
cd frontend/terminal-ui
node --test test/semantic-rendering.test.js test/render.test.js test/ansi.test.js
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/terminal-ui/src/ansi.js \
  frontend/terminal-ui/src/components/semantic-text.js \
  frontend/terminal-ui/src/components/markdown.js \
  frontend/terminal-ui/test/semantic-rendering.test.js \
  frontend/terminal-ui/test/render.test.js
git commit -m "feat: render terminal content by semantic meaning"
```

---

### Task 3: Structured Git colors in completion receipts

**Files:**
- Modify: `frontend/terminal-ui/src/components/completion-receipt-card.js`
- Create: `frontend/terminal-ui/test/completion-receipt-card.test.js`
- Test: `frontend/terminal-ui/test/components.test.js`

**Interfaces:**
- Produces: `renderChangeSummary(changes): string` containing bounded ANSI spans
- Produces: `renderGitSummary(git): string` containing bounded ANSI spans
- Consumed by: `renderCompletionReceiptCard()`

- [ ] **Step 1: Write failing structured Git color tests**

Create table-driven tests with one change of each status and assert:

```js
const expected = new Map([
  ["added", ANSI.green],
  ["untracked", ANSI.green],
  ["deleted", ANSI.red],
  ["removed_untracked", ANSI.red],
  ["modified", ANSI.yellow],
  ["renamed", ANSI.cyan],
  ["copied", ANSI.cyan],
  ["restored", ANSI.blue],
]);
```

Assert `conflicted` contains both `ANSI.bold` and `ANSI.red`. Add a Git-state test requiring branch cyan, dirty yellow, clean green, ahead green and behind red. Always assert the stripped Chinese summary remains unchanged and card width stays bounded.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd frontend/terminal-ui
node --test test/completion-receipt-card.test.js
```

Expected: FAIL because change and Git summaries are currently plain or wholly dimmed.

- [ ] **Step 3: Implement status-segment rendering**

Replace plain `changeSummary()` and `gitSummary()` output assembly with segment functions. Color only the semantic status/count phrase, not the whole row. Keep Chinese text and status ordering unchanged. Remove the outer `ANSI.dim` around reviewable Git state so inner colors survive.

Required mapping:

```js
const CHANGE_STYLE = {
  modified: ANSI.yellow,
  added: ANSI.green,
  untracked: ANSI.green,
  deleted: ANSI.red,
  removed_untracked: ANSI.red,
  renamed: ANSI.cyan,
  copied: ANSI.cyan,
  conflicted: `${ANSI.bold}${ANSI.red}`,
  restored: ANSI.blue,
};
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
cd frontend/terminal-ui
node --test test/completion-receipt-card.test.js test/components.test.js
```

Expected: all selected receipt/component tests pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/terminal-ui/src/components/completion-receipt-card.js \
  frontend/terminal-ui/test/completion-receipt-card.test.js \
  frontend/terminal-ui/test/components.test.js
git commit -m "feat: color git receipt changes by status"
```

---

### Task 4: Textual semantic math and Rich completion receipt

**Files:**
- Create: `src/naumi_agent/tui/semantic_markdown.py`
- Modify: `src/naumi_agent/tui/completion_receipt.py`
- Modify: `src/naumi_agent/tui/app.py`
- Modify: `src/naumi_agent/tui/renderers/registry.py`
- Test: `tests/unit/test_tui.py`
- Test: `tests/unit/test_tui_renderers.py`

**Interfaces:**
- Produces: `semantic_markdown_parser() -> MarkdownIt`
- Produces: `format_completion_receipt_text(value) -> rich.text.Text`
- Preserves: `format_completion_receipt_markdown()` as a plain/Markdown compatibility formatter

- [ ] **Step 1: Write failing Textual tests**

Add parser token tests proving `$x^2$` becomes visible inline code-style content and `$$...$$` becomes a `latex` fence while unmatched `$x` remains ordinary text. Add receipt tests inspecting Rich spans:

```python
text = format_completion_receipt_text(_receipt_with_all_change_states())
assert "新增 1 个文件" in text.plain
assert any(span.style == "green" for span in text.spans)
assert any(span.style == "red" for span in text.spans)
assert any(str(span.style) == "bold red" for span in text.spans)
```

Update renderer tests to require a `Static`/Rich `Text` completion receipt rather than a Markdown string while preserving all Chinese evidence.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest \
  tests/unit/test_tui.py tests/unit/test_tui_renderers.py \
  -k 'semantic_markdown or completion_receipt' -q
```

Expected: FAIL because parser and Rich receipt formatter do not exist.

- [ ] **Step 3: Implement semantic Markdown parser**

Use the already installed `mdit_py_plugins.dollarmath.dollarmath_plugin`. Add a core rule after inline parsing that rewrites `math_inline` children to `code_inline` with preserved `$...$` delimiters and rewrites `math_block` tokens to fenced `latex` code. Unmatched delimiters remain text because the plugin does not emit a math token.

```python
def semantic_markdown_parser() -> MarkdownIt:
    parser = MarkdownIt("gfm-like").use(dollarmath_plugin)
    parser.core.ruler.after("inline", "naumi_math_display", _rewrite_math_tokens)
    return parser
```

Pass this parser factory to assistant/tool Markdown widgets in `app.py` and `tui/renderers/registry.py`. Do not change protocol messages.

- [ ] **Step 4: Implement Rich completion receipt**

Build a `Text` line-by-line with `.append(text, style=...)`. Reuse the existing status normalization and ordering; style added green, deleted red, modified yellow, renamed/copy cyan, conflict bold red and restored blue. Style branch cyan, dirty yellow, clean green, ahead green and behind red. Mount it through `Static(text, classes="agent-msg")` in both completion-receipt call sites.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the exact pytest command from Step 2.

Expected: selected tests pass with no warnings.

- [ ] **Step 6: Commit**

```bash
git add src/naumi_agent/tui/semantic_markdown.py \
  src/naumi_agent/tui/completion_receipt.py \
  src/naumi_agent/tui/app.py \
  src/naumi_agent/tui/renderers/registry.py \
  tests/unit/test_tui.py tests/unit/test_tui_renderers.py
git commit -m "feat: sync semantic colors to textual tui"
```

---

### Task 5: Focused end-to-end verification and documentation closeout

**Files:**
- Modify if needed: `docs/superpowers/specs/2026-07-14-semantic-terminal-rendering-design.md`
- Test only: all files touched in Tasks 1–4

**Interfaces:**
- Verifies the public rendering path from assistant/tool/receipt data to bounded terminal lines.

- [ ] **Step 1: Run Node syntax and focused tests**

```bash
cd frontend/terminal-ui
npm run check
node --test \
  test/ansi.test.js \
  test/semantic-rendering.test.js \
  test/completion-receipt-card.test.js \
  test/render.test.js \
  test/components.test.js
```

Expected: syntax check and selected tests pass.

- [ ] **Step 2: Run focused Python tests**

```bash
PYTHONPATH=src NAUMI_MODELS__API_KEY=unit-test-placeholder \
  /Users/lv/Workspace/NaumiAgent/.venv/bin/python -m pytest \
  tests/unit/test_tui.py tests/unit/test_tui_renderers.py \
  -k 'semantic_markdown or completion_receipt or tool_result' -q
```

Expected: selected TUI tests pass.

- [ ] **Step 3: Run static checks only on touched files**

```bash
/Users/lv/Workspace/NaumiAgent/.venv/bin/ruff check \
  src/naumi_agent/tui/semantic_markdown.py \
  src/naumi_agent/tui/completion_receipt.py \
  src/naumi_agent/tui/app.py \
  src/naumi_agent/tui/renderers/registry.py \
  tests/unit/test_tui.py tests/unit/test_tui_renderers.py
git diff --check main...HEAD
```

Expected: no Ruff or whitespace errors.

- [ ] **Step 4: Run a real renderer smoke scenario**

Render one assistant message containing a heading, list, inline code, link, formula, fenced Python and fenced diff plus one completion receipt containing every Git status. Assert:

- every line is within 72 columns;
- stripped text contains all source facts;
- output contains green, red, yellow, cyan, blue and magenta SGR codes;
- Textual `Text` output contains corresponding status spans.

- [ ] **Step 5: Self-review against the spec**

Check explicitly:

- ordinary prose remains default color;
- unmatched Markdown/math delimiters remain literal;
- code strings/comments do not receive keyword colors internally;
- `+++`/`---` file headers are not mistaken for add/delete content;
- replay and live paths share the same component renderer;
- no untrusted OSC/CSI survives semantic input sanitization;
- no full test suite was run.

- [ ] **Step 6: Commit any verification-only fixes separately**

If no fixes are required, do not create an empty commit. If fixes are required, repeat the relevant focused red-green cycle and commit with an accurate English message.

- [ ] **Step 7: Merge and push**

After verification, fast-forward `codex/semantic-terminal-rendering` into `main`, rerun the smallest semantic-rendering and receipt smoke tests on merged `main`, then push `origin main`. Preserve the untracked `.superpowers/` directory.
