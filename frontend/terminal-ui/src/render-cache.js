const DEFAULT_MAX_ENTRIES = 600;
const MAX_RENDER_MUTATIONS = 1_024;

export function createRenderCache({ maxEntries = DEFAULT_MAX_ENTRIES } = {}) {
  return {
    maxEntries,
    entries: new Map(),
    hits: 0,
    misses: 0,
    generation: 0,
    revision: 0,
    mutations: [],
    messageRevisions: new WeakMap(),
  };
}

export function renderCachedMessage(cache, message, ctx, render) {
  if (!cache) {
    return render();
  }
  const semanticRevision = Math.max(0, Number(cache.messageRevisions?.get(message)) || 0);
  const key = `${messageRenderKey(message, ctx)}|revision:${semanticRevision}`;
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
  cache.generation = Math.max(0, Number(cache.generation) || 0) + 1;
  recordRenderMutation(cache, { scope: "all" });
}

export function markMessageRenderDirty(cache, message) {
  if (!cache || !message || typeof message !== "object") return;
  if (!(cache.messageRevisions instanceof WeakMap)) cache.messageRevisions = new WeakMap();
  cache.messageRevisions.set(
    message,
    Math.max(0, Number(cache.messageRevisions.get(message)) || 0) + 1,
  );
  recordRenderMutation(cache, { scope: "message", message });
}

export function renderMutationsSince(cache, revision) {
  const current = Math.max(0, Number(cache?.revision) || 0);
  const previous = Math.max(0, Number(revision) || 0);
  if (previous === current) return [];
  const mutations = Array.isArray(cache?.mutations) ? cache.mutations : [];
  const first = mutations[0]?.revision;
  if (!mutations.length || !Number.isInteger(first) || previous < first - 1) return null;
  const offset = Math.max(0, previous - first + 1);
  return mutations.slice(offset);
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
    phaseLabel: message.phaseLabel ?? "",
    turn: message.turn ?? 0,
    model: message.model ?? "",
    toolCalls: message.toolCalls ?? null,
    permissionCount: message.permissionCount ?? 0,
    perfPhases: message.perfPhases ?? [],
    metrics: message.metrics ?? null,
    details: message.details ?? [],
    prepareTitle: message.prepareTitle ?? "",
    preparePhase: message.preparePhase ?? "",
    prepareMetrics: message.prepareMetrics ?? null,
    prepareDetails: message.prepareDetails ?? [],
    durationMs: message.durationMs ?? 0,
    errorCode: message.errorCode ?? "",
    retryable: message.retryable === true,
    output: message.output ?? "",
    outputLength: message.outputLength ?? 0,
    outputBytes: message.outputBytes ?? 0,
    outputArtifactId: message.outputArtifactId ?? "",
    outputPageCount: message.outputPageCount ?? 0,
    outputPageChars: message.outputPageChars ?? 0,
    outputSha256: message.outputSha256 ?? "",
    level: message.level ?? "",
    title: message.title ?? "",
    message: message.message ?? null,
    folds: ctx.state?.folds ?? {},
    taskPanel: {
      selectedId: ctx.state?.taskPanel?.selectedId ?? "",
      focused: ctx.state?.taskPanel?.focused ?? false,
      expandedIds: ctx.state?.taskPanel?.expandedIds ?? {},
      collapsedTimelineSources: ctx.state?.taskPanel?.collapsedTimelineSources ?? {},
      searchQuery: ctx.state?.taskPanel?.searchQuery ?? "",
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

function recordRenderMutation(cache, mutation) {
  cache.revision = Math.max(0, Number(cache.revision) || 0) + 1;
  const record = { ...mutation, revision: cache.revision };
  if (!Array.isArray(cache.mutations)) cache.mutations = [];
  cache.mutations.push(record);
  if (cache.mutations.length > MAX_RENDER_MUTATIONS) {
    cache.mutations = [{ scope: "all", revision: cache.revision }];
  }
}
