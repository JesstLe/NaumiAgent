import { charWidth } from "./ansi.js";

export const INPUT_KEYS = {
  shiftTab: "\x1b[Z",
  shiftEnter: "\x1b[13;2u",
  ctrlEnter: "\x1b[13;5u",
  pageUp: "\x1b[5~",
  pageDown: "\x1b[6~",
  up: "\x1b[A",
  upAlt: "\x1bOA",
  down: "\x1b[B",
  downAlt: "\x1bOB",
  left: "\x1b[D",
  leftAlt: "\x1bOD",
  right: "\x1b[C",
  rightAlt: "\x1bOC",
  home: "\x1b[H",
  homeAlt: "\x1b[1~",
  homeSs3: "\x1bOH",
  end: "\x1b[F",
  endAlt: "\x1b[4~",
  endSs3: "\x1bOF",
  delete: "\x1b[3~",
  ctrlA: "\x01",
  ctrlE: "\x05",
};

const BRACKETED_PASTE_START = "\x1b[200~";
const BRACKETED_PASTE_END = "\x1b[201~";
const CSI_PATTERN = /^\x1b\[[0-9;?]*[~A-Za-z]/;
const SS3_PATTERN = /^\x1b[Oo][A-Za-z]/;
const SS3_FALLBACK_PATTERN = /^[Oo][ABab]$/;
const MAX_PENDING_ESCAPE_CHARS = 16;
const GRAPHEME_SEGMENTER = new Intl.Segmenter("und", { granularity: "grapheme" });

export function splitInputStreamChunk(chunk, pending = "") {
  const combined = `${pending ?? ""}${chunk ?? ""}`;
  const trailing = extractTrailingIncompleteEscape(combined);
  const text = trailing ? combined.slice(0, -trailing.length) : combined;
  return {
    keys: splitInputChunk(text),
    pending: trailing,
  };
}

export function createInputTokenizerState() {
  return { pendingEscape: "", pasteBuffer: null };
}

export function tokenizeInputChunk(chunk, state) {
  let text = `${state.pendingEscape}${String(chunk ?? "")}`;
  state.pendingEscape = "";
  const tokens = [];

  while (text) {
    if (state.pasteBuffer !== null) {
      const end = text.indexOf(BRACKETED_PASTE_END);
      if (end < 0) {
        const overlap = longestSuffixPrefix(text, BRACKETED_PASTE_END);
        state.pasteBuffer += text.slice(0, text.length - overlap);
        state.pendingEscape = text.slice(text.length - overlap);
        return tokens;
      }
      state.pasteBuffer += text.slice(0, end);
      tokens.push({ type: "paste", value: state.pasteBuffer });
      state.pasteBuffer = null;
      text = text.slice(end + BRACKETED_PASTE_END.length);
      continue;
    }

    const start = text.indexOf(BRACKETED_PASTE_START);
    if (start >= 0) {
      for (const key of splitInputChunk(text.slice(0, start))) {
        tokens.push({ type: "key", value: normalizeModifiedEnter(key) });
      }
      state.pasteBuffer = "";
      text = text.slice(start + BRACKETED_PASTE_START.length);
      continue;
    }

    const trailing = extractTrailingIncompleteEscape(text);
    const complete = trailing ? text.slice(0, -trailing.length) : text;
    for (const key of splitInputChunk(complete)) {
      tokens.push({ type: "key", value: normalizeModifiedEnter(key) });
    }
    state.pendingEscape = trailing;
    break;
  }
  return tokens;
}

export function splitInputChunk(chunk) {
  const keys = [];
  let text = String(chunk ?? "");

  while (text) {
    if (text.startsWith("\x1bO") || text.startsWith("\x1bo")) {
      const match = text.match(SS3_PATTERN);
      if (match) {
        keys.push(normalizeSs3Key(match[0]));
        text = text.slice(match[0].length);
        continue;
      }
    }

    if (text.length === 2 && SS3_FALLBACK_PATTERN.test(text)) {
      keys.push(normalizeSs3Key(`\x1b${text}`));
      text = "";
      continue;
    }

    if (text.startsWith("\x1b[")) {
      const match = text.match(CSI_PATTERN);
      if (match) {
        keys.push(match[0]);
        text = text.slice(match[0].length);
        continue;
      }
    }

    const [first] = Array.from(text);
    if (isControlInput(first)) {
      keys.push(first);
      text = text.slice(first.length);
      continue;
    }

    let printable = "";
    for (const char of Array.from(text)) {
      if (char === "\x1b" || isControlInput(char)) break;
      printable += char;
    }
    if (printable) {
      keys.push(printable);
      text = text.slice(printable.length);
      continue;
    }

    keys.push(first);
    text = text.slice(first.length);
  }

  return keys;
}

export function getInputCursor(state) {
  return clampCursor(state.input, state.inputCursor);
}

export function setInputText(state, text, cursor = null) {
  state.input = String(text ?? "");
  state.inputCursor = clampCursor(state.input, cursor ?? segmentGraphemes(state.input).length);
  state.inputPreferredColumn = null;
}

export function clearInput(state) {
  setInputText(state, "");
}

export function insertInputText(state, text) {
  resetInputHistoryNavigation(state);
  resetPreferredColumn(state);
  const chars = segmentGraphemes(state.input);
  const insertChars = segmentGraphemes(text);
  const cursor = getInputCursor(state);
  chars.splice(cursor, 0, ...insertChars);
  state.input = chars.join("");
  state.inputCursor = cursor + insertChars.length;
}

export function backspaceInput(state) {
  resetInputHistoryNavigation(state);
  resetPreferredColumn(state);
  const chars = segmentGraphemes(state.input);
  const cursor = getInputCursor(state);
  if (cursor <= 0) return false;
  chars.splice(cursor - 1, 1);
  state.input = chars.join("");
  state.inputCursor = cursor - 1;
  return true;
}

export function deleteInputForward(state) {
  resetInputHistoryNavigation(state);
  resetPreferredColumn(state);
  const chars = segmentGraphemes(state.input);
  const cursor = getInputCursor(state);
  if (cursor >= chars.length) return false;
  chars.splice(cursor, 1);
  state.input = chars.join("");
  state.inputCursor = cursor;
  return true;
}

export function moveInputCursor(state, direction) {
  resetPreferredColumn(state);
  const cursor = getInputCursor(state);
  if (direction === "left") state.inputCursor = clampCursor(state.input, cursor - 1);
  if (direction === "right") state.inputCursor = clampCursor(state.input, cursor + 1);
  if (direction === "home") state.inputCursor = 0;
  if (direction === "end") state.inputCursor = segmentGraphemes(state.input).length;
}

export function insertInputNewline(state) {
  insertInputText(state, "\n");
}

export function getInputCursorLocation(state) {
  const chars = segmentGraphemes(state.input);
  const before = chars.slice(0, getInputCursor(state));
  let line = 0;
  let column = 0;
  for (const char of before) {
    if (char === "\n") {
      line += 1;
      column = 0;
    } else {
      column += 1;
    }
  }
  return { line, column };
}

export function moveInputCursorVertical(state, direction) {
  if (direction !== "up" && direction !== "down") return false;
  const lines = String(state.input ?? "").split("\n").map(segmentGraphemes);
  const location = getInputCursorLocation(state);
  const targetLine = location.line + (direction === "up" ? -1 : 1);
  if (targetLine < 0 || targetLine >= lines.length) return false;

  const preferredColumn = Number.isFinite(state.inputPreferredColumn)
    ? state.inputPreferredColumn
    : location.column;
  const targetColumn = Math.min(preferredColumn, lines[targetLine].length);
  state.inputPreferredColumn = preferredColumn;
  state.inputCursor = inputLineOffset(lines, targetLine) + targetColumn;
  return true;
}

export function moveInputCursorToLineBoundary(state, boundary) {
  const lines = String(state.input ?? "").split("\n").map(segmentGraphemes);
  const location = getInputCursorLocation(state);
  const line = lines[location.line] ?? [];
  const column = boundary === "end" ? line.length : 0;
  state.inputCursor = inputLineOffset(lines, location.line) + column;
  resetPreferredColumn(state);
}

export function renderInputWithCursor(state) {
  const chars = segmentGraphemes(state.input);
  const cursor = getInputCursor(state);
  return `${chars.slice(0, cursor).join("")}\u258C${chars.slice(cursor).join("")}`;
}

export function renderInputLinesWithCursor(state, availableWidth, maxLines = 6) {
  const width = Math.max(1, Number(availableWidth) || 1);
  const limit = Math.max(1, Number(maxLines) || 1);
  const location = getInputCursorLocation(state);
  const logicalLines = String(state.input ?? "").split("\n");
  const rendered = [];

  for (const [lineIndex, line] of logicalLines.entries()) {
    const tokens = segmentGraphemes(line);
    if (lineIndex === location.line) {
      tokens.splice(location.column, 0, "\u258C");
    }
    const wrapped = wrapComposerTokens(tokens, width);
    rendered.push(...wrapped.map((text) => ({
      text,
      hasCursor: text.includes("\u258C"),
    })));
  }

  const cursorRow = Math.max(0, rendered.findIndex((row) => row.hasCursor));
  const maxStart = Math.max(0, rendered.length - limit);
  const start = Math.max(0, Math.min(cursorRow - Math.floor(limit / 2), maxStart));
  return rendered.slice(start, start + limit).map((row) => row.text);
}

export function truncateInputText(text, maxGraphemes = 200_000) {
  const limit = Math.max(0, Number(maxGraphemes) || 0);
  return segmentGraphemes(text).slice(0, limit).join("");
}

export function rememberSubmittedInput(state, text, { maxEntries = 100 } = {}) {
  const value = String(text ?? "").trim();
  if (!value) return;
  const history = Array.isArray(state.inputHistory) ? state.inputHistory : [];
  if (history.at(-1) !== value) {
    history.push(value);
  }
  state.inputHistory = history.slice(-maxEntries);
  resetInputHistoryNavigation(state);
}

export function navigateInputHistory(state, direction) {
  const history = Array.isArray(state.inputHistory) ? state.inputHistory : [];
  if (!history.length) return false;

  if (direction === "up") {
    if (state.inputHistoryCursor == null) {
      state.inputHistoryDraft = state.input ?? "";
      state.inputHistoryCursor = history.length - 1;
    } else {
      state.inputHistoryCursor = Math.max(0, state.inputHistoryCursor - 1);
    }
    setInputText(state, history[state.inputHistoryCursor] ?? "");
    return true;
  }

  if (direction === "down" && state.inputHistoryCursor != null) {
    if (state.inputHistoryCursor < history.length - 1) {
      state.inputHistoryCursor += 1;
      setInputText(state, history[state.inputHistoryCursor] ?? "");
    } else {
      const draft = state.inputHistoryDraft ?? "";
      resetInputHistoryNavigation(state);
      setInputText(state, draft);
    }
    return true;
  }

  return false;
}

export function resetInputHistoryNavigation(state) {
  state.inputHistoryCursor = null;
  state.inputHistoryDraft = "";
}

function inputLineOffset(lines, lineIndex) {
  return lines
    .slice(0, lineIndex)
    .reduce((total, line) => total + line.length + 1, 0);
}

function resetPreferredColumn(state) {
  state.inputPreferredColumn = null;
}

function clampCursor(text, cursor) {
  const length = segmentGraphemes(text).length;
  const value = Number.isFinite(cursor) ? Number(cursor) : length;
  return Math.max(0, Math.min(length, value));
}

function segmentGraphemes(text) {
  return Array.from(GRAPHEME_SEGMENTER.segment(String(text ?? "")), (entry) => entry.segment);
}

function wrapComposerTokens(tokens, width) {
  if (!tokens.length) return [""];
  const lines = [];
  let current = "";
  let currentWidth = 0;
  for (const token of tokens) {
    const tokenWidth = charWidth(token);
    if (current && currentWidth + tokenWidth > width) {
      lines.push(current);
      current = "";
      currentWidth = 0;
    }
    current += token;
    currentWidth += tokenWidth;
  }
  lines.push(current);
  return lines;
}

function isControlInput(char) {
  if (!char) return false;
  const code = char.charCodeAt(0);
  return code < 32 || code === 127;
}

function extractTrailingIncompleteEscape(text) {
  const value = String(text ?? "");
  const escIndex = value.lastIndexOf("\x1b");
  if (escIndex < 0) return "";
  const suffix = value.slice(escIndex);
  if (suffix.length > MAX_PENDING_ESCAPE_CHARS) return "";
  if (suffix === "\x1b" || suffix === "\x1b[" || suffix === "\x1bO" || suffix === "\x1bo") {
    return suffix;
  }
  if (suffix.startsWith("\x1b[") && !CSI_PATTERN.test(suffix)) {
    return suffix;
  }
  if ((suffix.startsWith("\x1bO") || suffix.startsWith("\x1bo")) && !SS3_PATTERN.test(suffix)) {
    return suffix;
  }
  return "";
}

function normalizeSs3Key(value) {
  if (!value || value.length < 3) return value;
  return `\x1bO${value.slice(2).toUpperCase()}`;
}

function longestSuffixPrefix(text, marker) {
  for (
    let length = Math.min(text.length, marker.length - 1);
    length > 0;
    length -= 1
  ) {
    if (text.endsWith(marker.slice(0, length))) return length;
  }
  return 0;
}

function normalizeModifiedEnter(key) {
  if (key === "\x1b[27;2;13~") return INPUT_KEYS.shiftEnter;
  if (key === "\x1b[27;5;13~") return INPUT_KEYS.ctrlEnter;
  return key;
}
