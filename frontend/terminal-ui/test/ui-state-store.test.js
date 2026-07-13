import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  getProjectInputHistory,
  getUiSnapshot,
  loadUiStateStore,
  saveUiStateStore,
  setProjectInputHistory,
  setUiSnapshot,
} from "../src/ui-state-store.js";

test("ui state store saves session-scoped snapshots atomically", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "naumi-ui-state-"));
  const filePath = path.join(dir, "state.json");
  const previous = process.env.NAUMI_TERMINAL_UI_STATE_PATH;
  process.env.NAUMI_TERMINAL_UI_STATE_PATH = filePath;

  try {
    const store = loadUiStateStore(process.cwd());
    setUiSnapshot(store, "session-a", {
      folds: { "tool:call-1": { expanded: true } },
      foldCursor: 1,
      scrollOffset: 7,
    });
    setProjectInputHistory(store, ["第一条", "第二条"]);
    saveUiStateStore(store);

    const reloaded = loadUiStateStore(process.cwd());
    assert.deepEqual(getUiSnapshot(reloaded, "session-a").folds, {
      "tool:call-1": { expanded: true },
    });
    assert.equal(getUiSnapshot(reloaded, "session-a").scrollOffset, 7);
    assert.equal(getUiSnapshot(reloaded, "missing"), null);
    assert.deepEqual(getProjectInputHistory(reloaded), ["第一条", "第二条"]);
  } finally {
    if (previous === undefined) {
      delete process.env.NAUMI_TERMINAL_UI_STATE_PATH;
    } else {
      process.env.NAUMI_TERMINAL_UI_STATE_PATH = previous;
    }
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("ui state store migrates version one sessions with an empty composer", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "naumi-ui-state-v1-"));
  const filePath = path.join(dir, "state.json");
  const previous = process.env.NAUMI_TERMINAL_UI_STATE_PATH;
  process.env.NAUMI_TERMINAL_UI_STATE_PATH = filePath;
  fs.writeFileSync(filePath, JSON.stringify({
    version: 1,
    sessions: {
      abc: { folds: {}, foldCursor: 1, scrollOffset: 4 },
    },
  }), "utf8");

  try {
    const store = loadUiStateStore(process.cwd());
    assert.equal(getUiSnapshot(store, "abc").scrollOffset, 4);
    assert.deepEqual(getUiSnapshot(store, "abc").composer, {
      text: "",
      cursor: 0,
      preferredColumn: null,
    });
    saveUiStateStore(store);
    assert.deepEqual(getProjectInputHistory(store), []);
    assert.equal(JSON.parse(fs.readFileSync(filePath, "utf8")).version, 3);
  } finally {
    if (previous === undefined) delete process.env.NAUMI_TERMINAL_UI_STATE_PATH;
    else process.env.NAUMI_TERMINAL_UI_STATE_PATH = previous;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("ui state store migrates version two without losing session drafts", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "naumi-ui-state-v2-"));
  const filePath = path.join(dir, "state.json");
  const previous = process.env.NAUMI_TERMINAL_UI_STATE_PATH;
  process.env.NAUMI_TERMINAL_UI_STATE_PATH = filePath;
  fs.writeFileSync(filePath, JSON.stringify({
    version: 2,
    sessions: {
      abc: { composer: { text: "保留草稿", cursor: 4, preferredColumn: null } },
    },
  }), "utf8");

  try {
    const store = loadUiStateStore(process.cwd());
    assert.equal(getUiSnapshot(store, "abc").composer.text, "保留草稿");
    assert.deepEqual(getProjectInputHistory(store), []);
    saveUiStateStore(store);
    assert.equal(JSON.parse(fs.readFileSync(filePath, "utf8")).version, 3);
  } finally {
    if (previous === undefined) delete process.env.NAUMI_TERMINAL_UI_STATE_PATH;
    else process.env.NAUMI_TERMINAL_UI_STATE_PATH = previous;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("project history filters malformed values and keeps bounded newest entries", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "naumi-ui-history-bounds-"));
  const filePath = path.join(dir, "state.json");
  const previous = process.env.NAUMI_TERMINAL_UI_STATE_PATH;
  process.env.NAUMI_TERMINAL_UI_STATE_PATH = filePath;
  fs.writeFileSync(filePath, JSON.stringify({
    version: 3,
    sessions: {},
    input_history: [null, "", ...Array.from({ length: 105 }, (_, index) => `entry-${index}`)],
  }), "utf8");

  try {
    const store = loadUiStateStore(process.cwd());
    const history = getProjectInputHistory(store);
    assert.equal(history.length, 100);
    assert.equal(history[0], "entry-5");
    assert.equal(history.at(-1), "entry-104");

    setProjectInputHistory(store, ["kept", "x".repeat(2_000_001), 42, "newest"]);
    assert.deepEqual(getProjectInputHistory(store), ["kept", "newest"]);
  } finally {
    if (previous === undefined) delete process.env.NAUMI_TERMINAL_UI_STATE_PATH;
    else process.env.NAUMI_TERMINAL_UI_STATE_PATH = previous;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("ui state store never overwrites an unknown future version", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "naumi-ui-state-future-"));
  const filePath = path.join(dir, "state.json");
  const previous = process.env.NAUMI_TERMINAL_UI_STATE_PATH;
  process.env.NAUMI_TERMINAL_UI_STATE_PATH = filePath;
  const future = JSON.stringify({ version: 99, sessions: { future: { value: true } } });
  fs.writeFileSync(filePath, future, "utf8");

  try {
    const store = loadUiStateStore(process.cwd());
    setUiSnapshot(store, "current", { scrollOffset: 1 });
    assert.equal(saveUiStateStore(store), false);
    assert.equal(fs.readFileSync(filePath, "utf8"), future);
  } finally {
    if (previous === undefined) delete process.env.NAUMI_TERMINAL_UI_STATE_PATH;
    else process.env.NAUMI_TERMINAL_UI_STATE_PATH = previous;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
