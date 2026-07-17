import { ANSI, color, compactText, padRight, visibleWidth, wrapAnsiLine } from "../ansi.js";

const SEVERITY = Object.freeze({
  ok: [ANSI.green, "正常"],
  degraded: [ANSI.yellow, "受限"],
  error: [ANSI.red, "错误"],
  unknown: [ANSI.dim, "未知"],
});
const DOMAIN = Object.freeze({
  runtime: "运行时", model: "模型", provider: "提供商", store: "存储", git: "Git",
  node: "Node.js", browser: "浏览器", mcp: "MCP", terminal: "终端",
});
const RESPONSIBILITY = Object.freeze({
  user_config: "用户配置", local_environment: "本机环境", external_service: "外部服务",
  product_runtime: "产品运行时", unknown: "无需归责",
});

export function renderDoctorHealthPage(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const value = object(view);
  const snapshot = object(value.snapshot);
  const heartbeat = heartbeatItem(value.heartbeat);
  const items = [heartbeat, ...array(snapshot.items)];
  const counts = items.reduce((result, item) => {
    const key = SEVERITY[item.severity] ? item.severity : "unknown";
    result[key] += 1;
    return result;
  }, { ok: 0, degraded: 0, error: 0, unknown: 0 });
  const logical = [
    color(ANSI.cyan, "环境健康诊断"),
    color(ANSI.dim, "本地只读检查 · 不会探测付费模型 · r 刷新 · Esc 返回"),
    value.loading && !snapshot.schema_version
      ? color(ANSI.cyan, "正在检查本机环境…")
      : `正常 ${counts.ok} · 受限 ${counts.degraded} · 错误 ${counts.error} · 未知 ${counts.unknown}`,
    ...(snapshot.generated_at ? [color(ANSI.dim, `生成时间 · ${text(snapshot.generated_at)}`)] : []),
    ...items.flatMap(renderItem),
    ...(snapshot.snapshot_sha256
      ? [color(ANSI.dim, `Snapshot · ${text(snapshot.snapshot_sha256).slice(0, 12)}`)]
      : []),
  ];
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, safeWidth));
  const offset = Math.min(
    Math.max(0, Number(value.scrollOffset) || 0),
    Math.max(0, wrapped.length - 1),
  );
  const lines = wrapped.slice(offset, offset + safeHeight);
  while (lines.length < safeHeight) lines.push("");
  return lines.map((line) => padRight(fit(line, safeWidth), safeWidth));
}

function renderItem(item) {
  const [tone, label] = SEVERITY[item.severity] || SEVERITY.unknown;
  const domain = DOMAIN[item.domain] || text(item.domain) || "运行时";
  const owner = RESPONSIBILITY[item.responsibility] || "未知";
  return [
    color(tone, `● ${label} · ${domain} · ${text(item.label) || "未命名检查"}`),
    `  ${text(item.detail) || "暂无详情"}`,
    ...(item.severity !== "ok" ? [color(ANSI.dim, `  归因 · ${owner}`)] : []),
    ...(item.suggestion ? [color(ANSI.cyan, `  下一步 · ${text(item.suggestion)}`)] : []),
  ];
}

function heartbeatItem(value) {
  const heartbeat = object(value);
  if (heartbeat.status === "stale") {
    return {
      domain: "runtime", label: "Bridge 心跳", severity: "error",
      responsibility: "product_runtime",
      detail: `后端控制面已 ${Math.max(0, Number(heartbeat.ageMs) || 0)}ms 无响应。`,
      suggestion: "保留当前任务现场，查看 /debug；不要自动重复提交命令。",
    };
  }
  if (heartbeat.status === "healthy") {
    return {
      domain: "runtime", label: "Bridge 心跳", severity: "ok", responsibility: "unknown",
      detail: `控制面响应正常，往返 ${Math.max(0, Number(heartbeat.rttMs) || 0)}ms。`,
      suggestion: "",
    };
  }
  return {
    domain: "runtime", label: "Bridge 心跳", severity: "unknown", responsibility: "unknown",
    detail: "正在等待首次心跳证据。", suggestion: "等待协议协商完成后再判断。",
  };
}

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function text(value) {
  return compactText(value ?? "", 500);
}

function fit(line, width) {
  if (visibleWidth(line) <= width) return line;
  return wrapAnsiLine(line, width)[0] ?? "";
}
