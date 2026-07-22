import { commandCategoryLabel, commandRiskLabel, commandTemplate } from "./command-metadata.js";
import { setInputText } from "./input-buffer.js";

const QUERY_LIMIT = 200;
const RESULT_LIMIT = 200;
const GRAPHEME_SEGMENTER = new Intl.Segmenter("und", { granularity: "grapheme" });

export function openCommandQuickOpen(state) {
  const quickOpen = ensureCommandQuickOpenState(state);
  quickOpen.open = true;
  quickOpen.query = "";
  quickOpen.selectedIndex = 0;
  quickOpen.draftText = String(state.input ?? "");
  quickOpen.draftCursor = state.inputCursor;
  return true;
}

export function closeCommandQuickOpen(state) {
  const quickOpen = ensureCommandQuickOpenState(state);
  if (!quickOpen.open) return false;
  quickOpen.open = false;
  quickOpen.query = "";
  quickOpen.selectedIndex = 0;
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
  const ranked = searchCommandEntries(
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
    selected: index === quickOpen.selectedIndex,
    recent: recent.has(entry.command),
  }));
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
  setInputText(state, commandTemplate(selected));
  closeCommandQuickOpen(state);
  return true;
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
    };
  }
  if (!Array.isArray(state.commandQuickOpen.recentCommands)) {
    state.commandQuickOpen.recentCommands = [];
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
