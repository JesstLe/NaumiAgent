import { renderMutationsSince } from "./render-cache.js";

const INDEX_BY_STATE = new WeakMap();

export function ensureTimelineRowIndex(state, ctx, renderMessage) {
  if (!state || typeof state !== "object") throw new Error("timeline index 缺少 state");
  if (typeof renderMessage !== "function") throw new Error("timeline index 缺少 renderMessage");
  const messages = Array.isArray(state.messages) ? state.messages : [];
  const width = Math.max(1, Math.trunc(Number(ctx?.width) || 1));
  const generation = Math.max(0, Math.trunc(Number(state.renderCache?.generation) || 0));
  const cacheRevision = Math.max(0, Math.trunc(Number(state.renderCache?.revision) || 0));
  const existing = INDEX_BY_STATE.get(state);
  if (existing
    && existing.messages === messages
    && existing.width === width
    && existing.generation === generation) {
    const mutations = renderMutationsSince(state.renderCache, existing.cacheRevision);
    if (mutations !== null && !mutations.some((mutation) => mutation.scope === "all")) {
      if (appendTimelineMessages(existing, messages, renderMessage)) {
        applyMessageMutations(existing, mutations, renderMessage);
        existing.cacheRevision = cacheRevision;
        existing.reuseCount += 1;
        return existing;
      }
    }
  }

  let cursor = 0;
  const segments = messages.map((message, messageIndex) => {
    const lines = renderMessage(message, messageIndex);
    const height = Array.isArray(lines) ? lines.length : 0;
    const segment = {
      messageId: message?.id === null || message?.id === undefined ? "" : String(message.id),
      message,
      messageIndex,
      start: cursor,
      end: cursor + height,
      height,
    };
    cursor = segment.end;
    return segment;
  });
  const index = {
    messages,
    messageCount: messages.length,
    width,
    generation,
    cacheRevision,
    segments,
    totalLines: cursor,
    buildCount: (existing?.buildCount || 0) + 1,
    reuseCount: 0,
    appendCount: 0,
    partialUpdateCount: 0,
    lastRenderedRange: null,
  };
  index.messagePositions = buildMessagePositions(index.segments);
  INDEX_BY_STATE.set(state, index);
  return index;
}

export function findTimelineSegmentAtRow(index, row) {
  if (!index?.segments?.length || index.totalLines <= 0) return -1;
  const target = Math.min(index.totalLines - 1, Math.max(0, Math.trunc(Number(row) || 0)));
  let low = 0;
  let high = index.segments.length - 1;
  let match = -1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const segment = index.segments[middle];
    if (segment.end <= target) {
      low = middle + 1;
    } else {
      match = middle;
      high = middle - 1;
    }
  }
  return match;
}

export function timelineRowIndexDebug(state) {
  const index = INDEX_BY_STATE.get(state);
  if (!index) return null;
  return {
    width: index.width,
    generation: index.generation,
    messageCount: index.messageCount,
    totalLines: index.totalLines,
    buildCount: index.buildCount,
    reuseCount: index.reuseCount,
    appendCount: index.appendCount,
    partialUpdateCount: index.partialUpdateCount,
    lastRenderedRange: index.lastRenderedRange
      ? { ...index.lastRenderedRange }
      : null,
    storesRenderedLines: false,
  };
}

function appendTimelineMessages(index, messages, renderMessage) {
  if (messages.length < index.messageCount) return false;
  for (let position = index.messageCount; position < messages.length; position += 1) {
    const message = messages[position];
    const lines = renderMessage(message, position);
    const height = Array.isArray(lines) ? lines.length : 0;
    const start = index.totalLines;
    const segment = {
      messageId: message?.id === null || message?.id === undefined ? "" : String(message.id),
      message,
      messageIndex: position,
      start,
      end: start + height,
      height,
    };
    index.segments.push(segment);
    index.messagePositions.set(message, position);
    index.totalLines = segment.end;
    index.appendCount += 1;
  }
  index.messageCount = messages.length;
  return true;
}

function applyMessageMutations(index, mutations, renderMessage) {
  const messages = new Set(
    mutations
      .filter((mutation) => mutation.scope === "message" && mutation.message)
      .map((mutation) => mutation.message),
  );
  for (const message of messages) {
    const position = index.messagePositions.get(message);
    if (!Number.isInteger(position)) continue;
    const segment = index.segments[position];
    const lines = renderMessage(message, position);
    const nextHeight = Array.isArray(lines) ? lines.length : 0;
    const delta = nextHeight - segment.height;
    segment.messageId = message?.id === null || message?.id === undefined ? "" : String(message.id);
    segment.height = nextHeight;
    segment.end = segment.start + nextHeight;
    if (delta !== 0) {
      for (let cursor = position + 1; cursor < index.segments.length; cursor += 1) {
        index.segments[cursor].start += delta;
        index.segments[cursor].end += delta;
      }
      index.totalLines += delta;
    }
    index.partialUpdateCount += 1;
  }
}

function buildMessagePositions(segments) {
  const positions = new WeakMap();
  for (let position = 0; position < segments.length; position += 1) {
    const message = segments[position].message;
    if (message && typeof message === "object") positions.set(message, position);
  }
  return positions;
}
