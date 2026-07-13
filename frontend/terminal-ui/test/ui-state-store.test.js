import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  getUiSnapshot,
  loadUiStateStore,
  saveUiStateStore,
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
    saveUiStateStore(store);

    const reloaded = loadUiStateStore(process.cwd());
    assert.deepEqual(getUiSnapshot(reloaded, "session-a").folds, {
      "tool:call-1": { expanded: true },
    });
    assert.equal(getUiSnapshot(reloaded, "session-a").scrollOffset, 7);
    assert.equal(getUiSnapshot(reloaded, "missing"), null);
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
    assert.equal(JSON.parse(fs.readFileSync(filePath, "utf8")).version, 2);
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
