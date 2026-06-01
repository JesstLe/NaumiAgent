import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { attachJsonlLineReader, createEventSender, parseArgs, splitShellLike } from "../src/protocol.js";

test("parseArgs supports config and bridge command", () => {
  assert.deepEqual(parseArgs(["--config", "local.yaml", "--bridge-command", "node fake.js"]), {
    config: "local.yaml",
    bridgeCommand: "node fake.js",
  });
});

test("splitShellLike keeps quoted arguments together", () => {
  assert.deepEqual(splitShellLike('node "fake bridge.js" --flag'), ["node", "fake bridge.js", "--flag"]);
});

test("event sender writes versioned JSONL records", () => {
  const chunks = [];
  const writable = { write: (chunk) => chunks.push(chunk) };
  const send = createEventSender(writable);

  const id = send("submit", { text: "hi" });

  assert.equal(id, "ui-1");
  assert.deepEqual(JSON.parse(chunks[0]), {
    id: "ui-1",
    type: "submit",
    version: 1,
    payload: { text: "hi" },
  });
});

test("jsonl reader emits complete lines across chunk boundaries", () => {
  const stream = new EventEmitter();
  const lines = [];
  attachJsonlLineReader(stream, (line) => lines.push(line));

  stream.emit("data", Buffer.from('{"a":'));
  stream.emit("data", Buffer.from("1}\n{\"b\":2}\r\n"));

  assert.deepEqual(lines, ['{"a":1}', '{"b":2}']);
});
