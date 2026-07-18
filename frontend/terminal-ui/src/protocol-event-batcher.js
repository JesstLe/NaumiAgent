const DEFAULT_FLUSH_DELAY_MS = 8;
const DEFAULT_MAX_CONTENT_CHARS = 65_536;
const DEFAULT_MAX_EVENTS = 2_048;

export function createProtocolEventBatcher({
  onRecord,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
  flushDelayMs = DEFAULT_FLUSH_DELAY_MS,
  maxContentChars = DEFAULT_MAX_CONTENT_CHARS,
  maxEvents = DEFAULT_MAX_EVENTS,
} = {}) {
  if (typeof onRecord !== "function") {
    throw new TypeError("协议事件合并器需要 onRecord 回调");
  }
  if (typeof setTimer !== "function" || typeof clearTimer !== "function") {
    throw new TypeError("协议事件合并器需要有效的计时器函数");
  }
  const delay = boundedInteger(flushDelayMs, DEFAULT_FLUSH_DELAY_MS, 0, 1_000);
  const maximum = boundedInteger(
    maxContentChars,
    DEFAULT_MAX_CONTENT_CHARS,
    1,
    1_000_000,
  );
  const maximumEvents = boundedInteger(maxEvents, DEFAULT_MAX_EVENTS, 1, 100_000);
  let pending = null;
  let timer = null;
  let pendingCount = 0;

  const clearPendingTimer = () => {
    if (timer === null) return;
    clearTimer(timer);
    timer = null;
  };

  const flush = () => {
    clearPendingTimer();
    if (pending === null) return 0;
    const record = pending;
    const count = pendingCount;
    pending = null;
    pendingCount = 0;
    onRecord(record);
    return count;
  };

  const arm = () => {
    if (timer !== null) return;
    timer = setTimer(() => {
      timer = null;
      flush();
    }, delay);
    timer?.unref?.();
  };

  return {
    get pending() {
      return pending !== null;
    },
    get pendingCount() {
      return pendingCount;
    },
    push(record) {
      if (!isCoalescibleStreamDelta(record)) {
        flush();
        onRecord(record);
        return false;
      }
      const content = String(record.payload.content ?? "");
      if (content.length > maximum) {
        flush();
        onRecord(record);
        return false;
      }
      if (
        pending !== null
        && (pendingCount >= maximumEvents
          || !sameStreamDelta(pending, record)
          || String(pending.payload.content ?? "").length + content.length > maximum)
      ) {
        flush();
      }
      if (pending === null) {
        pending = cloneRecord(record, content);
        pendingCount = 1;
      } else {
        pending = mergeRecord(pending, record, content);
        pendingCount += 1;
      }
      arm();
      return true;
    },
    flush,
    cancel() {
      const count = pendingCount;
      clearPendingTimer();
      pending = null;
      pendingCount = 0;
      return count;
    },
  };
}

export function isCoalescibleStreamDelta(record) {
  if (record?.type !== "ui/message") return false;
  const payload = record.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
  return (
    (payload.type === "assistant_stream" && payload.phase === "token")
    || (payload.type === "thinking" && payload.phase === "delta")
  ) && typeof payload.content === "string";
}

function sameStreamDelta(left, right) {
  return left.type === right.type
    && left.payload.type === right.payload.type
    && left.payload.phase === right.payload.phase
    && String(left.request_id ?? "") === String(right.request_id ?? "");
}

function cloneRecord(record, content) {
  return {
    ...record,
    payload: { ...record.payload, content },
  };
}

function mergeRecord(pending, latest, content) {
  return {
    ...pending,
    ...(latest.id == null ? {} : { id: latest.id }),
    ...(latest.seq == null ? {} : { seq: latest.seq }),
    ...(latest.request_id == null ? {} : { request_id: latest.request_id }),
    payload: {
      ...pending.payload,
      ...latest.payload,
      content: String(pending.payload.content ?? "") + content,
    },
  };
}

function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.trunc(parsed)));
}

export const PROTOCOL_EVENT_BATCH_DEFAULTS = Object.freeze({
  flushDelayMs: DEFAULT_FLUSH_DELAY_MS,
  maxContentChars: DEFAULT_MAX_CONTENT_CHARS,
  maxEvents: DEFAULT_MAX_EVENTS,
});
