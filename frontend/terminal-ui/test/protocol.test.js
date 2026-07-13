import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import {
  attachJsonlLineReader,
  createEventSender,
  normalizeServerRecord,
  parseArgs,
  parseBridgeCommandJson,
  PROTOCOL_CONTRACT,
  PROTOCOL_VERSION,
  splitShellLike,
} from "../src/protocol.js";

test("parseArgs supports config and bridge command", () => {
  assert.deepEqual(parseArgs([
    "--config",
    "local.yaml",
    "--bridge-command",
    "node fake.js",
    "--bridge-command-json",
    "[\"node\",\"fake.js\"]",
  ]), {
    config: "local.yaml",
    bridgeCommand: "node fake.js",
    bridgeCommandJson: "[\"node\",\"fake.js\"]",
  });
});

test("parseBridgeCommandJson decodes argv without shell splitting", () => {
  assert.deepEqual(
    parseBridgeCommandJson("[\"/path with spaces/python\",\"-m\",\"naumi_agent.ui.bridge\"]"),
    ["/path with spaces/python", "-m", "naumi_agent.ui.bridge"],
  );

  assert.throws(
    () => parseBridgeCommandJson("[\"python\",42]"),
    /必须是非空字符串数组/,
  );
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

test("event sender accepts a caller supplied request id", () => {
  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });

  assert.equal(
    send("submit", { text: "修复测试" }, { id: "submit-local-1" }),
    "submit-local-1",
  );
  assert.equal(JSON.parse(chunks[0]).id, "submit-local-1");
  assert.equal(send("ping", {}), "ui-1");
});

test("protocol contract drives client and server event validation", () => {
  assert.equal(PROTOCOL_VERSION, PROTOCOL_CONTRACT.version);
  assert(PROTOCOL_CONTRACT.client_events.includes("submit"));
  assert(PROTOCOL_CONTRACT.client_events.includes("task_panel"));
  assert(PROTOCOL_CONTRACT.server_events.includes("ui/message"));
  assert(PROTOCOL_CONTRACT.server_events.includes("runtime/status"));
  assert.deepEqual(PROTOCOL_CONTRACT.ui_messages.tool_prepare.phases, ["start", "snapshot", "end"]);
  assert(PROTOCOL_CONTRACT.ui_messages.tool_prepare.fields.includes("tool_call_id"));
  assert(PROTOCOL_CONTRACT.ui_messages.tool_prepare.fields.includes("content_lines"));
  assert(PROTOCOL_CONTRACT.ui_messages.tool_prepare.fields.includes("elapsed_ms"));
  assert(PROTOCOL_CONTRACT.ui_messages.tool_use.fields.includes("tool_call_id"));

  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });

  assert.throws(
    () => send("not_a_real_event", {}),
    /未知客户端事件/,
  );
  assert.equal(chunks.length, 0);
});

test("normalizeServerRecord stabilizes bridge payloads", () => {
  assert.deepEqual(normalizeServerRecord({
    id: 42,
    seq: "7",
    type: "user/message",
    version: "1",
    payload: { content: 123 },
  }), {
    id: "42",
    seq: 7,
    type: "user/message",
    version: 1,
    payload: { content: "123" },
  });

  assert.deepEqual(normalizeServerRecord({
    type: "session/replayed",
    payload: { session_id: 100, title: null, message_count: "4", clear: "false" },
  }).payload, {
    session_id: "100",
    title: "",
    message_count: 4,
    clear: false,
  });

  assert.deepEqual(normalizeServerRecord({
    type: "permission/resolved",
    payload: { request_id: 99, choice: "BYPASS" },
  }).payload, {
    request_id: "99",
    choice: "bypass",
  });
});

test("normalizeServerRecord rejects invalid bridge records", () => {
  assert.throws(
    () => normalizeServerRecord({ type: "surprise", payload: {} }),
    /未知 Bridge 事件/,
  );
  assert.throws(
    () => normalizeServerRecord({ type: "ready", version: 99, payload: {} }),
    /协议版本不兼容/,
  );
  assert.throws(
    () => normalizeServerRecord({ type: "ready", payload: [] }),
    /payload 必须是对象/,
  );
  assert.throws(
    () => normalizeServerRecord({ type: "ui/message", payload: {} }),
    /缺少 type/,
  );
});

test("normalizes workbench snapshot events", () => {
  const record = normalizeServerRecord({
    type: "workbench/snapshot",
    version: 1,
    payload: {
      session_id: "s",
      missions: [{ id: "m1", title: "Mac 工作台" }],
      issues: [],
      tasks: [],
      failures: [],
      events: [],
    },
  });

  assert.equal(record.payload.session_id, "s");
  assert.equal(record.payload.missions[0].title, "Mac 工作台");
});

test("normalizes workbench event payloads", () => {
  const record = normalizeServerRecord({
    type: "workbench/event",
    version: 1,
    payload: {
      id: "evt-1",
      type: "issue.claimed",
      actor: "Backend-Agent",
      subject_id: "1",
      payload: { lease_id: "lease-1" },
      timestamp: "2026-06-27T10:00:00",
    },
  });

  assert.equal(record.payload.id, "evt-1");
  assert.equal(record.payload.type, "issue.claimed");
  assert.equal(record.payload.actor, "Backend-Agent");
  assert.equal(record.payload.subject_id, "1");
  assert.equal(record.payload.payload.lease_id, "lease-1");
  assert.equal(record.payload.timestamp, "2026-06-27T10:00:00");
});

test("jsonl reader emits complete lines across chunk boundaries", () => {
  const stream = new EventEmitter();
  const lines = [];
  attachJsonlLineReader(stream, (line) => lines.push(line));

  stream.emit("data", Buffer.from('{"a":'));
  stream.emit("data", Buffer.from("1}\n{\"b\":2}\r\n"));

  assert.deepEqual(lines, ['{"a":1}', '{"b":2}']);
});
