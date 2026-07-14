import fs from "node:fs";
import path from "node:path";

const STORE_VERSION = 5;
const LEGACY_STORE_VERSION = 1;
const COMPOSER_STORE_VERSION = 2;
const HISTORY_STORE_VERSION = 3;
const INSPECTOR_STORE_VERSION = 4;
const STORE_PATH = path.join(".naumi", "terminal-ui-state.json");
const DEFAULT_SESSION_KEY = "__default__";
const MAX_HISTORY_ENTRIES = 100;
const MAX_HISTORY_ENTRY_CHARS = 200_000;
const MAX_HISTORY_TOTAL_CHARS = 1_000_000;
let temporaryFileSequence = 0;

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
        sessions: migrateAgentSessions(migrateInspectorSessions(parsed.sessions)),
        inputHistory: [],
        writable: true,
      };
    }
    if (parsed.version === HISTORY_STORE_VERSION) {
      return {
        filePath,
        sessions: migrateAgentSessions(migrateInspectorSessions(parsed.sessions)),
        inputHistory: sanitizeInputHistory(parsed.input_history),
        writable: true,
      };
    }
    if (parsed.version === INSPECTOR_STORE_VERSION) {
      return {
        filePath,
        sessions: migrateAgentSessions(parsed.sessions),
        inputHistory: sanitizeInputHistory(parsed.input_history),
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
  const tmpPath = temporaryStatePath(store.filePath);
  try {
    fs.mkdirSync(path.dirname(store.filePath), { recursive: true });
    fs.writeFileSync(tmpPath, JSON.stringify({
      version: STORE_VERSION,
      sessions: store.sessions,
      input_history: sanitizeInputHistory(store.inputHistory),
    }, null, 2), "utf8");
    replaceUiStateFile(tmpPath, store.filePath);
    return true;
  } catch {
    return false;
  } finally {
    removeFileQuietly(fs, tmpPath);
  }
}

export function replaceUiStateFile(
  temporaryPath,
  destinationPath,
  { fileSystem = fs, platform = process.platform } = {},
) {
  try {
    fileSystem.renameSync(temporaryPath, destinationPath);
    return;
  } catch (error) {
    if (platform !== "win32" || !isWindowsReplacementError(error)) throw error;
  }

  const backupPath = `${temporaryPath}.replace-backup`;
  let backupCreated = false;
  try {
    fileSystem.renameSync(destinationPath, backupPath);
    backupCreated = true;
    fileSystem.renameSync(temporaryPath, destinationPath);
  } catch (error) {
    if (backupCreated) {
      try {
        fileSystem.renameSync(backupPath, destinationPath);
        backupCreated = false;
      } catch (restoreError) {
        error.cause = restoreError;
      }
    }
    throw error;
  } finally {
    if (backupCreated) removeFileQuietly(fileSystem, backupPath);
  }
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

function temporaryStatePath(filePath) {
  temporaryFileSequence = (temporaryFileSequence + 1) % Number.MAX_SAFE_INTEGER;
  return `${filePath}.${process.pid}.${Date.now()}.${temporaryFileSequence}.tmp`;
}

function isWindowsReplacementError(error) {
  return ["EACCES", "EEXIST", "EPERM"].includes(String(error?.code ?? ""));
}

function removeFileQuietly(fileSystem, filePath) {
  try {
    fileSystem.rmSync(filePath, { force: true });
  } catch {
    // A failed best-effort cleanup must not terminate the interactive UI.
  }
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
    inspector: defaultInspectorPresentation(),
    agents: defaultAgentPresentation(),
  };
}

function migrateInspectorSessions(sessions) {
  return Object.fromEntries(
    Object.entries(sessions).map(([key, snapshot]) => [
      key,
      {
        ...(snapshot && typeof snapshot === "object" ? snapshot : {}),
        inspector: defaultInspectorPresentation(),
      },
    ]),
  );
}

function migrateAgentSessions(sessions) {
  return Object.fromEntries(
    Object.entries(sessions).map(([key, snapshot]) => [
      key,
      {
        ...(snapshot && typeof snapshot === "object" ? snapshot : {}),
        agents: defaultAgentPresentation(),
      },
    ]),
  );
}

function defaultInspectorPresentation() {
  return {
    open: false,
    selectedTab: "plan",
    selectionByTab: {},
    expandedByTab: {},
    scrollByTab: {},
  };
}

function defaultAgentPresentation() {
  return {
    open: false,
    selectedTab: "agents",
    selectedByTab: {},
    detailId: "",
    scrollByTab: {},
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
