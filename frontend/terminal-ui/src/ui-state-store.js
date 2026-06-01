import fs from "node:fs";
import path from "node:path";

const STORE_VERSION = 1;
const STORE_PATH = path.join(".naumi", "terminal-ui-state.json");
const DEFAULT_SESSION_KEY = "__default__";

export function loadUiStateStore(cwd) {
  const filePath = process.env.NAUMI_TERMINAL_UI_STATE_PATH || path.join(cwd, STORE_PATH);
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw);
    if (parsed?.version !== STORE_VERSION || typeof parsed.sessions !== "object") {
      return createEmptyStore(filePath);
    }
    return { filePath, sessions: parsed.sessions };
  } catch (error) {
    if (error?.code !== "ENOENT") {
      // Ignore corrupt local UI state; the debug trace has backend evidence.
    }
    return createEmptyStore(filePath);
  }
}

export function saveUiStateStore(store) {
  fs.mkdirSync(path.dirname(store.filePath), { recursive: true });
  const tmpPath = `${store.filePath}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify({ version: STORE_VERSION, sessions: store.sessions }, null, 2), "utf8");
  fs.renameSync(tmpPath, store.filePath);
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

export function sessionKey(sessionId) {
  return sessionId || DEFAULT_SESSION_KEY;
}

function createEmptyStore(filePath) {
  return { filePath, sessions: {} };
}
