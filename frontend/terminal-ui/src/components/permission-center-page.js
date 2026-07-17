import {
  ANSI,
  color,
  compactText,
  padRight,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";

export function renderPermissionCenterPage(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const value = view && typeof view === "object" ? view : {};
  const snapshot = value.snapshot && typeof value.snapshot === "object" ? value.snapshot : null;
  const logical = [
    color(ANSI.cyan, "权限策略中心"),
    color(ANSI.dim, "r 刷新 · ↑/↓ 滚动 · Esc 返回"),
  ];
  if (value.loading && !snapshot) {
    logical.push(color(ANSI.cyan, "正在加载权限权威快照…"));
  } else if (!snapshot) {
    logical.push(color(ANSI.yellow, compactText(value.error || "权限快照暂不可用。", 500)));
  } else {
    logical.push(
      `运行模式 · ${mode(snapshot.runtime_mode)} · 权限模式 · ${mode(snapshot.permission_mode)}`,
      color(
        snapshot.permission_mode === "bypass" ? ANSI.yellow : ANSI.dim,
        snapshot.permission_mode === "bypass"
          ? "bypass · 常规工具全权限放行，审计与资源限额继续生效"
          : "权限判断由 Python 策略层执行，前端只展示权威结果",
      ),
      ...section("待确认", snapshot.pending, renderPermission, "暂无待确认请求"),
      ...section("有效授权", snapshot.grants, renderGrant, "暂无本会话有效授权"),
      ...section("最近决定", snapshot.history, renderPermission, "暂无权限决定历史"),
      ...warnings(snapshot.warnings),
    );
  }
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, safeWidth));
  const offset = Math.min(
    Math.max(0, Number(value.scrollOffset) || 0),
    Math.max(0, wrapped.length - 1),
  );
  const lines = wrapped.slice(offset, offset + safeHeight);
  while (lines.length < safeHeight) lines.push("");
  return lines.slice(0, safeHeight).map((line) => padRight(fit(line, safeWidth), safeWidth));
}

function section(title, rawItems, renderer, empty) {
  const items = Array.isArray(rawItems) ? rawItems : [];
  return [
    color(ANSI.cyan, `── ${title} · ${items.length}`),
    ...(items.length ? items.slice(0, 50).flatMap(renderer) : [color(ANSI.dim, empty)]),
  ];
}

function renderPermission(item) {
  const status = String(item.status || "-");
  const style = ["denied", "blocked"].includes(status)
    ? ANSI.red
    : ["allowed", "confirmed", "granted", "bypass_enabled"].includes(status)
      ? ANSI.green
      : ANSI.yellow;
  const primary = `${item.request_id || item.call_id || "-"} · ${item.agent_name || "main"} → ${item.tool_name || "tool"} · ${status}`;
  const policy = item.policy || {};
  const detail = [
    `风险 ${policy.risk || item.risk_level || "-"}`,
    `来源 ${policy.source || "-"}`,
    policy.confirmation || "",
    policy.bypass || "",
  ].filter(Boolean).join(" · ");
  return [
    color(style, compactText(primary, 500)),
    color(ANSI.dim, compactText(detail, 500)),
    item.reason ? color(ANSI.dim, `原因 · ${compactText(item.reason, 500)}`) : "",
  ].filter(Boolean);
}

function renderGrant(item) {
  return [
    color(ANSI.green, `${item.grant_id || "-"} · ${item.tool_family || "tool"}`),
    color(
      ANSI.dim,
      item.expires_at ? `有效至 · ${item.expires_at}` : "范围 · 当前会话",
    ),
  ];
}

function warnings(rawItems) {
  const items = Array.isArray(rawItems) ? rawItems : [];
  if (!items.length) return [];
  return [
    color(ANSI.cyan, `── 警告 · ${items.length}`),
    ...items.map((item) => color(ANSI.yellow, compactText(item, 500))),
  ];
}

function mode(value) {
  const text = compactText(value || "-", 80);
  return value === "bypass" ? color(ANSI.yellow, text) : text;
}

function fit(line, width) {
  if (visibleWidth(line) <= width) return line;
  return wrapAnsiLine(line, width)[0] ?? "";
}
