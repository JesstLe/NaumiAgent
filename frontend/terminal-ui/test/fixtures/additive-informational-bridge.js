#!/usr/bin/env node
import process from "node:process";
import {
  attachJsonlLineReader,
  PROTOCOL_CONTRACT,
  PROTOCOL_REGISTRY_SHA256,
} from "../../src/protocol.js";

let sequence = 1;
const futureRegistrySha256 = "f".repeat(64);

attachJsonlLineReader(process.stdin, (line) => {
  if (!line.trim()) return;
  const record = JSON.parse(line);

  if (record.type === "hello") {
    emit("ack", {
      event: "hello",
      negotiation: {
        selected_version: 1,
        server_minimum_version: 1,
        server_maximum_version: 1,
        capabilities: PROTOCOL_CONTRACT.negotiation.capabilities,
      },
    }, record.id);
    emit("ready", {
      version: "future-compatible",
      mode: "default",
      permission_mode: "moderate",
      model: "future/additive-compatible",
      workspace_root: "/tmp/additive-compatible",
      protocol_registry: {
        contract_version: 1,
        registry_sha256: futureRegistrySha256,
        compatible_registry_sha256: [
          futureRegistrySha256,
          PROTOCOL_REGISTRY_SHA256,
        ],
        client_event_count: PROTOCOL_CONTRACT.client_events.length,
        server_event_count: PROTOCOL_CONTRACT.server_events.length + 1,
      },
    });
    emit("future/progress", {
      secret: "unknown-payload-must-never-enter-debug-log",
      arbitrary: { private: true },
    }, "", "informational");
    emit("runtime/status", {
      model: "future/additive-status-confirmed",
    });
    return;
  }

  if (record.type === "shutdown") {
    emit("shutdown", { ok: true });
    setTimeout(() => process.exit(0), 5);
  }
});

function emit(type, payload, requestId = "", criticality = "") {
  process.stdout.write(`${JSON.stringify({
    type,
    version: 1,
    seq: sequence++,
    ...(requestId ? { request_id: requestId } : {}),
    ...(criticality ? { criticality } : {}),
    payload,
  })}\n`);
}
