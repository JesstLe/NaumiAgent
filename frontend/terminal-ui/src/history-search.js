import { getInputCursor, setInputText } from "./input-buffer.js";

export function openHistorySearch(state) {
  if (state.historySearch?.open) {
    return cycleHistorySearch(state);
  }
  state.historySearch = {
    open: true,
    query: "",
    matches: historyMatches(state.inputHistory, ""),
    selectedIndex: 0,
    draftText: String(state.input ?? ""),
    draftCursor: getInputCursor(state),
  };
  return true;
}

export function appendHistorySearchQuery(state, text) {
  if (!state.historySearch?.open) return false;
  state.historySearch.query += String(text ?? "");
  refreshMatches(state);
  return true;
}

export function backspaceHistorySearchQuery(state) {
  if (!state.historySearch?.open || !state.historySearch.query) return false;
  const graphemes = Array.from(
    new Intl.Segmenter("und", { granularity: "grapheme" }).segment(state.historySearch.query),
    (entry) => entry.segment,
  );
  graphemes.pop();
  state.historySearch.query = graphemes.join("");
  refreshMatches(state);
  return true;
}

export function cycleHistorySearch(state) {
  const search = state.historySearch;
  if (!search?.open || !search.matches.length) return false;
  search.selectedIndex = (search.selectedIndex + 1) % search.matches.length;
  return true;
}

export function moveHistorySearchSelection(state, direction) {
  const search = state.historySearch;
  if (!search?.open || !search.matches.length) return false;
  const offset = direction === "older" ? 1 : -1;
  search.selectedIndex = Math.max(
    0,
    Math.min(search.matches.length - 1, search.selectedIndex + offset),
  );
  return true;
}

export function acceptHistorySearch(state) {
  const search = state.historySearch;
  if (!search?.open || !search.matches.length) return false;
  const selected = search.matches[search.selectedIndex];
  closeHistorySearch(state);
  setInputText(state, selected);
  return true;
}

export function cancelHistorySearch(state) {
  const search = state.historySearch;
  if (!search?.open) return false;
  const draftText = search.draftText;
  const draftCursor = search.draftCursor;
  closeHistorySearch(state);
  setInputText(state, draftText, draftCursor);
  return true;
}

export function resetHistorySearch(state) {
  closeHistorySearch(state);
}

function refreshMatches(state) {
  state.historySearch.matches = historyMatches(
    state.inputHistory,
    state.historySearch.query,
  );
  state.historySearch.selectedIndex = 0;
}

function historyMatches(history, query) {
  const needle = String(query ?? "").toLocaleLowerCase();
  const seen = new Set();
  const matches = [];
  for (const raw of [...(Array.isArray(history) ? history : [])].reverse()) {
    const value = typeof raw === "string" ? raw : "";
    if (!value || seen.has(value)) continue;
    seen.add(value);
    if (!needle || value.toLocaleLowerCase().includes(needle)) {
      matches.push(value);
    }
  }
  return matches;
}

function closeHistorySearch(state) {
  state.historySearch = {
    open: false,
    query: "",
    matches: [],
    selectedIndex: 0,
    draftText: "",
    draftCursor: 0,
  };
}
