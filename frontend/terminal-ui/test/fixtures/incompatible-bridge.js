#!/usr/bin/env node
import process from "node:process";
import { attachJsonlLineReader } from "../../src/protocol.js";

let sequence = 0;

attachJsonlLineReader(process.stdin, (line) => {
  if (!line.trim()) return;
  const record = JSON.parse(line);
  if (record.type !== "hello") return;
  if (process.env.NAUMI_TEST_MALFORMED_ACK === "1") {
    emit("ack", {
      event: "hello",
      negotiation: {
        selected_version: 2,
        server_minimum_version: 2,
        server_maximum_version: 2,
        capabilities: ["typed_ui_messages"],
      },
    }, record.id);
    return;
  }
  emit("error", {
    code: "protocol_version_unsupported",
    message: "协议版本不兼容：客户端支持 1-1，当前测试 Bridge 支持 2-2。请升级 Naumi 或终端 UI 后重试。",
  }, record.id);
});

function emit(type, payload, requestId = "") {
  sequence += 1;
  process.stdout.write(`${JSON.stringify({
    type,
    version: 1,
    id: `incompatible-${sequence}`,
    request_id: requestId,
    seq: sequence,
    ts: new Date().toISOString(),
    payload,
  })}\n`);
}
