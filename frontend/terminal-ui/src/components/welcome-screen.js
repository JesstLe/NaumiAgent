import {
  ANSI,
  color,
  shortPath,
  truncateAnsi,
  visibleWidth,
} from "../ansi.js";

const WIDE_LOGO = [
  "██   ██   █████   ██   ██  ██   ██  ███████",
  "███  ██  ██   ██  ██   ██  ███ ███    ███  ",
  "████ ██  ██   ██  ██   ██  ███████    ███  ",
  "██ ████  ███████  ██   ██  ██ █ ██    ███  ",
  "██  ███  ██   ██  ██   ██  ██   ██    ███  ",
  "██   ██  ██   ██  ██   ██  ██   ██    ███  ",
  "██   ██  ██   ██   █████   ██   ██  ███████",
];

const MEDIUM_LOGO = [
  "█  █   ██   █  █  █  █  ███",
  "██ █  █  █  █  █  ████   █ ",
  "████  ████  █  █  █ ██   █ ",
  "█ ██  █  █  █  █  █  █   █ ",
  "█  █  █  █   ██   █  █  ███",
];

export function selectWelcomeLayout(width, bodyHeight) {
  if (width < 24 || bodyHeight < 4) return "minimal";
  if (width >= 100 && bodyHeight >= 16) return "wide";
  if (width >= 56 && bodyHeight >= 10) return "medium";
  return "compact";
}

export function shouldRenderWelcome(state) {
  return state?.route?.name === "conversation"
    && state?.inspector?.open !== true
    && state?.welcome?.dismissed !== true;
}

export function renderWelcomeScreen(state, width, bodyHeight, env = {}) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(bodyHeight) || 1);
  const layout = selectWelcomeLayout(safeWidth, safeHeight);
  const ready = state?.welcome?.phase === "ready_empty";
  const status = state?.status ?? {};
  const fact = (value) => String(value || "未解析");
  const mode = fact(status.mode || state?.mode);
  const permissionMode = fact(status.permission_mode);
  const renderedMode = mode === "bypass" ? color(ANSI.yellow, mode) : mode;
  const renderedPermissionMode = permissionMode === "bypass"
    ? color(ANSI.yellow, permissionMode)
    : permissionMode;
  const readiness = ready
    ? color(ANSI.green, "已就绪")
    : color(ANSI.yellow, "正在启动本地运行时…");
  const product = `NaumiAgent v${fact(status.version)}`;
  const workspace = shortPath(fact(status.workspace_root), env.home ?? "");
  const model = fact(status.model);

  let content;
  if (layout === "minimal") {
    content = [color(`${ANSI.bold}${ANSI.cyan}`, "NAUMI") + ` · ${readiness}`];
  } else if (!ready) {
    const logo = layout === "wide" ? WIDE_LOGO : layout === "medium" ? MEDIUM_LOGO : ["NAUMI"];
    content = [
      ...logo.map((line) => color(`${ANSI.bold}${ANSI.cyan}`, line)),
      "",
      readiness,
    ];
  } else {
    const logo = layout === "wide" ? WIDE_LOGO : layout === "medium" ? MEDIUM_LOGO : ["NAUMI"];
    content = [
      ...logo.map((line) => color(`${ANSI.bold}${ANSI.cyan}`, line)),
      "",
      `${color(ANSI.dim, "版本")} ${product} · ${readiness}`,
      `${color(ANSI.dim, "工作区")} ${workspace}`,
      `${color(ANSI.dim, "模型")} ${model}`,
      `${color(ANSI.dim, "模式")} ${renderedMode} · ${color(ANSI.dim, "权限")} ${renderedPermissionMode}`,
    ];
  }

  const bounded = content.map((line) => truncateAnsi(line, safeWidth));
  const top = Math.max(0, Math.floor((safeHeight - bounded.length) / 2));
  const lines = Array.from({ length: top }, () => "");
  for (const line of bounded) {
    const left = Math.max(0, Math.floor((safeWidth - visibleWidth(line)) / 2));
    lines.push(`${" ".repeat(left)}${line}`);
  }
  while (lines.length < safeHeight) lines.push("");
  return lines.slice(0, safeHeight);
}
