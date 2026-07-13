const DEFAULT_MAX_ENTRIES = 600;

export function createRenderCache({ maxEntries = DEFAULT_MAX_ENTRIES } = {}) {
  return {
    maxEntries,
    entries: new Map(),
    hits: 0,
    misses: 0,
  };
}

export function renderCachedMessage(cache, message, ctx, render) {
  if (!cache) {
    return render();
  }
  const key = messageRenderKey(message, ctx);
  const cached = cache.entries.get(key);
  if (cached) {
    cache.hits += 1;
    cache.entries.delete(key);
    cache.entries.set(key, cached);
    return cached.slice();
  }
  cache.misses += 1;
  const rendered = render();
  cache.entries.set(key, rendered.slice());
  evictOldEntries(cache);
  return rendered;
}

export function clearRenderCache(cache) {
  if (!cache) return;
  cache.entries.clear();
  cache.hits = 0;
  cache.misses = 0;
}

export function messageRenderKey(message, ctx) {
  return JSON.stringify({
    width: ctx.width,
    kind: message.kind,
    id: message.id ?? "",
    content: message.content ?? "",
    requestId: message.requestId ?? "",
    deliveryStatus: message.deliveryStatus ?? "",
    attempt: message.attempt ?? 0,
    errorCode: message.errorCode ?? "",
    errorMessage: message.errorMessage ?? "",
    done: message.done ?? false,
    callId: message.callId ?? "",
    name: message.name ?? "",
    primary: message.primary ?? "",
    status: message.status ?? "",
    toolCallId: message.toolCallId ?? "",
    toolName: message.toolName ?? "",
    phase: message.phase ?? "",
    metrics: message.metrics ?? null,
    details: message.details ?? [],
    prepareTitle: message.prepareTitle ?? "",
    preparePhase: message.preparePhase ?? "",
    prepareMetrics: message.prepareMetrics ?? null,
    prepareDetails: message.prepareDetails ?? [],
    durationMs: message.durationMs ?? 0,
    output: message.output ?? "",
    outputLength: message.outputLength ?? 0,
    level: message.level ?? "",
    title: message.title ?? "",
    message: message.message ?? null,
    folds: ctx.state?.folds ?? {},
    taskPanel: {
      selectedId: ctx.state?.taskPanel?.selectedId ?? "",
      focused: ctx.state?.taskPanel?.focused ?? false,
      expandedIds: ctx.state?.taskPanel?.expandedIds ?? {},
      collapsedTimelineSources: ctx.state?.taskPanel?.collapsedTimelineSources ?? {},
    },
  });
}

function evictOldEntries(cache) {
  while (cache.entries.size > cache.maxEntries) {
    const first = cache.entries.keys().next().value;
    if (first === undefined) return;
    cache.entries.delete(first);
  }
}
