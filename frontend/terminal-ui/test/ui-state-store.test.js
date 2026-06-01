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
