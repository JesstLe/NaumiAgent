import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const MAX_STRING_LENGTH = 20000;

export function createDebugLog({ cwd = process.cwd(), env = process.env } = {}) {
  const configured = env.NAUMI_TERMINAL_UI_DEBUG_LOG;
  if (configured && ["0", "false", "off", "none"].includes(configured.toLowerCase())) {
    return null;
  }
  const filePath = configured || path.join(cwd, ".naumi", "terminal-ui-debug.jsonl");
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  let closed = false;
  const logger = {
    path: filePath,
    log(event, payload = {}) {
      const record = {
        ts: new Date().toISOString(),
        event,
        pid: process.pid,
        payload: sanitize(payload),
      };
      if (!closed) fs.appendFileSync(filePath, `${JSON.stringify(record)}\n`);
    },
    close() {
      if (closed) return;
      closed = true;
    },
  };
  logger.log("terminal_ui.start", { cwd, path: filePath });
  return logger;
}

export function sanitize(value) {
  if (typeof value === "string") {
    if (value.length <= MAX_STRING_LENGTH) return value;
    return `${value.slice(0, MAX_STRING_LENGTH)}…[truncated ${value.length - MAX_STRING_LENGTH} chars]`;
  }
  if (Array.isArray(value)) return value.map((item) => sanitize(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, sanitize(item)]),
    );
  }
  return value;
}
