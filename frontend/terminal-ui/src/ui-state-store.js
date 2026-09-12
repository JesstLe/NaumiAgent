import fs from "node:fs";
import path from "node:path";
import { randomBytes } from "node:crypto";

const STORE_VERSION = 6;
const LEGACY_STORE_VERSION = 1;
const COMPOSER_STORE_VERSION = 2;
const HISTORY_STORE_VERSION = 3;
const INSPECTOR_STORE_VERSION = 4;
const AGENT_STORE_VERSION = 5;
const STORE_PATH = path.join(".naumi", "terminal-ui-state.json");
const DEFAULT_SESSION_KEY = "__default__";
const MAX_HISTORY_ENTRIES = 100;
const MAX_HISTORY_ENTRY_CHARS = 200_000;
const MAX_HISTORY_TOTAL_CHARS = 1_000_000;
const LOCK_WAIT_MS = 10;
const LOCK_TIMEOUT_MS = 2_000;
const LOCK_STALE_MS = 30_000;
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
        terminalEventClientId: sanitizeTerminalEventClientId(
          parsed.terminal_event_client_id,
        ),
        baseInputHistory: sanitizeInputHistory(parsed.input_history),
        dirtySessionKeys: new Set(),
        inputHistoryDirty: false,
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
        terminalEventClientId: newTerminalEventClientId(),
        baseInputHistory: [],
        dirtySessionKeys: new Set(Object.keys(parsed.sessions)),
        inputHistoryDirty: false,
        writable: true,
      };
    }
    if (parsed.version === COMPOSER_STORE_VERSION) {
      return {
        filePath,
        sessions: migrateAgentSessions(migrateInspectorSessions(parsed.sessions)),
        inputHistory: [],
        terminalEventClientId: newTerminalEventClientId(),
        baseInputHistory: [],
        dirtySessionKeys: new Set(Object.keys(parsed.sessions)),
        inputHistoryDirty: false,
        writable: true,
      };
    }
    if (parsed.version === HISTORY_STORE_VERSION) {
      return {
        filePath,
        sessions: migrateAgentSessions(migrateInspectorSessions(parsed.sessions)),
        inputHistory: sanitizeInputHistory(parsed.input_history),
        terminalEventClientId: newTerminalEventClientId(),
        baseInputHistory: sanitizeInputHistory(parsed.input_history),
        dirtySessionKeys: new Set(Object.keys(parsed.sessions)),
        inputHistoryDirty: false,
        writable: true,
      };
    }
    if (parsed.version === INSPECTOR_STORE_VERSION) {
      return {
        filePath,
        sessions: migrateAgentSessions(parsed.sessions),
        inputHistory: sanitizeInputHistory(parsed.input_history),
        terminalEventClientId: newTerminalEventClientId(),
        baseInputHistory: sanitizeInputHistory(parsed.input_history),
        dirtySessionKeys: new Set(Object.keys(parsed.sessions)),
        inputHistoryDirty: false,
        writable: true,
      };
    }
    if (parsed.version === AGENT_STORE_VERSION) {
      return {
        filePath,
        sessions: parsed.sessions,
        inputHistory: sanitizeInputHistory(parsed.input_history),
        terminalEventClientId: newTerminalEventClientId(),
        baseInputHistory: sanitizeInputHistory(parsed.input_history),
        dirtySessionKeys: new Set(Object.keys(parsed.sessions)),
        inputHistoryDirty: false,
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
  let releaseLock = null;
  let tmpPath = "";
  try {
    fs.mkdirSync(path.dirname(store.filePath), { recursive: true });
    if (fs.existsSync(store.filePath) && fs.statSync(store.filePath).isDirectory()) return false;
    releaseLock = acquireUiStateLock(store.filePath);
    const persisted = readCurrentStore(store.filePath);
    if (persisted?.futureVersion) return false;
    const sessions = { ...(persisted?.sessions ?? {}) };
    const dirtyKeys = store.dirtySessionKeys instanceof Set
      ? store.dirtySessionKeys
      : new Set(Object.keys(store.sessions ?? {}));
    for (const key of dirtyKeys) {
      if (Object.hasOwn(store.sessions ?? {}, key)) sessions[key] = store.sessions[key];
    }
    const inputHistory = store.inputHistoryDirty
      ? mergeInputHistory(
        persisted?.inputHistory ?? [],
        store.baseInputHistory ?? [],
        store.inputHistory ?? [],
      )
      : (persisted?.inputHistory ?? sanitizeInputHistory(store.inputHistory));
    tmpPath = temporaryStatePath(store.filePath);
    const terminalEventClientId = sanitizeTerminalEventClientId(
      persisted?.terminalEventClientId ?? store.terminalEventClientId,
    );
    fs.writeFileSync(tmpPath, JSON.stringify({
      version: STORE_VERSION,
      sessions,
      input_history: inputHistory,
      terminal_event_client_id: terminalEventClientId,
    }, null, 2), "utf8");
    replaceUiStateFile(tmpPath, store.filePath);
    store.sessions = sessions;
    store.inputHistory = inputHistory;
    store.baseInputHistory = [...inputHistory];
    store.terminalEventClientId = terminalEventClientId;
    store.dirtySessionKeys = new Set();
    store.inputHistoryDirty = false;
    return true;
  } catch {
    return false;
  } finally {
    if (tmpPath) removeFileQuietly(fs, tmpPath);
    releaseLock?.();
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
  const key = sessionKey(sessionId);
  store.sessions[key] = {
    version: STORE_VERSION,
    updated_at: new Date().toISOString(),
    ...snapshot,
  };
  if (!(store.dirtySessionKeys instanceof Set)) store.dirtySessionKeys = new Set();
  store.dirtySessionKeys.add(key);
}

export function getProjectInputHistory(store) {
  return [...sanitizeInputHistory(store.inputHistory)];
}

export function setProjectInputHistory(store, history) {
  store.inputHistory = sanitizeInputHistory(history);
  store.inputHistoryDirty = true;
}

export function sessionKey(sessionId) {
  return sessionId || DEFAULT_SESSION_KEY;
}

function createEmptyStore(filePath, { writable = true } = {}) {
  return {
    filePath,
    sessions: {},
    inputHistory: [],
    baseInputHistory: [],
    terminalEventClientId: newTerminalEventClientId(),
    dirtySessionKeys: new Set(),
    inputHistoryDirty: false,
    writable,
  };
}

function readCurrentStore(filePath) {
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
    if (!parsed || typeof parsed !== "object") return null;
    if (Number(parsed.version) > STORE_VERSION) return { futureVersion: true };
    if (!parsed.sessions || typeof parsed.sessions !== "object" || Array.isArray(parsed.sessions)) {
      return null;
    }
    return {
      sessions: parsed.sessions,
      inputHistory: sanitizeInputHistory(parsed.input_history),
      terminalEventClientId: sanitizeTerminalEventClientId(parsed.terminal_event_client_id),
    };
  } catch {
    return null;
  }
}

function acquireUiStateLock(filePath) {
  const lockPath = `${filePath}.lock`;
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  while (true) {
    try {
      fs.mkdirSync(lockPath);
      fs.writeFileSync(path.join(lockPath, "owner"), `${process.pid}\n${Date.now()}\n`, "utf8");
      return () => removeDirectoryQuietly(lockPath);
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      removeStaleLock(lockPath);
      if (Date.now() >= deadline) throw new Error("终端 UI 状态文件正被其他会话写入");
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, LOCK_WAIT_MS);
    }
  }
}

function removeStaleLock(lockPath) {
  try {
    const age = Date.now() - fs.statSync(lockPath).mtimeMs;
    if (age > LOCK_STALE_MS) fs.rmSync(lockPath, { recursive: true, force: true });
  } catch {
    // Another writer may release the lock between stat and removal.
  }
}

function removeDirectoryQuietly(directoryPath) {
  try {
    fs.rmSync(directoryPath, { recursive: true, force: true });
  } catch {
    // Lock cleanup is best effort; stale locks are recovered on the next save.
  }
}

function mergeInputHistory(persisted, base, local) {
  const safePersisted = sanitizeInputHistory(persisted);
  const safeBase = sanitizeInputHistory(base);
  const safeLocal = sanitizeInputHistory(local);
  let prefix = 0;
  while (
    prefix < safeBase.length
    && prefix < safeLocal.length
    && safeBase[prefix] === safeLocal[prefix]
  ) {
    prefix += 1;
  }
  return sanitizeInputHistory([...safePersisted, ...safeLocal.slice(prefix)]);
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

function sanitizeTerminalEventClientId(value) {
  const normalized = String(value ?? "");
  return /^tecli_[0-9a-f]{24}$/.test(normalized)
    ? normalized
    : newTerminalEventClientId();
}

function newTerminalEventClientId() {
  return `tecli_${randomBytes(12).toString("hex")}`;
}
