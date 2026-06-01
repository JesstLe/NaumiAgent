import {
  ANSI,
  color,
  padRight,
  wrapAnsiLine,
} from "./ansi.js";
import { createRenderContext, renderComponent } from "./components/core.js";
import { renderFooter } from "./components/footer.js";
import { Message } from "./components/message.js";
import { renderCachedMessage } from "./render-cache.js";

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
  const bodyLines = renderBodyWindow(state, width, bodyHeight, state.scrollOffset, ctx);
  const target = bodyHeight + state.scrollOffset;
  const start = Math.max(0, bodyLines.length - target);
  const visible = bodyLines.slice(start, start + bodyHeight);
  while (visible.length < bodyHeight) visible.push("");
  return [
    ...visible.map((line) => padRight(line, width)),
    ...footer.map((line) => padRight(line, width)),
  ];
}

export function renderBodyWindow(state, width, bodyHeight, scrollOffset, ctx = createRenderContext({ width, env: {}, state })) {
  const targetLines = Math.max(1, bodyHeight + Math.max(0, scrollOffset));
  const tail = renderBodyTail(state, width);
  const segments = [];
  let collected = tail.length;

  for (let index = state.messages.length - 1; index >= 0 && collected < targetLines; index -= 1) {
    const messageLines = renderCachedMessage(
      state.renderCache,
      state.messages[index],
      ctx,
      () => renderComponent(Message({ message: state.messages[index] }), ctx),
    );
    segments.unshift(messageLines);
    collected += messageLines.length;
  }

  return [...segments.flat(), ...tail];
}

export function renderBody(state, width, ctx = createRenderContext({ width, env: {}, state })) {
  const lines = [];
  for (const message of state.messages) {
    lines.push(...renderCachedMessage(
      state.renderCache,
      message,
      ctx,
      () => renderComponent(Message({ message }), ctx),
    ));
  }
  lines.push(...renderBodyTail(state, width));
  return lines;
}

function renderBodyTail(state, width) {
  const lines = [];
  if (state.running) {
    lines.push(color(ANSI.dim, "运行中..."));
  }
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}
