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
  const exportPreview = object(value.exportPreview);
  const exportReceipt = object(value.exportReceipt);
  const probeResult = object(value.probeResult);
  const counts = items.reduce((result, item) => {
    const key = SEVERITY[item.severity] ? item.severity : "unknown";
    result[key] += 1;
    return result;
  }, { ok: 0, degraded: 0, error: 0, unknown: 0 });
  const logical = [
    color(ANSI.cyan, "环境健康诊断"),
    color(
      ANSI.dim,
      "r 本地只读零网络 · p 在线（最多 1 请求/最多 8 输出 token/15000ms/不自动重试）"
        + " · e 导出 · Esc 返回",
    ),
    value.loading && !snapshot.schema_version
      ? color(ANSI.cyan, "正在检查本机环境…")
      : `正常 ${counts.ok} · 受限 ${counts.degraded} · 错误 ${counts.error} · 未知 ${counts.unknown}`,
    ...(snapshot.generated_at ? [color(ANSI.dim, `生成时间 · ${text(snapshot.generated_at)}`)] : []),
    ...(snapshot.live_probe
      ? [color(ANSI.cyan, "此快照包含显式在线探测证据。")]
      : []),
    ...items.flatMap(renderItem),
    ...(snapshot.snapshot_sha256
      ? [color(ANSI.dim, `Snapshot · ${text(snapshot.snapshot_sha256).slice(0, 12)}`)]
      : []),
    ...(value.exportLoading ? [color(ANSI.cyan, "正在准备脱敏诊断包…")] : []),
    ...(value.exportNotice
      ? [color(ANSI.yellow, `兼容模式 · ${text(value.exportNotice)}`)]
      : []),
    ...(value.exportError ? [color(ANSI.red, `导出失败 · ${text(value.exportError)}`)] : []),
    ...renderExportPreview(exportPreview, exportReceipt),
    ...(value.probeLoading
      ? [color(
        ANSI.cyan,
        `模型提供商在线探测中 · 超时 ${Number(value.probeTimeoutMs) || 15000}ms · c 取消`,
      )]
      : []),
    ...(value.probeNotice
      ? [color(ANSI.yellow, `在线探测 · ${text(value.probeNotice)}`)]
      : []),
    ...(value.probeError
      ? [color(ANSI.red, `在线探测失败 · ${text(value.probeError)}`)]
      : []),
    ...renderProbeResult(probeResult),
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

function renderProbeResult(result) {
  if (!result.schema_version) return [];
  const labels = {
    passed: [ANSI.green, "通过"],
    failed: [ANSI.red, "失败"],
    blocked: [ANSI.yellow, "已阻止"],
    cancelled: [ANSI.yellow, "已取消"],
  };
  const [tone, label] = labels[result.status] || [ANSI.dim, "未知"];
  return [
    color(tone, `在线探测回执 · ${label}`),
    `  ${text(result.message)}`,
    `  请求 ${Number(result.request_count) || 0}/${Number(result.request_limit) || 1}`
      + ` · 最大输出 ${Number(result.max_output_tokens) || 8} tokens`
      + ` · 超时 ${Number(result.timeout_ms) || 15000}ms`
      + ` · 耗时 ${Number(result.duration_ms) || 0}ms`,
    ...(result.diagnostic_code
      ? [color(ANSI.magenta, `  诊断码 · ${text(result.diagnostic_code)}`)]
      : []),
    ...(result.suggestion
      ? [color(ANSI.cyan, `  下一步 · ${text(result.suggestion)}`)]
      : []),
  ];
}

function renderExportPreview(preview, receipt) {
  if (!preview.schema_version) return [];
  const lines = [
    color(ANSI.cyan, "诊断包预览"),
    `${array(preview.files).length} 个文件 · ${formatBytes(preview.total_bytes)} · ZIP`,
    ...array(preview.files).map(
      (file) => `  ${text(file.path)} · ${formatBytes(file.size_bytes)} · ${text(file.description)}`,
    ),
    color(ANSI.dim, `Bundle · ${text(preview.bundle_sha256).slice(0, 16)}`),
    color(ANSI.yellow, text(preview.privacy_notice)),
  ];
  if (receipt.output_path) {
    lines.push(color(
      ANSI.green,
      `${receipt.reused_existing ? "已复用" : "已导出"} · ${text(receipt.output_path)}`,
    ));
  } else {
    lines.push(color(ANSI.cyan, "确认清单无误后，再按 e 写入 Naumi 状态目录。"));
  }
  return lines;
}

function formatBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KiB`;
}

function renderItem(item) {
  const [tone, label] = SEVERITY[item.severity] || SEVERITY.unknown;
  const domain = DOMAIN[item.domain] || text(item.domain) || "运行时";
  const owner = RESPONSIBILITY[item.responsibility] || "未知";
  return [
    color(tone, `● ${label} · ${domain} · ${text(item.label) || "未命名检查"}`),
    `  ${text(item.detail) || "暂无详情"}`,
    ...(item.severity !== "ok" ? [color(ANSI.dim, `  归因 · ${owner}`)] : []),
    ...(item.diagnostic_code ? [color(ANSI.magenta, `  诊断码 · ${text(item.diagnostic_code)}`)] : []),
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
