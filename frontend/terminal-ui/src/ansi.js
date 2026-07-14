export const ANSI = {
  clear: "\x1b[2J\x1b[H",
  hideCursor: "\x1b[?25l",
  showCursor: "\x1b[?25h",
  altOn: "\x1b[?1049h",
  altOff: "\x1b[?1049l",
  bracketedPasteOn: "\x1b[?2004h",
  bracketedPasteOff: "\x1b[?2004l",
  keyboardDisambiguateOn: "\x1b[>1u",
  keyboardDisambiguateOff: "\x1b[<u",
  reset: "\x1b[0m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
  red: "\x1b[31m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  cyan: "\x1b[36m",
  magenta: "\x1b[35m",
  blue: "\x1b[34m",
};

export function color(style, text) {
  return `${style}${text}${ANSI.reset}`;
}

const OSC_PATTERN = /\x1b\][\s\S]*?(?:\x07|\x1b\\)/g;
const CSI_PATTERN = /\x1b\[[0-?]*[ -/]*[@-~]/g;
const SGR_AT_START_PATTERN = /^\x1b\[[0-9;]*m/;

export function sanitizeTerminalText(value) {
  return String(value ?? "")
    .replace(OSC_PATTERN, "")
    .replace(CSI_PATTERN, "")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");
}

export function colorDiffLine(line) {
  if (line.startsWith("+") && !line.startsWith("+++")) return color(ANSI.green, line);
  if (line.startsWith("-") && !line.startsWith("---")) return color(ANSI.red, line);
  if (line.startsWith("@@")) return color(ANSI.magenta, line);
  return color(ANSI.dim, line);
}

export function colorCodeLine(line) {
  let result = line
    .replace(/\b(class|def|function|const|let|var|return|if|else|for|while|import|from|async|await)\b/g, `${ANSI.cyan}$1${ANSI.reset}`)
    .replace(/\b(True|False|None|null|undefined)\b/g, `${ANSI.yellow}$1${ANSI.reset}`);
  if (/^\s*(#|\/\/)/.test(line)) result = color(ANSI.dim, line);
  return result;
}

export function looksLikeDiff(text) {
  const sample = String(text).split("\n").slice(0, 20);
  return sample.some((line) => line.startsWith("@@") || line.startsWith("---") || line.startsWith("+++"));
}

export function compactText(text, maxLength = 180) {
  return String(text).replace(/\s+/g, " ").trim().slice(0, maxLength);
}

export function formatContext(context) {
  const used = Number(context.used ?? 0);
  const window = Number(context.window ?? 0);
  const percent = context.percentage ?? 0;
  return `${Math.round(used / 1000)}K/${Math.round(window / 1000)}K (${percent}%)`;
}

export function formatMoney(value) {
  const num = Number(value ?? 0);
  return `$${num.toFixed(4)}`;
}

export function shortPath(value, home = "") {
  if (home && value.startsWith(home)) return `~${value.slice(home.length)}`;
  return value;
}

export function wrapAnsiLine(line, width) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const source = String(line ?? "");
  const result = [];
  const activeSgr = [];
  let current = "";
  let visible = 0;

  for (let index = 0; index < source.length;) {
    if (source[index] === "\x1b") {
      const sequence = source.slice(index).match(SGR_AT_START_PATTERN)?.[0];
      if (sequence) {
        current += sequence;
        if (sequence === ANSI.reset || /^\x1b\[(?:0|0;.*)?m$/.test(sequence)) {
          activeSgr.length = 0;
        } else {
          activeSgr.push(sequence);
        }
        index += sequence.length;
        continue;
      }
    }

    const codePoint = source.codePointAt(index);
    const ch = String.fromCodePoint(codePoint);
    const nextWidth = charWidth(ch);
    if (visible > 0 && visible + nextWidth > safeWidth) {
      result.push(activeSgr.length ? `${current}${ANSI.reset}` : current);
      current = activeSgr.join("");
      visible = 0;
    }
    current += ch;
    visible += nextWidth;
    index += ch.length;
  }

  result.push(current);
  return result;
}

export function truncateAnsi(line, width) {
  if (visibleWidth(line) <= width) return line;
  return `${stripAnsi(line).slice(0, Math.max(0, width - 1))}…`;
}

export function padRight(line, width) {
  return line + " ".repeat(Math.max(0, width - visibleWidth(line)));
}

export function visibleWidth(text) {
  return Array.from(stripAnsi(String(text))).reduce((sum, ch) => sum + charWidth(ch), 0);
}

export function charWidth(ch) {
  if (/^\p{Mark}+$/u.test(ch)) return 0;
  return /[\u1100-\u115f\u2e80-\ua4cf\uf900-\ufaff\ufe10-\ufe19\ufe30-\ufe6f\uff00-\uff60\uffe0-\uffe6]|\p{Extended_Pictographic}/u.test(ch) ? 2 : 1;
}

export function stripAnsi(text) {
  return String(text).replace(/\x1b\[[0-9;?<>]*[A-Za-z]/g, "");
}
