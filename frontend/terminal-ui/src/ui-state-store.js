import fs from "node:fs";
import path from "node:path";

const STORE_VERSION = 2;
const LEGACY_STORE_VERSION = 1;
const STORE_PATH = path.join(".naumi", "terminal-ui-state.json");
const DEFAULT_SESSION_KEY = "__default__";

export function loadUiStateStore(cwd) {
  const filePath = process.env.NAUMI_TERMINAL_UI_STATE_PATH || path.join(cwd, STORE_PATH);
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.sessions !== "object" || Array.isArray(parsed.sessions)) {
      return createEmptyStore(filePath);
    }
    if (parsed.version === STORE_VERSION) {
      return { filePath, sessions: parsed.sessions, writable: true };
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
  fs.writeFileSync(tmpPath, JSON.stringify({ version: STORE_VERSION, sessions: store.sessions }, null, 2), "utf8");
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

export function sessionKey(sessionId) {
  return sessionId || DEFAULT_SESSION_KEY;
}

function createEmptyStore(filePath, { writable = true } = {}) {
  return { filePath, sessions: {}, writable };
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
