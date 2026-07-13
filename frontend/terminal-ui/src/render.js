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
import { jumpTimelineToLatest } from "./timeline-follow.js";

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
  ctx.bodyHeight = bodyHeight;
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

export function captureViewportAnchor(state, width, height, env = {}) {
  if (state.followTail || Number(state.scrollOffset) <= 0) return null;
  const layout = renderViewportLayout(state, width, height, env);
  if (!layout.segments.length) return null;

  const firstVisibleLine = Math.max(
    0,
    layout.totalBodyLines - layout.bodyHeight - Math.max(0, Number(state.scrollOffset) || 0),
  );
  let segmentStart = 0;
  for (const segment of layout.segments) {
    const segmentEnd = segmentStart + segment.lines.length;
    if (firstVisibleLine < segmentEnd) {
      return {
        messageId: segment.messageId,
        messageIndex: segment.messageIndex,
      };
    }
    segmentStart = segmentEnd;
  }

  const last = layout.segments.at(-1);
  return last
    ? { messageId: last.messageId, messageIndex: last.messageIndex }
    : null;
}

export function restoreViewportAnchor(state, anchor, width, height, env = {}) {
  if (state.followTail || !anchor) {
    jumpTimelineToLatest(state);
    return 0;
  }

  const layout = renderViewportLayout(state, width, height, env);
  const maxOffset = Math.max(0, layout.totalBodyLines - layout.bodyHeight);
  const targetIndex = findAnchorSegmentIndex(layout.segments, anchor);
  if (targetIndex < 0) {
    const fallbackOffset = Math.min(maxOffset, Math.max(0, Number(state.scrollOffset) || 0));
    if (fallbackOffset === 0) {
      jumpTimelineToLatest(state);
    } else {
      state.followTail = false;
      state.scrollOffset = fallbackOffset;
    }
    return state.scrollOffset;
  }

  const anchorSegmentStart = layout.segments
    .slice(0, targetIndex)
    .reduce((total, segment) => total + segment.lines.length, 0);
  const nextOffset = Math.min(
    maxOffset,
    Math.max(0, layout.totalBodyLines - layout.bodyHeight - anchorSegmentStart),
  );

  if (nextOffset === 0) {
    jumpTimelineToLatest(state);
  } else {
    state.followTail = false;
    state.scrollOffset = nextOffset;
  }
  return state.scrollOffset;
}

function renderViewportLayout(state, width, height, env) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const ctx = createRenderContext({ width: safeWidth, env, state });
  const footer = clampFooterSections(renderFooterSections(state, safeWidth, env), safeHeight);
  const bodyHeight = Math.max(1, safeHeight - footer.length);
  ctx.bodyHeight = bodyHeight;
  const segments = state.messages.map((message, messageIndex) => ({
    messageId: message.id === null || message.id === undefined ? "" : String(message.id),
    messageIndex,
    lines: renderCachedMessage(
      state.renderCache,
      message,
      ctx,
      () => renderComponent(Message({ message }), ctx),
    ),
  }));
  const messageLines = segments.reduce((total, segment) => total + segment.lines.length, 0);
  const tailLines = renderBodyTail(state, safeWidth).length;
  return {
    segments,
    bodyHeight,
    totalBodyLines: messageLines + tailLines,
  };
}

function findAnchorSegmentIndex(segments, anchor) {
  const messageId = String(anchor.messageId ?? "");
  if (messageId) {
    const byId = segments.findIndex((segment) => segment.messageId === messageId);
    if (byId >= 0) return byId;
  }
  const messageIndex = Number(anchor.messageIndex);
  if (!Number.isInteger(messageIndex)) return -1;
  return messageIndex >= 0 && messageIndex < segments.length ? messageIndex : -1;
}

function renderBodyTail(state, width) {
  const lines = [];
  if (state.running) {
    const phase = state.activeRuntimePhase ? ` · ${state.activeRuntimePhase}` : "";
    lines.push(color(ANSI.dim, `运行中...${phase}`));
  }
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}
