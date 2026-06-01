import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createDebugLog, sanitize } from "../src/debug-log.js";

test("debug log writes structured jsonl records to a configured path", () => {
  const filePath = path.join(os.tmpdir(), `naumi-terminal-debug-${Date.now()}-${Math.random()}.jsonl`);
  const logger = createDebugLog({
    cwd: os.tmpdir(),
    env: { NAUMI_TERMINAL_UI_DEBUG_LOG: filePath },
  });

  logger.log("test.event", { value: "ok" });
  logger.close();

  const records = fs.readFileSync(filePath, "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.equal(records[0].event, "terminal_ui.start");
  assert.equal(records[1].event, "test.event");
  assert.equal(records[1].payload.value, "ok");
});

test("debug log can be disabled and truncates large strings", () => {
  assert.equal(createDebugLog({ env: { NAUMI_TERMINAL_UI_DEBUG_LOG: "0" } }), null);
  const value = sanitize({ text: "x".repeat(21000) });
  assert(value.text.includes("[truncated"));
  assert(value.text.length < 20200);
});
