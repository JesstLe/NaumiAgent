export const INPUT_KEYS = {
  shiftTab: "\x1b[Z",
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

const CSI_PATTERN = /^\x1b\[[0-9;?]*[~A-Za-z]/;
const SS3_PATTERN = /^\x1bO[A-Za-z]/;

export function splitInputChunk(chunk) {
  const keys = [];
  let text = String(chunk ?? "");

  while (text) {
    if (text.startsWith("\x1bO")) {
      const match = text.match(SS3_PATTERN);
      if (match) {
        keys.push(match[0]);
        text = text.slice(match[0].length);
        continue;
      }
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
  state.inputCursor = clampCursor(state.input, cursor ?? Array.from(state.input).length);
}

export function clearInput(state) {
  setInputText(state, "");
}

export function insertInputText(state, text) {
  resetInputHistoryNavigation(state);
  const chars = Array.from(state.input ?? "");
  const insertChars = Array.from(String(text ?? ""));
  const cursor = getInputCursor(state);
  chars.splice(cursor, 0, ...insertChars);
  state.input = chars.join("");
  state.inputCursor = cursor + insertChars.length;
}

export function backspaceInput(state) {
  resetInputHistoryNavigation(state);
  const chars = Array.from(state.input ?? "");
  const cursor = getInputCursor(state);
  if (cursor <= 0) return false;
  chars.splice(cursor - 1, 1);
  state.input = chars.join("");
  state.inputCursor = cursor - 1;
  return true;
}

export function deleteInputForward(state) {
  resetInputHistoryNavigation(state);
  const chars = Array.from(state.input ?? "");
  const cursor = getInputCursor(state);
  if (cursor >= chars.length) return false;
  chars.splice(cursor, 1);
  state.input = chars.join("");
  state.inputCursor = cursor;
  return true;
}

export function moveInputCursor(state, direction) {
  const cursor = getInputCursor(state);
  if (direction === "left") state.inputCursor = clampCursor(state.input, cursor - 1);
  if (direction === "right") state.inputCursor = clampCursor(state.input, cursor + 1);
  if (direction === "home") state.inputCursor = 0;
  if (direction === "end") state.inputCursor = Array.from(state.input ?? "").length;
}

export function renderInputWithCursor(state) {
  const chars = Array.from(state.input ?? "");
  const cursor = getInputCursor(state);
  return `${chars.slice(0, cursor).join("")}\u258C${chars.slice(cursor).join("")}`;
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

function clampCursor(text, cursor) {
  const length = Array.from(text ?? "").length;
  const value = Number.isFinite(cursor) ? Number(cursor) : length;
  return Math.max(0, Math.min(length, value));
}

function isControlInput(char) {
  if (!char) return false;
  const code = char.charCodeAt(0);
  return code < 32 || code === 127;
}
