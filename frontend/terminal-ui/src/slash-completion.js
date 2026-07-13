import { setInputText } from "./input-buffer.js";
import { getSlashCommandCompletions } from "./state.js";

export function syncSlashCompletion(state) {
  const completion = ensureCompletionState(state);
  const input = String(state.input ?? "");
  if (completion.input !== input) {
    completion.input = input;
    completion.selectedIndex = 0;
  }
  const count = visibleCandidates(state).length;
  if (count) completion.selectedIndex = Math.min(completion.selectedIndex, count - 1);
  else completion.selectedIndex = 0;
  return count > 0;
}

export function getSlashCompletionItems(state) {
  syncSlashCompletion(state);
  const completion = ensureCompletionState(state);
  return visibleCandidates(state).map((item, index) => ({
    ...item,
    selected: index === completion.selectedIndex,
  }));
}

export function isSlashCompletionOpen(state) {
  return getSlashCompletionItems(state).length > 0;
}

export function moveSlashCompletionSelection(state, direction) {
  const items = getSlashCompletionItems(state);
  if (!items.length) return false;
  const completion = ensureCompletionState(state);
  const offset = direction === "previous" ? -1 : 1;
  completion.selectedIndex = (completion.selectedIndex + offset + items.length) % items.length;
  return true;
}

export function acceptSlashCompletion(state) {
  const items = getSlashCompletionItems(state);
  if (!items.length) return false;
  const completion = ensureCompletionState(state);
  const selected = items[completion.selectedIndex];
  if (String(state.input ?? "") === selected.command) return false;
  setInputText(state, selected.command);
  completion.input = state.input;
  completion.selectedIndex = 0;
  completion.dismissedInput = state.input;
  return true;
}

export function dismissSlashCompletion(state) {
  if (!isSlashCompletionOpen(state)) return false;
  const completion = ensureCompletionState(state);
  completion.dismissedInput = String(state.input ?? "");
  return true;
}

export function resetSlashCompletion(state) {
  state.slashCompletion = { input: "", selectedIndex: 0, dismissedInput: null };
}

function visibleCandidates(state) {
  const completion = ensureCompletionState(state);
  const input = String(state.input ?? "");
  if (completion.dismissedInput === input) return [];
  return getSlashCommandCompletions(input, state.slashCommands);
}

function ensureCompletionState(state) {
  if (!state.slashCompletion || typeof state.slashCompletion !== "object") {
    state.slashCompletion = { input: "", selectedIndex: 0, dismissedInput: null };
  }
  return state.slashCompletion;
}
