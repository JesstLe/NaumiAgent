import fs from "node:fs";
import { StringDecoder } from "node:string_decoder";

export const PROTOCOL_CONTRACT = loadProtocolContract();
export const PROTOCOL_VERSION = Number(PROTOCOL_CONTRACT.version);

const CLIENT_EVENT_TYPES = new Set(PROTOCOL_CONTRACT.client_events ?? []);
const SERVER_EVENT_TYPES = new Set(PROTOCOL_CONTRACT.server_events ?? []);

export function parseArgs(argv) {
  const parsed = { config: "config.yaml", bridgeCommand: "", bridgeCommandJson: "" };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if ((arg === "--config" || arg === "-c") && argv[i + 1]) {
      parsed.config = argv[i + 1];
      i += 1;
    } else if (arg === "--bridge-command" && argv[i + 1]) {
      parsed.bridgeCommand = argv[i + 1];
      i += 1;
    } else if (arg === "--bridge-command-json" && argv[i + 1]) {
      parsed.bridgeCommandJson = argv[i + 1];
      i += 1;
    }
  }
  return parsed;
}

export function parseBridgeCommandJson(value) {
  if (!value) return [];
  const parsed = JSON.parse(value);
  if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== "string" || item.length === 0)) {
    throw new Error("--bridge-command-json 必须是非空字符串数组");
  }
  return parsed;
}

export function splitShellLike(command) {
  return command.match(/(?:[^\s"]+|"[^"]*")+/g)?.map((part) => part.replace(/^"|"$/g, "")) ?? [];
}

export function createEventSender(writable, { debugLog = null } = {}) {
  let nextClientId = 1;
  return function send(type, payload, options = {}) {
    if (!CLIENT_EVENT_TYPES.has(type)) {
      throw new Error(`未知客户端事件: ${type}`);
    }
    const id = options.id ? String(options.id) : `ui-${nextClientId++}`;
    const record = {
      id,
      type,
      version: PROTOCOL_VERSION,
      payload,
    };
    const line = `${JSON.stringify(record)}\n`;
    debugLog?.log("protocol.send", { record, line });
    writable.write(line);
    return record.id;
  };
}

function loadProtocolContract() {
  const contractUrl = new URL("../protocol-contract.json", import.meta.url);
  const contract = JSON.parse(fs.readFileSync(contractUrl, "utf8"));
  if (!contract || typeof contract !== "object" || Array.isArray(contract)) {
    throw new Error("protocol-contract.json 必须是对象");
  }
  if (!Number.isInteger(Number(contract.version)) || Number(contract.version) <= 0) {
    throw new Error("protocol-contract.json 缺少有效 version");
  }
  for (const key of ["client_events", "server_events"]) {
    if (!Array.isArray(contract[key]) || contract[key].some((item) => typeof item !== "string" || !item)) {
      throw new Error(`protocol-contract.json ${key} 必须是非空字符串数组`);
    }
  }
  return contract;
}

export function normalizeServerRecord(record) {
  if (!record || typeof record !== "object" || Array.isArray(record)) {
    throw new Error("Bridge 事件必须是对象");
  }
  const type = String(record.type ?? "");
  if (!type) {
    throw new Error("Bridge 事件缺少 type 字段");
  }
  if (!SERVER_EVENT_TYPES.has(type)) {
    throw new Error(`未知 Bridge 事件: ${type}`);
  }
  if (record.version != null && Number(record.version) !== PROTOCOL_VERSION) {
    throw new Error(`Bridge 协议版本不兼容: ${record.version}`);
  }
  const payload = record.payload ?? {};
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Bridge payload 必须是对象");
  }
  const normalized = {
    ...record,
    type,
    version: PROTOCOL_VERSION,
    payload: normalizeServerPayload(type, payload),
  };
  if (normalized.id != null) normalized.id = String(normalized.id);
  if (normalized.request_id != null) normalized.request_id = String(normalized.request_id);
  if (normalized.seq != null) normalized.seq = Number(normalized.seq);
  return normalized;
}

function normalizeServerPayload(type, payload) {
  if (type === "user/message") {
    return { ...payload, content: String(payload.content ?? "") };
  }
  if (type === "task/created") {
    return {
      ...payload,
      mission: normalizeObject(payload.mission),
      task: normalizeObject(payload.task),
      issue: normalizeObject(payload.issue),
      workbench_snapshot: normalizeObject(payload.workbench_snapshot),
    };
  }
  if (type === "run/cancelled") {
    return {
      ...payload,
      target_request_id: String(payload.target_request_id ?? ""),
      intent: payload.intent === "task" ? "task" : "chat",
      task_id: String(payload.task_id ?? ""),
      mission_id: String(payload.mission_id ?? ""),
      task_status: String(payload.task_status ?? ""),
      reason: String(payload.reason ?? ""),
    };
  }
  if (type === "ui/message") {
    const messageType = String(payload.type ?? "");
    if (!messageType) {
      throw new Error("ui/message payload 缺少 type 字段");
    }
    return { ...payload, type: messageType };
  }
  if (type === "mode/changed") {
    return {
      ...payload,
      mode: String(payload.mode ?? "").trim().toLowerCase(),
      status: normalizeObject(payload.status),
    };
  }
  if (type === "permission/resolved") {
    return {
      ...payload,
      request_id: String(payload.request_id ?? ""),
      choice: String(payload.choice ?? "").trim().toLowerCase(),
    };
  }
  if (type === "session/replayed") {
    return {
      ...payload,
      session_id: String(payload.session_id ?? ""),
      title: String(payload.title ?? ""),
      message_count: Number(payload.message_count ?? 0),
      clear: payload.clear == null ? true : toBool(payload.clear),
    };
  }
  if (type === "run/completed") {
    return {
      ...payload,
      status: String(payload.status ?? ""),
      response: String(payload.response ?? ""),
      error: String(payload.error ?? ""),
    };
  }
  if (type === "error") {
    return {
      ...payload,
      message: String(payload.message ?? "未知错误"),
      code: String(payload.code ?? "error"),
    };
  }
  if (type === "debug/trace") {
    return {
      ...payload,
      run_id: String(payload.run_id ?? ""),
      run_dir: String(payload.run_dir ?? ""),
      events_path: String(payload.events_path ?? ""),
      transcript_path: String(payload.transcript_path ?? ""),
    };
  }
  if (type === "workbench/snapshot") {
    return {
      ...payload,
      session_id: String(payload.session_id ?? ""),
      missions: Array.isArray(payload.missions) ? payload.missions : [],
      tasks: Array.isArray(payload.tasks) ? payload.tasks : [],
      issues: Array.isArray(payload.issues) ? payload.issues : [],
      failures: Array.isArray(payload.failures) ? payload.failures : [],
      events: Array.isArray(payload.events) ? payload.events : [],
    };
  }
  if (type === "workbench/event") {
    return {
      ...payload,
      id: String(payload.id ?? ""),
      type: String(payload.type ?? ""),
      actor: String(payload.actor ?? ""),
      subject_id: String(payload.subject_id ?? ""),
      payload: normalizeObject(payload.payload),
      timestamp: String(payload.timestamp ?? ""),
    };
  }
  return { ...payload };
}

function normalizeObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function toBool(value) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    return ["1", "true", "yes", "y", "on"].includes(value.trim().toLowerCase());
  }
  return false;
}

export function attachJsonlLineReader(stream, onLine) {
  const decoder = new StringDecoder("utf8");
  let buffer = "";
  stream.on("data", (chunk) => {
    buffer += typeof chunk === "string" ? chunk : decoder.write(chunk);
    while (true) {
      const index = buffer.indexOf("\n");
      if (index < 0) return;
      const line = buffer.slice(0, index).replace(/\r$/, "");
      buffer = buffer.slice(index + 1);
      onLine(line);
    }
  });
}
