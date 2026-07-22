const INDEX_BY_STATE = new WeakMap();

export function ensureTimelineRowIndex(state, ctx, renderMessage) {
  if (!state || typeof state !== "object") throw new Error("timeline index 缺少 state");
  if (typeof renderMessage !== "function") throw new Error("timeline index 缺少 renderMessage");
  const messages = Array.isArray(state.messages) ? state.messages : [];
  const width = Math.max(1, Math.trunc(Number(ctx?.width) || 1));
  const generation = Math.max(0, Math.trunc(Number(state.renderCache?.generation) || 0));
  const existing = INDEX_BY_STATE.get(state);
  if (existing
    && existing.messages === messages
    && existing.messageCount === messages.length
    && existing.width === width
    && existing.generation === generation) {
    existing.reuseCount += 1;
    return existing;
  }

  let cursor = 0;
  const segments = messages.map((message, messageIndex) => {
    const lines = renderMessage(message, messageIndex);
    const height = Array.isArray(lines) ? lines.length : 0;
    const segment = {
      messageId: message?.id === null || message?.id === undefined ? "" : String(message.id),
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
    segments,
    totalLines: cursor,
    buildCount: (existing?.buildCount || 0) + 1,
    reuseCount: 0,
    lastRenderedRange: null,
  };
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
    lastRenderedRange: index.lastRenderedRange
      ? { ...index.lastRenderedRange }
      : null,
    storesRenderedLines: false,
  };
}
