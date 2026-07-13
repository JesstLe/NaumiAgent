const VISIBLE_UI_MESSAGE_TYPES = new Set([
  "recovery",
  "context_compact",
  "runtime_notification",
  "subagent_event",
  "team_event",
  "hook_trace",
  "error",
  "system_notice",
]);

export function initializeTimelineFollow(state) {
  state.followTail = true;
  state.scrollOffset = 0;
  state.unreadOutputCount = 0;
  state.unreadOutputKeys = {};
}

export function markTimelineOutput(state, record, entryId = "") {
  const key = timelineOutputKey(record, entryId);
  if (!key) return false;

  ensureTimelineFollowState(state);
  if (state.followTail) {
    jumpTimelineToLatest(state);
    return true;
  }

  if (!state.unreadOutputKeys[key]) {
    state.unreadOutputKeys[key] = true;
    state.unreadOutputCount += 1;
  }
  return true;
}

export function detachTimeline(state, scrollOffset) {
  ensureTimelineFollowState(state);
  state.followTail = false;
  state.scrollOffset = Math.max(1, finiteNumber(scrollOffset, 1));
}

export function jumpTimelineToLatest(state) {
  state.followTail = true;
  state.scrollOffset = 0;
  state.unreadOutputCount = 0;
  state.unreadOutputKeys = {};
}

export function scrollTimeline(state, delta) {
  ensureTimelineFollowState(state);
  const nextOffset = Math.max(0, state.scrollOffset + finiteNumber(delta, 0));
  if (nextOffset === 0) {
    jumpTimelineToLatest(state);
    return;
  }
  detachTimeline(state, nextOffset);
}

export function timelineOutputKey(record, entryId = "") {
  const payload = record?.payload ?? {};
  const sequence = normalizeIdentity(record?.seq ?? entryId);

  if (record?.type === "user/message") {
    return sequence ? `user:${sequence}` : null;
  }
  if (record?.type === "error") {
    return sequence ? `error:${sequence}` : null;
  }
  if (record?.type === "permission/request") {
    const requestId = normalizeIdentity(record?.request_id ?? record?.id ?? entryId);
    return requestId ? `permission:${requestId}` : null;
  }
  if (record?.type !== "ui/message") return null;

  if (payload.type === "assistant_stream") {
    const assistantId = normalizeIdentity(entryId);
    return assistantId ? `assistant:${assistantId}` : null;
  }
  if (payload.type === "tool_prepare") {
    const toolId = normalizeIdentity(payload.tool_call_id);
    return toolId ? `tool:${toolId}` : null;
  }
  if (["tool_use", "tool_result"].includes(payload.type)) {
    const toolId = normalizeIdentity(payload.tool_call_id ?? entryId);
    return toolId ? `tool:${toolId}` : null;
  }
  if (payload.type === "thinking") {
    const thinkingId = normalizeIdentity(entryId);
    return thinkingId ? `thinking:${thinkingId}` : null;
  }
  if (payload.type === "permission_bubble") {
    const requestId = normalizeIdentity(payload.request_id ?? entryId);
    return requestId ? `permission:${requestId}` : null;
  }
  if (VISIBLE_UI_MESSAGE_TYPES.has(payload.type)) {
    return sequence ? `${payload.type}:${sequence}` : null;
  }
  return null;
}

function ensureTimelineFollowState(state) {
  if (typeof state.followTail !== "boolean") {
    state.followTail = Number(state.scrollOffset) <= 0;
  }
  if (!Number.isFinite(Number(state.scrollOffset))) {
    state.scrollOffset = 0;
  }
  if (!Number.isFinite(Number(state.unreadOutputCount))) {
    state.unreadOutputCount = 0;
  }
  if (!state.unreadOutputKeys || typeof state.unreadOutputKeys !== "object" || Array.isArray(state.unreadOutputKeys)) {
    state.unreadOutputKeys = {};
  }
}

function finiteNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function normalizeIdentity(value) {
  if (value === null || value === undefined) return "";
  return String(value).trim();
}
