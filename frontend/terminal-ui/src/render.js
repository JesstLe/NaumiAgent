import {
  ANSI,
  color,
  padRight,
  wrapAnsiLine,
} from "./ansi.js";
import { createRenderContext, renderComponent } from "./components/core.js";
import { renderFooter, renderFooterSections } from "./components/footer.js";
import { Message } from "./components/message.js";
import { renderCachedMessage } from "./render-cache.js";

export { boxLines } from "./components/core.js";
export { renderFooter } from "./components/footer.js";
export { renderMarkdownExcerpt, renderToolOutput } from "./components/markdown.js";
export { renderMessage } from "./components/message.js";
export { renderToolCard } from "./components/tool-card.js";

export function renderScreen(state, width, height, env = {}) {
  const ctx = createRenderContext({ width, env, state });
  const footer = clampFooterSections(renderFooterSections(state, width, env), height);
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

function clampFooterSections(sections, height) {
  const maxFooterHeight = Math.max(0, height - 1);
  const footer = sections.flatMap((section) => section.lines);
  if (footer.length <= maxFooterHeight) return footer;
  if (maxFooterHeight <= 0) return [];

  const prompt = sections.find((section) => section.name === "prompt")?.lines ?? [];
  const promptLines = prompt.slice(-maxFooterHeight);
  if (promptLines.length >= maxFooterHeight) return promptLines;

  const remaining = maxFooterHeight - promptLines.length;
  const leading = [];
  for (const section of sections) {
    if (section.name === "prompt" || section.name === "help") continue;
    for (const line of section.lines) {
      if (leading.length >= remaining) break;
      leading.push(line);
    }
    if (leading.length >= remaining) break;
  }
  return [...leading, ...promptLines];
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
    const phase = state.activeRuntimePhase ? ` · ${state.activeRuntimePhase}` : "";
    lines.push(color(ANSI.dim, `运行中...${phase}`));
  }
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}
