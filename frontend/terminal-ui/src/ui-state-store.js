import fs from "node:fs";
import path from "node:path";

const STORE_VERSION = 3;
const LEGACY_STORE_VERSION = 1;
const COMPOSER_STORE_VERSION = 2;
const STORE_PATH = path.join(".naumi", "terminal-ui-state.json");
const DEFAULT_SESSION_KEY = "__default__";
const MAX_HISTORY_ENTRIES = 100;
const MAX_HISTORY_ENTRY_CHARS = 200_000;
const MAX_HISTORY_TOTAL_CHARS = 1_000_000;

export function loadUiStateStore(cwd) {
  const filePath = process.env.NAUMI_TERMINAL_UI_STATE_PATH || path.join(cwd, STORE_PATH);
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.sessions !== "object" || Array.isArray(parsed.sessions)) {
      return createEmptyStore(filePath);
    }
    if (parsed.version === STORE_VERSION) {
      return {
        filePath,
        sessions: parsed.sessions,
        inputHistory: sanitizeInputHistory(parsed.input_history),
        writable: true,
      };
    }
    if (parsed.version === LEGACY_STORE_VERSION) {
      return {
        filePath,
        sessions: Object.fromEntries(
          Object.entries(parsed.sessions).map(([key, snapshot]) => [
            key,
            migrateLegacySnapshot(snapshot),
          ]),
        ),
        inputHistory: [],
        writable: true,
      };
    }
    if (parsed.version === COMPOSER_STORE_VERSION) {
      return {
        filePath,
        sessions: parsed.sessions,
        inputHistory: [],
        writable: true,
      };
    }
    if (Number(parsed.version) > STORE_VERSION) {
      return createEmptyStore(filePath, { writable: false });
    }
    return createEmptyStore(filePath);
  } catch (error) {
    if (error?.code !== "ENOENT") {
      // Ignore corrupt local UI state; the debug trace has backend evidence.
    }
    return createEmptyStore(filePath);
  }
}

export function saveUiStateStore(store) {
  if (store.writable === false) return false;
  fs.mkdirSync(path.dirname(store.filePath), { recursive: true });
  const tmpPath = `${store.filePath}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify({
    version: STORE_VERSION,
    sessions: store.sessions,
    input_history: sanitizeInputHistory(store.inputHistory),
  }, null, 2), "utf8");
  fs.renameSync(tmpPath, store.filePath);
  return true;
}

export function getUiSnapshot(store, sessionId) {
  return store.sessions[sessionKey(sessionId)] ?? null;
}

export function setUiSnapshot(store, sessionId, snapshot) {
  store.sessions[sessionKey(sessionId)] = {
    version: STORE_VERSION,
    updated_at: new Date().toISOString(),
    ...snapshot,
  };
}

export function getProjectInputHistory(store) {
  return [...sanitizeInputHistory(store.inputHistory)];
}

export function setProjectInputHistory(store, history) {
  store.inputHistory = sanitizeInputHistory(history);
}

export function sessionKey(sessionId) {
  return sessionId || DEFAULT_SESSION_KEY;
}

function createEmptyStore(filePath, { writable = true } = {}) {
  return { filePath, sessions: {}, inputHistory: [], writable };
}

function migrateLegacySnapshot(snapshot) {
  const safe = snapshot && typeof snapshot === "object" ? snapshot : {};
  return {
    ...safe,
    composer: {
      text: "",
      cursor: 0,
      preferredColumn: null,
    },
  };
}

function sanitizeInputHistory(history) {
  if (!Array.isArray(history)) return [];
  const keptNewestFirst = [];
  let totalChars = 0;
  for (const value of [...history].reverse()) {
    if (keptNewestFirst.length >= MAX_HISTORY_ENTRIES) break;
    if (typeof value !== "string" || !value || value.length > MAX_HISTORY_ENTRY_CHARS) continue;
    if (totalChars + value.length > MAX_HISTORY_TOTAL_CHARS) continue;
    keptNewestFirst.push(value);
    totalChars += value.length;
  }
  return keptNewestFirst.reverse();
}
