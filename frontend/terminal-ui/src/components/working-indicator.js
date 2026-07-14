import {
  ANSI,
  charWidth,
  color,
  compactText,
  sanitizeTerminalText,
  visibleWidth,
} from "../ansi.js";

export const WORKING_FRAME_COUNT = 4;

const FRAMES = Object.freeze([
  { leftEye: "◉", rightEye: "•", core: "◐", coreStyle: "magenta" },
  { leftEye: "◉", rightEye: "◉", core: "◓", coreStyle: "blue" },
  { leftEye: "•", rightEye: "◉", core: "◑", coreStyle: "cyan" },
  { leftEye: "•", rightEye: "•", core: "◒", coreStyle: "green" },
]);

const STATIC_FRAME = Object.freeze({
  leftEye: "•",
  rightEye: "•",
  core: "◇",
  coreStyle: "yellow",
});

export function workingIndicatorStatus(state) {
  if (state?.running !== true) {
    return { visible: false, animate: false, label: "", phaseLabel: "", style: ANSI.dim };
  }

  const phase = String(state?.activeRunActivity?.phase ?? "preparing");
  const phaseLabel = compactText(sanitizeTerminalText(
    state?.activeRunActivity?.phaseLabel ?? phaseLabelFor(phase),
  ), 80);
  if (state?.cancelPending === true) {
    return { visible: true, animate: false, label: "正在取消", phaseLabel, style: ANSI.yellow };
  }
  if (state?.permission || phase === "awaiting_permission") {
    return { visible: true, animate: false, label: "等待权限确认", phaseLabel, style: ANSI.yellow };
  }
  if (state?.interaction || phase === "awaiting_input") {
    return { visible: true, animate: false, label: "等待用户输入", phaseLabel, style: ANSI.yellow };
  }
  if (phase === "executing") {
    return { visible: true, animate: true, label: "工具执行中", phaseLabel, style: ANSI.cyan };
  }
  return { visible: true, animate: true, label: "模型工作中", phaseLabel, style: ANSI.green };
}

export function shouldAnimateWorkingIndicator(state, capabilities = {}) {
  const status = workingIndicatorStatus(state);
  if (!status.animate) return false;
  if (capabilities.isTTY !== true) return false;
  if (String(capabilities.term ?? "").toLowerCase() === "dumb") return false;
  if (capabilities.ci === true || capabilities.reduceMotion === true) return false;
  return true;
}

export function renderWorkingIndicator(state, width, options = {}) {
  const status = workingIndicatorStatus(state);
  if (!status.visible) return [];

  const safeWidth = Math.max(1, Number(width) || 1);
  const requestedBodyHeight = Number(options.bodyHeight);
  const safeBodyHeight = Number.isFinite(requestedBodyHeight)
    ? Math.max(1, requestedBodyHeight)
    : Number.POSITIVE_INFINITY;
  const ascii = options.ascii === true || String(options.term ?? "").toLowerCase() === "dumb";
  if (ascii) {
    const base = `[o] ${status.label}`;
    if (visibleWidth(base) >= safeWidth) return [truncateVisiblePlain(base, safeWidth)];
    const suffix = boundedPhaseSuffix(status, safeWidth - visibleWidth(base));
    return [`${base}${suffix}`];
  }

  const frame = status.animate
    ? FRAMES[normalizeFrame(state?.workingAnimationFrame)]
    : STATIC_FRAME;
  if (safeWidth < 70 || safeBodyHeight < 8) {
    const plainBase = `${frame.core} ${status.label}`;
    if (visibleWidth(plainBase) >= safeWidth) {
      return [truncateVisiblePlain(plainBase, safeWidth)];
    }
    const suffix = boundedPhaseSuffix(status, safeWidth - visibleWidth(plainBase));
    return [
      `${color(ANSI[frame.coreStyle], frame.core)} ${color(status.style, status.label)}${color(ANSI.dim, suffix)}`,
    ];
  }

  const middleBase = `   │ ${frame.leftEye} ${frame.rightEye} │   ${status.label}`;
  const suffix = boundedPhaseSuffix(status, safeWidth - visibleWidth(middleBase));
  return [
    color(ANSI.cyan, "   ╭─────╮"),
    `${color(ANSI.cyan, "   │")} ${color(ANSI.yellow, frame.leftEye)} ${color(ANSI.yellow, frame.rightEye)} ${color(ANSI.cyan, "│")}   ${color(status.style, status.label)}${color(ANSI.dim, suffix)}`,
    `${color(ANSI.cyan, "   ╰──")}${color(ANSI[frame.coreStyle], frame.core)}${color(ANSI.cyan, "──╯")}`,
  ];
}

function normalizeFrame(value) {
  const frame = Math.trunc(Number(value) || 0);
  return ((frame % WORKING_FRAME_COUNT) + WORKING_FRAME_COUNT) % WORKING_FRAME_COUNT;
}

function boundedPhaseSuffix(status, maxWidth) {
  if (!status.phaseLabel || status.phaseLabel === status.label || maxWidth <= 0) return "";
  const separator = " · ";
  const available = maxWidth - visibleWidth(separator);
  if (available <= 0) return "";
  return `${separator}${truncateVisiblePlain(status.phaseLabel, available)}`;
}

function truncateVisiblePlain(value, maxWidth) {
  const safeWidth = Math.max(0, Number(maxWidth) || 0);
  const text = String(value ?? "");
  if (visibleWidth(text) <= safeWidth) return text;
  if (safeWidth <= 0) return "";
  if (safeWidth === 1) return "…";
  let output = "";
  let width = 0;
  for (const character of text) {
    const next = charWidth(character);
    if (width + next + 1 > safeWidth) break;
    output += character;
    width += next;
  }
  return `${output}…`;
}

function phaseLabelFor(phase) {
  return {
    preparing: "准备运行",
    generating: "生成响应",
    executing: "执行工具",
    awaiting_permission: "等待权限",
    awaiting_input: "等待用户输入",
    summarizing: "整理结果",
  }[phase] ?? "运行中";
}
