import { commandCategoryLabel, commandRiskLabel, commandTemplate } from "./command-metadata.js";
import { setInputText } from "./input-buffer.js";

const QUERY_LIMIT = 200;
const RESULT_LIMIT = 200;
const TASK_RESULT_LIMIT = 50;
const SESSION_RESULT_LIMIT = 100;
const FILE_RESULT_LIMIT = 200;
const AGENT_RESULT_LIMIT = 50;
const PAGE_RESULT_LIMIT = 32;
const GRAPHEME_SEGMENTER = new Intl.Segmenter("und", { granularity: "grapheme" });
const TASK_STATUS_ORDER = Object.freeze({
  running: 0,
  blocked: 1,
  pending: 2,
  failed: 3,
  cancelled: 4,
  completed: 5,
});
const TASK_SOURCE_ORDER = Object.freeze({ todo: 0, subagent: 1, background: 2, browser: 3 });
const AGENT_STATE_ORDER = Object.freeze({
  running: 0,
  ready: 1,
  spawned: 2,
  idle: 3,
  uninitialized: 4,
  destroyed: 5,
});
const AGENT_KIND_ORDER = Object.freeze({ dynamic: 0, preset: 1 });

export function openCommandQuickOpen(state) {
  const quickOpen = ensureCommandQuickOpenState(state);
  quickOpen.open = true;
  quickOpen.query = "";
  quickOpen.selectedIndex = 0;
  quickOpen.provider = "commands";
  if (!quickOpen.taskLoading) {
    quickOpen.taskItems = [];
    quickOpen.taskWarnings = [];
    quickOpen.taskLoaded = false;
    quickOpen.taskError = "";
    quickOpen.taskRequestId = "";
  }
  if (!quickOpen.sessionLoading) {
    quickOpen.sessionItems = [];
    quickOpen.sessionWarnings = [];
    quickOpen.sessionLoaded = false;
    quickOpen.sessionError = "";
    quickOpen.sessionRequestId = "";
  }
  if (!quickOpen.fileLoading) {
    quickOpen.fileItems = [];
    quickOpen.fileLoaded = false;
    quickOpen.fileError = "";
    quickOpen.fileRequestId = "";
    quickOpen.fileMeta = null;
  }
  if (!quickOpen.agentLoading) {
    quickOpen.agentItems = [];
    quickOpen.agentWarnings = [];
    quickOpen.agentLoaded = false;
    quickOpen.agentError = "";
    quickOpen.agentRequestId = "";
    quickOpen.agentRevision = 0;
  }
  quickOpen.draftText = String(state.input ?? "");
  quickOpen.draftCursor = state.inputCursor;
  return true;
}

export function closeCommandQuickOpen(state) {
  const quickOpen = ensureCommandQuickOpenState(state);
  if (!quickOpen.open) return false;
  if (quickOpen.agentRequestId) {
    quickOpen.agentDiscardRequestIds = [
      ...quickOpen.agentDiscardRequestIds.filter(
        (requestId) => requestId !== quickOpen.agentRequestId,
      ),
      quickOpen.agentRequestId,
    ].slice(-20);
    quickOpen.agentLoading = false;
    quickOpen.agentRequestId = "";
  }
  quickOpen.open = false;
  quickOpen.query = "";
  quickOpen.selectedIndex = 0;
  quickOpen.fileLoading = false;
  quickOpen.fileRequestId = "";
  return true;
}

export function appendCommandQuickOpenQuery(state, text) {
  const quickOpen = ensureCommandQuickOpenState(state);
  const graphemes = segmentGraphemes(`${quickOpen.query}${String(text ?? "")}`);
  quickOpen.query = graphemes.slice(0, QUERY_LIMIT).join("");
  quickOpen.selectedIndex = 0;
  return true;
}

export function backspaceCommandQuickOpenQuery(state) {
  const quickOpen = ensureCommandQuickOpenState(state);
  const graphemes = segmentGraphemes(quickOpen.query);
  if (!graphemes.length) return false;
  graphemes.pop();
  quickOpen.query = graphemes.join("");
  quickOpen.selectedIndex = 0;
  return true;
}

export function getCommandQuickOpenItems(state) {
  const quickOpen = ensureCommandQuickOpenState(state);
  const ranked = quickOpen.provider === "tasks"
    ? searchTaskEntries(quickOpen.taskItems, quickOpen.query, TASK_RESULT_LIMIT)
    : quickOpen.provider === "sessions"
      ? searchSessionEntries(quickOpen.sessionItems, quickOpen.query, SESSION_RESULT_LIMIT)
      : quickOpen.provider === "files"
        ? quickOpen.fileItems.slice(0, FILE_RESULT_LIMIT)
        : quickOpen.provider === "agents"
          ? searchAgentEntries(quickOpen.agentItems, quickOpen.query, AGENT_RESULT_LIMIT)
          : quickOpen.provider === "pages"
            ? searchPageEntries(state.navigationPages, quickOpen.query, PAGE_RESULT_LIMIT)
    : searchCommandEntries(
      state.slashCommands,
      quickOpen.query,
      RESULT_LIMIT,
      quickOpen.recentCommands,
    );
  const maximum = Math.max(0, ranked.length - 1);
  quickOpen.selectedIndex = Math.min(maximum, Math.max(0, Number(quickOpen.selectedIndex) || 0));
  const recent = new Set(quickOpen.recentCommands);
  return ranked.map((entry, index) => ({
    ...entry,
    provider: quickOpen.provider,
    selected: index === quickOpen.selectedIndex,
    recent: quickOpen.provider === "commands" && recent.has(entry.command),
  }));
}

export function switchCommandQuickOpenProvider(state, requests = null) {
  const quickOpen = ensureCommandQuickOpenState(state);
  quickOpen.provider = ({
    commands: "tasks",
    tasks: "sessions",
    sessions: "files",
    files: "agents",
    agents: "pages",
    pages: "commands",
  })[quickOpen.provider];
  quickOpen.query = "";
  quickOpen.selectedIndex = 0;
  if (
    quickOpen.provider === "tasks"
    && !quickOpen.taskLoaded
    && !quickOpen.taskLoading
    && typeof (typeof requests === "function" ? requests : requests?.tasks) === "function"
  ) {
    const requestTaskSnapshot = typeof requests === "function" ? requests : requests.tasks;
    quickOpen.taskLoading = true;
    quickOpen.taskError = "";
    quickOpen.taskRequestId = String(requestTaskSnapshot() || "");
    if (!quickOpen.taskRequestId) {
      quickOpen.taskLoading = false;
      quickOpen.taskError = "任务快照请求未发送。";
    }
  }
  if (quickOpen.provider === "sessions" && !quickOpen.sessionLoaded && !quickOpen.sessionLoading
    && typeof requests?.sessions === "function") {
    quickOpen.sessionLoading = true;
    quickOpen.sessionError = "";
    quickOpen.sessionRequestId = String(requests.sessions() || "");
    if (!quickOpen.sessionRequestId) {
      quickOpen.sessionLoading = false;
      quickOpen.sessionError = "会话快照请求未发送。";
    }
  }
  if (quickOpen.provider === "files" && typeof requests?.files === "function") {
    requestCommandQuickOpenFiles(state, requests.files, { refresh: !quickOpen.fileLoaded });
  }
  if (
    quickOpen.provider === "agents"
    && !quickOpen.agentLoaded
    && !quickOpen.agentLoading
    && typeof requests?.agents === "function"
  ) {
    quickOpen.agentLoading = true;
    quickOpen.agentError = "";
    quickOpen.agentRequestId = String(requests.agents() || "");
    if (!quickOpen.agentRequestId) {
      quickOpen.agentLoading = false;
      quickOpen.agentError = "Agent 权威快照请求未发送。";
    }
  }
  return quickOpen.provider;
}

export function requestCommandQuickOpenFiles(state, requestFiles, { refresh = false } = {}) {
  const quickOpen = ensureCommandQuickOpenState(state);
  if (quickOpen.provider !== "files" || typeof requestFiles !== "function") return false;
  quickOpen.fileLoading = true;
  quickOpen.fileError = "";
  quickOpen.fileRequestId = String(requestFiles(quickOpen.query, Boolean(refresh)) || "");
  if (!quickOpen.fileRequestId) {
    quickOpen.fileLoading = false;
    quickOpen.fileError = "Workspace 文件索引请求未发送。";
    return false;
  }
  return true;
}

export function applyCommandQuickOpenFileSnapshot(state, requestId, payload) {
  const quickOpen = ensureCommandQuickOpenState(state);
  if (!quickOpen.fileRequestId || quickOpen.fileRequestId !== String(requestId || "")) return false;
  quickOpen.fileItems = payload?.status === "ready" && Array.isArray(payload.items)
    ? payload.items.slice(0, FILE_RESULT_LIMIT) : [];
  quickOpen.fileLoaded = payload?.status === "ready";
  quickOpen.fileLoading = false;
  quickOpen.fileError = payload?.status === "cancelled"
    ? String(payload.message || "Workspace 文件索引构建已取消。").slice(0, 500) : "";
  quickOpen.fileRequestId = "";
  quickOpen.fileMeta = payload?.status === "ready" ? {
    revision: payload.revision,
    totalIndexed: payload.total_indexed,
    truncated: payload.truncated,
    source: payload.source,
  } : null;
  quickOpen.selectedIndex = 0;
  return true;
}

export function failCommandQuickOpenFileSnapshot(state, requestId, message = "") {
  const quickOpen = ensureCommandQuickOpenState(state);
  if (!quickOpen.fileRequestId || quickOpen.fileRequestId !== String(requestId || "")) return false;
  quickOpen.fileLoading = false;
  quickOpen.fileError = String(
    message || "Workspace 文件索引读取失败，请检查目录权限后重试。",
  ).slice(0, 500);
  quickOpen.fileRequestId = "";
  return true;
}

export function applyCommandQuickOpenAgentSnapshot(state, requestId, payload) {
  const quickOpen = ensureCommandQuickOpenState(state);
  const correlationId = String(requestId || "");
  if (quickOpen.agentDiscardRequestIds.includes(correlationId)) {
    quickOpen.agentDiscardRequestIds = quickOpen.agentDiscardRequestIds.filter(
      (item) => item !== correlationId,
    );
    return true;
  }
  if (!quickOpen.agentRequestId || quickOpen.agentRequestId !== correlationId) {
    return false;
  }
  if (
    state.currentSessionId
    && String(payload?.session_id || "") !== String(state.currentSessionId)
  ) {
    quickOpen.agentLoading = false;
    quickOpen.agentError = "Agent 快照会话与当前会话不一致。";
    quickOpen.agentRequestId = "";
    return true;
  }
  quickOpen.agentItems = Array.isArray(payload?.agents)
    ? payload.agents.slice(0, 100) : [];
  quickOpen.agentWarnings = Array.isArray(payload?.warnings)
    ? payload.warnings.slice(0, 20) : [];
  quickOpen.agentLoaded = true;
  quickOpen.agentLoading = false;
  quickOpen.agentError = "";
  quickOpen.agentRequestId = "";
  quickOpen.agentRevision = Number(payload?.revision) || 0;
  quickOpen.selectedIndex = 0;
  return true;
}

export function failCommandQuickOpenAgentSnapshot(state, requestId, message = "") {
  const quickOpen = ensureCommandQuickOpenState(state);
  const correlationId = String(requestId || "");
  if (quickOpen.agentDiscardRequestIds.includes(correlationId)) {
    quickOpen.agentDiscardRequestIds = quickOpen.agentDiscardRequestIds.filter(
      (item) => item !== correlationId,
    );
    return true;
  }
  if (!quickOpen.agentRequestId || quickOpen.agentRequestId !== correlationId) {
    return false;
  }
  quickOpen.agentLoading = false;
  quickOpen.agentError = String(
    message || "Agent 权威快照读取失败，请稍后重试。",
  ).slice(0, 500);
  quickOpen.agentRequestId = "";
  return true;
}

export function applyCommandQuickOpenSessionSnapshot(state, requestId, payload) {
  const quickOpen = ensureCommandQuickOpenState(state);
  if (!quickOpen.sessionRequestId || quickOpen.sessionRequestId !== String(requestId || "")) return false;
  quickOpen.sessionItems = Array.isArray(payload?.items) ? payload.items.slice(0, SESSION_RESULT_LIMIT) : [];
  quickOpen.sessionWarnings = Array.isArray(payload?.warnings) ? payload.warnings.slice(0, 10) : [];
  quickOpen.sessionLoaded = true;
  quickOpen.sessionLoading = false;
  quickOpen.sessionError = "";
  quickOpen.sessionRequestId = "";
  quickOpen.selectedIndex = 0;
  return true;
}

export function failCommandQuickOpenSessionSnapshot(state, requestId, message = "") {
  const quickOpen = ensureCommandQuickOpenState(state);
  if (!quickOpen.sessionRequestId || quickOpen.sessionRequestId !== String(requestId || "")) return false;
  quickOpen.sessionLoading = false;
  quickOpen.sessionError = String(message || "会话快照读取失败，请稍后重试。").slice(0, 500);
  quickOpen.sessionRequestId = "";
  return true;
}

export function applyCommandQuickOpenTaskSnapshot(state, requestId, payload) {
  const quickOpen = ensureCommandQuickOpenState(state);
  if (!quickOpen.taskRequestId || quickOpen.taskRequestId !== String(requestId || "")) return false;
  quickOpen.taskItems = Array.isArray(payload?.items) ? payload.items.slice(0, RESULT_LIMIT) : [];
  quickOpen.taskWarnings = Array.isArray(payload?.warnings) ? payload.warnings.slice(0, 20) : [];
  quickOpen.taskLoaded = true;
  quickOpen.taskLoading = false;
  quickOpen.taskError = "";
  quickOpen.taskRequestId = "";
  quickOpen.selectedIndex = 0;
  return true;
}

export function failCommandQuickOpenTaskSnapshot(state, requestId, message = "") {
  const quickOpen = ensureCommandQuickOpenState(state);
  if (!quickOpen.taskRequestId || quickOpen.taskRequestId !== String(requestId || "")) return false;
  quickOpen.taskLoading = false;
  quickOpen.taskError = String(message || "任务快照读取失败，请运行 /doctor 后重试。").slice(0, 500);
  quickOpen.taskRequestId = "";
  return true;
}

export function resetCommandQuickOpenTaskCache(state) {
  const quickOpen = ensureCommandQuickOpenState(state);
  quickOpen.taskItems = [];
  quickOpen.taskWarnings = [];
  quickOpen.taskLoaded = false;
  quickOpen.taskLoading = false;
  quickOpen.taskError = "";
  quickOpen.taskRequestId = "";
  quickOpen.sessionItems = [];
  quickOpen.sessionWarnings = [];
  quickOpen.sessionLoaded = false;
  quickOpen.sessionLoading = false;
  quickOpen.sessionError = "";
  quickOpen.sessionRequestId = "";
  quickOpen.fileItems = [];
  quickOpen.fileLoaded = false;
  quickOpen.fileLoading = false;
  quickOpen.fileError = "";
  quickOpen.fileRequestId = "";
  quickOpen.fileMeta = null;
  quickOpen.agentItems = [];
  quickOpen.agentWarnings = [];
  quickOpen.agentLoaded = false;
  quickOpen.agentLoading = false;
  quickOpen.agentError = "";
  quickOpen.agentRequestId = "";
  quickOpen.agentRevision = 0;
  quickOpen.provider = "commands";
  quickOpen.selectedIndex = 0;
}

export function moveCommandQuickOpenSelection(state, direction) {
  const items = getCommandQuickOpenItems(state);
  if (!items.length) return false;
  const quickOpen = ensureCommandQuickOpenState(state);
  const offset = direction === "previous" ? -1 : 1;
  quickOpen.selectedIndex = (quickOpen.selectedIndex + offset + items.length) % items.length;
  return true;
}

export function acceptCommandQuickOpen(state) {
  const items = getCommandQuickOpenItems(state);
  if (!items.length) return false;
  const selected = items.find((item) => item.selected) || items[0];
  setInputText(
    state,
    selected.provider === "tasks" ? taskTemplate(selected)
      : selected.provider === "sessions" ? sessionTemplate(selected)
        : selected.provider === "files" ? fileTemplate(selected)
          : selected.provider === "agents" ? agentTemplate(selected)
            : selected.provider === "pages" ? pageTemplate(selected)
        : commandTemplate(selected),
  );
  closeCommandQuickOpen(state);
  return true;
}

export function searchSessionEntries(entries, query, limit = SESSION_RESULT_LIMIT) {
  const term = normalizeSearchText(query).slice(0, QUERY_LIMIT);
  return (Array.isArray(entries) ? entries : []).filter((entry) => entry?.resumable && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(String(entry.session_id || "")))
    .map((entry) => ({ entry, text: normalizeSearchText([entry.session_id, entry.title, entry.model, entry.git_branch].join(" ")) }))
    .filter(({ text }) => !term || text.includes(term) || subsequenceGap(term, text) !== null)
    .sort((a, b) => Number(b.entry.is_current) - Number(a.entry.is_current) || String(b.entry.updated_at).localeCompare(String(a.entry.updated_at)))
    .slice(0, Math.max(1, Math.min(SESSION_RESULT_LIMIT, Number(limit) || SESSION_RESULT_LIMIT)))
    .map(({ entry }) => entry);
}

export function sessionTemplate(entry) {
  if (!entry?.resumable || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(String(entry.session_id || ""))) throw new Error("会话 ID 无法安全填入 QuickOpen。");
  return `/load ${entry.session_id}`;
}

export function fileTemplate(entry) {
  const template = String(entry?.template || "");
  if (
    !entry
    || typeof entry !== "object"
    || typeof entry.path !== "string"
    || !template.startsWith("/read ")
    || template.length > 4_200
  ) {
    throw new Error("Workspace 文件无法安全填入 QuickOpen。");
  }
  return template;
}

export function searchAgentEntries(entries, query, limit = AGENT_RESULT_LIMIT) {
  const boundedLimit = Math.max(
    1,
    Math.min(AGENT_RESULT_LIMIT, Math.trunc(Number(limit) || AGENT_RESULT_LIMIT)),
  );
  const term = normalizeSearchText(query).slice(0, QUERY_LIMIT);
  return (Array.isArray(entries) ? entries : []).slice(0, 100)
    .filter(isSafeAgentEntry)
    .map((entry) => ({ entry, score: agentSearchScore(entry, term) }))
    .filter((item) => item.score !== null)
    .sort((left, right) => left.score - right.score
      || (AGENT_STATE_ORDER[left.entry.state] ?? 9) - (AGENT_STATE_ORDER[right.entry.state] ?? 9)
      || (AGENT_KIND_ORDER[left.entry.kind] ?? 9) - (AGENT_KIND_ORDER[right.entry.kind] ?? 9)
      || String(left.entry.name).localeCompare(String(right.entry.name)))
    .slice(0, boundedLimit)
    .map((item) => item.entry);
}

export function agentTemplate(entry) {
  if (!isSafeAgentEntry(entry)) {
    throw new Error("Agent 名称无法安全填入 QuickOpen。");
  }
  return `/agents agent ${shellQuote(String(entry.name))}`;
}

export function searchPageEntries(entries, query, limit = PAGE_RESULT_LIMIT) {
  const boundedLimit = Math.max(
    1,
    Math.min(PAGE_RESULT_LIMIT, Math.trunc(Number(limit) || PAGE_RESULT_LIMIT)),
  );
  const term = normalizeSearchText(query).slice(0, QUERY_LIMIT).replace(/^\//, "");
  return (Array.isArray(entries) ? entries : []).slice(0, PAGE_RESULT_LIMIT)
    .filter(isSafePageEntry)
    .map((entry) => ({ entry, score: pageSearchScore(entry, term) }))
    .filter((item) => item.score !== null)
    .sort((left, right) => left.score - right.score
      || left.entry.order - right.entry.order
      || String(left.entry.page_id).localeCompare(String(right.entry.page_id)))
    .slice(0, boundedLimit)
    .map((item) => item.entry);
}

export function pageTemplate(entry) {
  if (!isSafePageEntry(entry)) {
    throw new Error("页面命令无法安全填入 QuickOpen。");
  }
  return String(entry.command);
}

export function parseAgentDeepLink(text) {
  const parts = splitShellWords(String(text || ""));
  if (
    parts.length !== 3
    || parts[0].toLocaleLowerCase("und") !== "/agents"
    || parts[1].toLocaleLowerCase("und") !== "agent"
    || !isSafeAgentName(parts[2])
  ) return null;
  return parts[2];
}

export function searchTaskEntries(entries, query, limit = TASK_RESULT_LIMIT) {
  const boundedLimit = Math.max(1, Math.min(TASK_RESULT_LIMIT, Math.trunc(Number(limit) || TASK_RESULT_LIMIT)));
  const term = normalizeSearchText(query).slice(0, QUERY_LIMIT);
  return (Array.isArray(entries) ? entries : []).slice(0, RESULT_LIMIT)
    .filter(isSafeTaskEntry)
    .map((entry) => ({ entry, score: taskSearchScore(entry, term) }))
    .filter((item) => item.score !== null)
    .sort((left, right) => left.score - right.score
      || (TASK_STATUS_ORDER[left.entry.status] ?? 9) - (TASK_STATUS_ORDER[right.entry.status] ?? 9)
      || (TASK_SOURCE_ORDER[left.entry.source] ?? 9) - (TASK_SOURCE_ORDER[right.entry.source] ?? 9)
      || String(left.entry.view_id).localeCompare(String(right.entry.view_id)))
    .slice(0, boundedLimit)
    .map((item) => item.entry);
}

export function taskTemplate(entry) {
  if (!isSafeTaskEntry(entry)) throw new Error("任务 ID 无法安全填入 QuickOpen。");
  return `/tasks detail ${entry.task_id}`;
}

export function searchCommandEntries(entries, query, limit = 50, recentCommands = []) {
  const boundedLimit = Math.max(1, Math.min(RESULT_LIMIT, Math.trunc(Number(limit) || 50)));
  const term = normalizeSearchText(query).slice(0, QUERY_LIMIT).replace(/^\//, "");
  const recentRank = new Map(
    (Array.isArray(recentCommands) ? recentCommands : [])
      .slice(0, 20)
      .map((command, index) => [String(command), index]),
  );
  return (Array.isArray(entries) ? entries : [])
    .map((entry) => ({ entry, score: commandSearchScore(entry, term) }))
    .filter((item) => item.score !== null)
    .sort((left, right) => left.score - right.score
      || (recentRank.get(left.entry.command) ?? 20) - (recentRank.get(right.entry.command) ?? 20)
      || String(left.entry.command).localeCompare(String(right.entry.command)))
    .slice(0, boundedLimit)
    .map((item) => item.entry);
}

export function recordRecentCommand(entries, recentCommands, submittedText, limit = 20) {
  const boundedLimit = Math.trunc(Number(limit));
  if (!Number.isInteger(boundedLimit) || boundedLimit < 1 || boundedLimit > 20) {
    throw new Error("最近命令 limit 必须在 1 到 20 之间。");
  }
  const [rawToken = ""] = String(submittedText ?? "").trim().split(/\s+/, 1);
  const token = rawToken.toLocaleLowerCase("und");
  if (!token.startsWith("/")) return sanitizeRecentCommands(recentCommands, boundedLimit);
  const entry = (Array.isArray(entries) ? entries : []).find((candidate) => {
    if (String(candidate?.command || "").toLocaleLowerCase("und") === token) return true;
    return (Array.isArray(candidate?.aliases) ? candidate.aliases : [])
      .some((alias) => String(alias).toLocaleLowerCase("und") === token);
  });
  if (!entry?.command) return sanitizeRecentCommands(recentCommands, boundedLimit);
  const canonical = String(entry.command);
  return [canonical, ...sanitizeRecentCommands(recentCommands, boundedLimit)
    .filter((command) => command !== canonical)].slice(0, boundedLimit);
}

function commandSearchScore(entry, term) {
  if (!entry || typeof entry !== "object") return null;
  if (!term) return 0;
  const command = normalizeSearchText(String(entry.command || "").replace(/^\//, ""));
  const aliases = (Array.isArray(entry.aliases) ? entry.aliases : [])
    .map((alias) => normalizeSearchText(String(alias || "").replace(/^\//, "")));
  if (term === command) return 0;
  if (aliases.includes(term)) return 1;
  if (command.startsWith(term)) return 10_000 + command.length - term.length;
  const aliasPrefixes = aliases
    .filter((alias) => alias.startsWith(term))
    .map((alias) => alias.length - term.length);
  if (aliasPrefixes.length) return 12_000 + Math.min(...aliasPrefixes);
  if (command.includes(term)) return 20_000 + command.indexOf(term);
  const aliasPositions = aliases
    .filter((alias) => alias.includes(term))
    .map((alias) => alias.indexOf(term));
  if (aliasPositions.length) return 30_000 + Math.min(...aliasPositions);
  const metadata = normalizeSearchText([
    entry.description,
    entry.category,
    commandCategoryLabel(entry.category),
    entry.permission_risk,
    commandRiskLabel(entry.permission_risk),
    entry.source,
  ].join(" "));
  if (metadata.includes(term)) return 40_000 + metadata.indexOf(term);
  const commandGap = subsequenceGap(term, command);
  if (commandGap !== null) return 60_000 + commandGap;
  const aliasGaps = aliases.map((alias) => subsequenceGap(term, alias)).filter((gap) => gap !== null);
  if (aliasGaps.length) return 70_000 + Math.min(...aliasGaps);
  const metadataGap = subsequenceGap(term, metadata);
  return metadataGap === null ? null : 100_000 + metadataGap;
}

function taskSearchScore(entry, term) {
  if (!term) return 0;
  const taskId = normalizeSearchText(entry.task_id);
  const viewId = normalizeSearchText(entry.view_id);
  const title = normalizeSearchText(entry.title);
  const owner = normalizeSearchText(entry.owner);
  if (term === taskId || term === viewId) return 0;
  if (taskId.startsWith(term) || viewId.startsWith(term)) return 10_000;
  if (title.startsWith(term)) return 20_000;
  if (taskId.includes(term) || viewId.includes(term)) return 30_000;
  if (title.includes(term)) return 40_000;
  const metadata = normalizeSearchText([
    entry.source,
    entry.status,
    entry.owner,
    entry.detail,
    taskSourceLabel(entry.source),
    taskStatusLabel(entry.status),
  ].join(" "));
  if (metadata.includes(term)) return 50_000;
  const gap = subsequenceGap(term, [taskId, title, owner, metadata].join(" "));
  return gap === null ? null : 100_000 + gap;
}

function agentSearchScore(entry, term) {
  if (!term) return 0;
  const name = normalizeSearchText(entry.name);
  if (term === name) return 0;
  if (name.startsWith(term)) return 10_000 + name.length - term.length;
  if (name.includes(term)) return 20_000 + name.indexOf(term);
  const metadata = normalizeSearchText([
    entry.description,
    entry.kind,
    entry.kind === "dynamic" ? "动态" : "预置",
    entry.state,
    ({
      uninitialized: "未初始化",
      spawned: "已启动",
      ready: "就绪",
      running: "运行中",
      idle: "空闲",
      destroyed: "已销毁",
    })[entry.state],
    entry.model_tier,
    entry.permission_level,
    ...(Array.isArray(entry.capabilities) ? entry.capabilities : []),
    ...(Array.isArray(entry.tools) ? entry.tools : []),
  ].join(" "));
  if (metadata.includes(term)) return 30_000 + metadata.indexOf(term);
  const gap = subsequenceGap(term, `${name} ${metadata}`);
  return gap === null ? null : 100_000 + gap;
}

function pageSearchScore(entry, term) {
  if (!term) return 0;
  const pageId = normalizeSearchText(entry.page_id);
  const command = normalizeSearchText(entry.command).replace(/^\//, "");
  const label = normalizeSearchText(entry.label);
  if ([pageId, command, label].includes(term)) return 0;
  if (pageId.startsWith(term) || command.startsWith(term)) return 10_000;
  if (label.startsWith(term)) return 20_000;
  if (pageId.includes(term) || command.includes(term)) return 30_000;
  if (label.includes(term)) return 40_000;
  const metadata = normalizeSearchText([
    ...(Array.isArray(entry.keywords) ? entry.keywords : []),
    entry.description,
    entry.surface,
  ].join(" "));
  if (metadata.includes(term)) return 50_000 + metadata.indexOf(term);
  const gap = subsequenceGap(term, `${pageId} ${command} ${label} ${metadata}`);
  return gap === null ? null : 100_000 + gap;
}

function isSafePageEntry(entry) {
  return Boolean(
    entry
    && typeof entry === "object"
    && entry.schema_version === 1
    && /^[a-z][a-z0-9_-]{0,63}$/.test(String(entry.page_id || ""))
    && /^\/[a-z][a-z0-9_-]{0,63}$/.test(String(entry.command || ""))
    && isSafePageText(entry.label, 80)
    && isSafePageText(entry.description, 300)
    && Array.isArray(entry.keywords)
    && entry.keywords.length <= 12
    && entry.keywords.every((keyword) => isSafePageText(keyword, 80))
    && entry.keywords.length === new Set(entry.keywords).size
    && entry.keywords.every(
      (keyword, index) => keyword === [...entry.keywords].sort()[index],
    )
    && Number.isInteger(entry.order)
    && entry.order >= 0
    && entry.order <= 1_000
    && entry.surface === "new_ui"
  );
}

function isSafePageText(value, limit) {
  return typeof value === "string"
    && value.length >= 1
    && value.length <= limit
    && value === value.trim()
    && value === value.normalize("NFKC")
    && !/[\u0000-\u001f\u007f]/.test(value);
}

function isSafeAgentEntry(entry) {
  return Boolean(
    entry
    && typeof entry === "object"
    && isSafeAgentName(entry.name)
    && Object.hasOwn(AGENT_STATE_ORDER, String(entry.state || ""))
    && Object.hasOwn(AGENT_KIND_ORDER, String(entry.kind || ""))
  );
}

function isSafeAgentName(value) {
  const name = String(value || "");
  return name.length >= 1 && name.length <= 200 && !/[\u0000-\u001f\u007f]/.test(name);
}

function shellQuote(value) {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(value)) return value;
  return `'${value.replaceAll("'", `'\"'\"'`)}'`;
}

function splitShellWords(value) {
  const words = [];
  let word = "";
  let quote = "";
  let started = false;
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (quote) {
      if (character === quote) {
        quote = "";
      } else if (quote === "\"" && character === "\\" && index + 1 < value.length) {
        word += value[index += 1];
      } else {
        word += character;
      }
      started = true;
    } else if (character === "'" || character === "\"") {
      quote = character;
      started = true;
    } else if (/\s/.test(character)) {
      if (started) {
        words.push(word);
        word = "";
        started = false;
      }
    } else if (character === "\\" && index + 1 < value.length) {
      word += value[index += 1];
      started = true;
    } else {
      word += character;
      started = true;
    }
  }
  if (quote) return [];
  if (started) words.push(word);
  return words;
}

function isSafeTaskEntry(entry) {
  return Boolean(
    entry
    && typeof entry === "object"
    && Object.hasOwn(TASK_SOURCE_ORDER, String(entry.source || ""))
    && String(entry.view_id || "")
    && /^[A-Za-z0-9._:/-]{1,256}$/.test(String(entry.task_id || ""))
  );
}

function taskSourceLabel(source) {
  return ({ todo: "待办", subagent: "子智能体", background: "后台任务", browser: "浏览器" })[source] || source;
}

function taskStatusLabel(status) {
  return ({
    pending: "等待",
    running: "运行中",
    blocked: "阻塞",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  })[status] || status;
}

function subsequenceGap(term, target) {
  let position = -1;
  let score = 0;
  for (const character of term) {
    const nextPosition = target.indexOf(character, position + 1);
    if (nextPosition < 0) return null;
    score += position < 0 ? nextPosition : nextPosition - position - 1;
    position = nextPosition;
  }
  return score;
}

function normalizeSearchText(value) {
  return String(value ?? "").normalize("NFKC").trim().toLocaleLowerCase("und");
}

function segmentGraphemes(value) {
  return [...GRAPHEME_SEGMENTER.segment(String(value ?? ""))].map((item) => item.segment);
}

function ensureCommandQuickOpenState(state) {
  if (!state.commandQuickOpen || typeof state.commandQuickOpen !== "object") {
    state.commandQuickOpen = {
      open: false,
      query: "",
      selectedIndex: 0,
      draftText: "",
      draftCursor: 0,
      recentCommands: [],
      provider: "commands",
      taskItems: [],
      taskWarnings: [],
      taskLoaded: false,
      taskLoading: false,
      taskError: "",
      taskRequestId: "",
      sessionItems: [], sessionWarnings: [], sessionLoaded: false, sessionLoading: false,
      sessionError: "", sessionRequestId: "",
      fileItems: [], fileLoaded: false, fileLoading: false, fileError: "",
      fileRequestId: "", fileMeta: null,
      agentItems: [], agentWarnings: [], agentLoaded: false, agentLoading: false,
      agentError: "", agentRequestId: "", agentRevision: 0, agentDiscardRequestIds: [],
    };
  }
  if (!Array.isArray(state.commandQuickOpen.recentCommands)) {
    state.commandQuickOpen.recentCommands = [];
  }
  if (!Array.isArray(state.commandQuickOpen.taskItems)) state.commandQuickOpen.taskItems = [];
  if (!Array.isArray(state.commandQuickOpen.taskWarnings)) state.commandQuickOpen.taskWarnings = [];
  if (!Array.isArray(state.commandQuickOpen.sessionItems)) state.commandQuickOpen.sessionItems = [];
  if (!Array.isArray(state.commandQuickOpen.sessionWarnings)) state.commandQuickOpen.sessionWarnings = [];
  if (!Array.isArray(state.commandQuickOpen.fileItems)) state.commandQuickOpen.fileItems = [];
  if (!Array.isArray(state.commandQuickOpen.agentItems)) state.commandQuickOpen.agentItems = [];
  if (!Array.isArray(state.commandQuickOpen.agentWarnings)) state.commandQuickOpen.agentWarnings = [];
  if (!Array.isArray(state.commandQuickOpen.agentDiscardRequestIds)) {
    state.commandQuickOpen.agentDiscardRequestIds = [];
  }
  if (!["commands", "tasks", "sessions", "files", "agents", "pages"].includes(state.commandQuickOpen.provider)) {
    state.commandQuickOpen.provider = "commands";
  }
  return state.commandQuickOpen;
}

function sanitizeRecentCommands(value, limit) {
  const commands = Array.isArray(value) ? value : [];
  return [...new Set(commands
    .map((command) => String(command))
    .filter((command) => /^\/[A-Za-z0-9_-]{1,64}$/.test(command)))]
    .slice(0, limit);
}
