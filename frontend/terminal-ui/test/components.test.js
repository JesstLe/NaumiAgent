import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, stripAnsi, visibleWidth } from "../src/ansi.js";
import { boxComponent, line, renderComponent, stack } from "../src/components/core.js";
import { Footer, PermissionFooter, TodoFooter } from "../src/components/footer.js";
import { Message } from "../src/components/message.js";
import { ActivityCard } from "../src/components/activity-card.js";
import { ToolCard } from "../src/components/tool-card.js";
import { createInitialState } from "../src/state.js";

test("component core composes nested stacks and boxes within width", () => {
  const lines = renderComponent(
    boxComponent("group", [
      line("第一行"),
      (ctx) => stack([line("第二行"), line("第三行")], ctx),
    ]),
    { width: 48 },
  );

  assert(lines.some((item) => stripAnsi(item).includes("group")));
  assert(lines.some((item) => stripAnsi(item).includes("第一行")));
  assert(lines.every((item) => visibleWidth(item) <= 48));
});

test("semantic message component delegates tool cards and markdown text", () => {
  const toolLines = renderComponent(
    Message({
      message: {
        kind: "tool",
        name: "file_edit",
        primary: "demo.py",
        status: "success",
        output: "@@\n-old\n+new",
      },
    }),
    { width: 72 },
  );
  const assistantLines = renderComponent(
    Message({ message: { kind: "assistant", content: "```python\nreturn True\n```" } }),
    { width: 72 },
  );

  assert(toolLines.some((item) => item.includes(`${ANSI.green}+new${ANSI.reset}`)));
  assert(assistantLines.some((item) => item.includes(`${ANSI.cyan}return${ANSI.reset}`)));
});

test("footer components can render independently or as a full footer", () => {
  const state = createInitialState();
  state.permission = { requestId: "p1", payload: { tool_name: "bash_run", reason: "需要确认" } };
  state.todo = { total: 2, completed: 1, current: { id: "2", subject: "验证", status: "in_progress" } };

  const permission = renderComponent(PermissionFooter({ permission: state.permission }), { width: 80 });
  const todo = renderComponent(TodoFooter({ todo: state.todo }), { width: 80 });
  const full = renderComponent(Footer({ state, env: { cwd: "/tmp", home: "/Users/lv" } }), { width: 80 });

  assert(stripAnsi(permission.join("\n")).includes("permission: bash_run"));
  assert(stripAnsi(todo.join("\n")).includes("todo: 1/2 完成"));
  assert(stripAnsi(full.join("\n")).includes("Shift+Tab 模式"));
  assert(full.every((item) => visibleWidth(item) <= 80));
});

test("tool card component preserves existing diff folding behavior", () => {
  const card = renderComponent(
    ToolCard({
      tool: {
        name: "file_write",
        primary: "large.md",
        status: "success",
        output: Array.from({ length: 80 }, (_, index) => (index % 2 === 0 ? `+line ${index}` : ` line ${index}`)).join("\n"),
      },
    }),
    { width: 90 },
  );

  assert(card.some((item) => stripAnsi(item).includes("file_write large.md")));
  assert(!stripAnsi(card.join("\n")).includes("+line 78"));
});

test("activity card renders live operation details within width", () => {
  const card = renderComponent(
    ActivityCard({
      activity: {
        status: "running",
        title: "准备 file_write",
        details: ["路径: showcase/index.html", "内容: 88 行", "参数: 4096 字符"],
      },
    }),
    { width: 72 },
  );
  const plain = stripAnsi(card.join("\n"));

  assert(plain.includes("activity"));
  assert(plain.includes("running 准备 file_write"));
  assert(plain.includes("路径: showcase/index.html"));
  assert(card.every((item) => visibleWidth(item) <= 72));
});
