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
    done: message.done ?? false,
    callId: message.callId ?? "",
    name: message.name ?? "",
    primary: message.primary ?? "",
    status: message.status ?? "",
    details: message.details ?? [],
    prepareTitle: message.prepareTitle ?? "",
    prepareDetails: message.prepareDetails ?? [],
    durationMs: message.durationMs ?? 0,
    output: message.output ?? "",
    outputLength: message.outputLength ?? 0,
    level: message.level ?? "",
    title: message.title ?? "",
    message: message.message ?? null,
    folds: ctx.state?.folds ?? {},
  });
}

function evictOldEntries(cache) {
  while (cache.entries.size > cache.maxEntries) {
    const first = cache.entries.keys().next().value;
    if (first === undefined) return;
    cache.entries.delete(first);
  }
}
