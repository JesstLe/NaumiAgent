#!/usr/bin/env node
import process from "node:process";
import { attachJsonlLineReader } from "../../src/protocol.js";

attachJsonlLineReader(process.stdin, (line) => {
  if (!line.trim()) return;
  const record = JSON.parse(line);
  if (record.type !== "hello") return;
  emit(1, "ack", {
    event: "hello",
    negotiation: {
      selected_version: 1,
      server_minimum_version: 1,
      server_maximum_version: 1,
      capabilities: ["sequence_integrity", "typed_ui_messages"],
    },
  }, record.id);
  emit(3, "ready", {
    version: "gap-record-must-not-render",
    workspace_root: "/unsafe-gap",
  });
});

function emit(seq, type, payload, requestId = "") {
  process.stdout.write(`${JSON.stringify({
    type,
    version: 1,
    id: `sequence-gap-${seq}`,
    request_id: requestId,
    seq,
    ts: new Date().toISOString(),
    payload,
  })}\n`);
}
