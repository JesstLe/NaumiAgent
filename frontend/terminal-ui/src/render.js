import {
  ANSI,
  color,
  padRight,
  wrapAnsiLine,
} from "./ansi.js";
import { createRenderContext, renderComponent } from "./components/core.js";
import { renderFooter } from "./components/footer.js";
import { Message } from "./components/message.js";

export { boxLines } from "./components/core.js";
export { renderFooter } from "./components/footer.js";
export { renderMarkdownExcerpt, renderToolOutput } from "./components/markdown.js";
export { renderMessage } from "./components/message.js";
export { renderToolCard } from "./components/tool-card.js";

export function renderScreen(state, width, height, env = {}) {
  const ctx = createRenderContext({ width, env, state });
  const footer = renderFooter(state, width, env);
  const footerHeight = footer.length;
  const bodyHeight = Math.max(1, height - footerHeight);
  const bodyLines = renderBody(state, width, ctx);
  const start = Math.max(0, bodyLines.length - bodyHeight - state.scrollOffset);
  const visible = bodyLines.slice(start, start + bodyHeight);
  while (visible.length < bodyHeight) visible.push("");
  return [
    ...visible.map((line) => padRight(line, width)),
    ...footer.map((line) => padRight(line, width)),
  ];
}

export function renderBody(state, width, ctx = createRenderContext({ width, env: {}, state })) {
  const lines = [];
  for (const message of state.messages) {
    lines.push(...renderComponent(Message({ message }), ctx));
  }
  if (state.activeToolPrepare) {
    lines.push(color(ANSI.dim, `tool prepare: ${state.activeToolPrepare}`));
  }
  if (state.running) {
    lines.push(color(ANSI.dim, "运行中..."));
  }
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}
