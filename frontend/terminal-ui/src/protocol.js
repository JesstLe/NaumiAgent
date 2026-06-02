import { StringDecoder } from "node:string_decoder";

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
  return function send(type, payload) {
    const record = {
      id: `ui-${nextClientId++}`,
      type,
      version: 1,
      payload,
    };
    const line = `${JSON.stringify(record)}\n`;
    debugLog?.log("protocol.send", { record, line });
    writable.write(line);
    return record.id;
  };
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
