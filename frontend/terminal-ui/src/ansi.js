const COLOR_CODES = Object.freeze({
  reset: "\x1b[0m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
  red: "\x1b[31m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  cyan: "\x1b[36m",
  magenta: "\x1b[35m",
  blue: "\x1b[34m",
});

export const ANSI = {
  clear: "\x1b[2J\x1b[H",
  synchronizedOutputOn: "\x1b[?2026h",
  synchronizedOutputOff: "\x1b[?2026l",
  cursorTo(row, column = 1) {
    const safeRow = Math.max(1, Math.trunc(Number(row) || 1));
    const safeColumn = Math.max(1, Math.trunc(Number(column) || 1));
    return `\x1b[${safeRow};${safeColumn}H`;
  },
  hideCursor: "\x1b[?25l",
  showCursor: "\x1b[?25h",
  altOn: "\x1b[?1049h",
  altOff: "\x1b[?1049l",
  bracketedPasteOn: "\x1b[?2004h",
  bracketedPasteOff: "\x1b[?2004l",
  keyboardDisambiguateOn: "\x1b[>1u",
  keyboardDisambiguateOff: "\x1b[<u",
  ...COLOR_CODES,
};

export function configureAnsiColors(enabled) {
  for (const [name, sequence] of Object.entries(COLOR_CODES)) {
    ANSI[name] = enabled ? sequence : "";
  }
}

export function color(style, text) {
  return `${style}${text}${ANSI.reset}`;
}

const OSC_PATTERN = /\x1b\][\s\S]*?(?:\x07|\x1b\\)/g;
const CSI_PATTERN = /\x1b\[[0-?]*[ -/]*[@-~]/g;
const SGR_AT_START_PATTERN = /^\x1b\[[0-9;]*m/;
const GRAPHEME_SEGMENTER = new Intl.Segmenter("und", { granularity: "grapheme" });
const ZERO_WIDTH_CODE_POINT = /^(?:\p{Mark}|\p{Cf}|\p{Cc})$/u;
const EMOJI_PRESENTATION = /\p{Emoji_Presentation}/u;
const EXTENDED_PICTOGRAPHIC = /\p{Extended_Pictographic}/u;
const REGIONAL_INDICATOR_CLUSTER = /^(?:\p{Regional_Indicator})+$/u;
const KEYCAP_CLUSTER = /^[0-9#*]\ufe0f?\u20e3$/u;
const WIDE_CODE_POINT = /[\u1100-\u115f\u231a\u231b\u2329\u232a\u23e9-\u23ec\u23f0\u23f3\u25fd\u25fe\u2614\u2615\u2648-\u2653\u267f\u2693\u26a1\u26aa\u26ab\u26bd\u26be\u26c4\u26c5\u26ce\u26d4\u26ea\u26f2\u26f3\u26f5\u26fa\u26fd\u2705\u270a\u270b\u2728\u274c\u274e\u2753-\u2755\u2757\u2795-\u2797\u27b0\u27bf\u2b1b\u2b1c\u2b50\u2b55\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe10-\ufe19\ufe30-\ufe6f\uff00-\uff60\uffe0-\uffe6]|\p{Emoji_Presentation}|[\u{16fe0}-\u{16fe4}\u{16ff0}\u{16ff1}\u{17000}-\u{18dff}\u{1aff0}-\u{1afff}\u{1b000}-\u{1b2ff}\u{1f200}-\u{1f251}\u{20000}-\u{3fffd}]/u;

export function sanitizeTerminalText(value) {
  return String(value ?? "")
    .replace(OSC_PATTERN, "")
    .replace(CSI_PATTERN, "")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");
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
  const widthState = { skipFirstCodePoint: false };
  let current = "";
  let visible = 0;

  const finishLine = () => {
    if (visible <= 0) return;
    result.push(activeSgr.length ? `${current}${COLOR_CODES.reset}` : current);
    current = activeSgr.join("");
    visible = 0;
  };

  for (const token of ansiGraphemeTokens(source)) {
    if (token.type === "sgr") {
      current += token.value;
      updateActiveSgr(activeSgr, token.value);
      continue;
    }

    const nextWidth = contextualGraphemeWidth(token.value, widthState);
    if (nextWidth > safeWidth) {
      finishLine();
      const replacement = truncatePlain("…", safeWidth, "");
      current += replacement;
      visible = visibleWidth(replacement);
      finishLine();
      continue;
    }
    if (visible > 0 && visible + nextWidth > safeWidth) {
      finishLine();
    }
    current += token.value;
    visible += nextWidth;
  }

  finishLine();
  if (!result.length) result.push(current);
  return result;
}

export function truncateAnsi(line, width) {
  const safeWidth = Math.max(0, Number(width) || 0);
  const source = String(line ?? "");
  if (safeWidth <= 0) return "";
  if (visibleWidth(source) <= safeWidth) return source;

  const marker = truncatePlain("…", safeWidth, "");
  const target = Math.max(0, safeWidth - visibleWidth(marker));
  const activeSgr = [];
  const widthState = { skipFirstCodePoint: false };
  let output = "";
  let used = 0;

  for (const token of ansiGraphemeTokens(source)) {
    if (token.type === "sgr") {
      output += token.value;
      updateActiveSgr(activeSgr, token.value);
      continue;
    }
    const nextWidth = contextualGraphemeWidth(token.value, widthState);
    if (used + nextWidth > target) break;
    output += token.value;
    used += nextWidth;
  }
  return `${output}${marker}${activeSgr.length ? COLOR_CODES.reset : ""}`;
}

export function padRight(line, width) {
  return line + " ".repeat(Math.max(0, width - visibleWidth(line)));
}

export function visibleWidth(text) {
  return Array.from(graphemesWithWidths(stripAnsi(String(text)))).reduce(
    (sum, entry) => sum + entry.width,
    0,
  );
}

export function charWidth(value) {
  return Array.from(graphemesWithWidths(String(value ?? ""))).reduce(
    (sum, entry) => sum + entry.width,
    0,
  );
}

export function graphemeCellWidths(value) {
  return Array.from(
    graphemesWithWidths(String(value ?? "")),
    (entry) => entry.width,
  );
}

export function stripAnsi(text) {
  return String(text).replace(OSC_PATTERN, "").replace(CSI_PATTERN, "");
}

export function truncatePlain(value, width, marker = "…") {
  const safeWidth = Math.max(0, Number(width) || 0);
  if (safeWidth <= 0) return "";
  const text = stripAnsi(String(value ?? ""));
  if (visibleWidth(text) <= safeWidth) return text;

  const safeMarker = takePlainPrefix(stripAnsi(marker), safeWidth);
  const target = Math.max(0, safeWidth - visibleWidth(safeMarker));
  return `${takePlainPrefix(text, target)}${safeMarker}`;
}

export function segmentGraphemes(value) {
  return Array.from(
    GRAPHEME_SEGMENTER.segment(String(value ?? "")),
    (entry) => entry.segment,
  );
}

function graphemeWidth(grapheme) {
  if (!grapheme) return 0;
  if (REGIONAL_INDICATOR_CLUSTER.test(grapheme)) return 2;
  if (
    KEYCAP_CLUSTER.test(grapheme)
    || (
      (grapheme.includes("\u200d") || grapheme.includes("\ufe0f"))
      && EXTENDED_PICTOGRAPHIC.test(grapheme)
    )
    || EMOJI_PRESENTATION.test(grapheme)
  ) {
    return 2;
  }

  let width = 0;
  for (const codePoint of Array.from(grapheme)) {
    if (ZERO_WIDTH_CODE_POINT.test(codePoint)) continue;
    width += WIDE_CODE_POINT.test(codePoint) ? 2 : 1;
  }
  return width;
}

function takePlainPrefix(value, width) {
  const safeWidth = Math.max(0, Number(width) || 0);
  let output = "";
  let used = 0;
  for (const { grapheme, width: nextWidth } of graphemesWithWidths(value)) {
    if (used + nextWidth > safeWidth) break;
    output += grapheme;
    used += nextWidth;
  }
  return output;
}

function* graphemesWithWidths(value) {
  const state = { skipFirstCodePoint: false };
  for (const grapheme of segmentGraphemes(value)) {
    yield {
      grapheme,
      width: contextualGraphemeWidth(grapheme, state),
    };
  }
}

function contextualGraphemeWidth(grapheme, state) {
  let measured = grapheme;
  if (state.skipFirstCodePoint) {
    measured = Array.from(grapheme).slice(1).join("");
    state.skipFirstCodePoint = false;
  }
  const width = graphemeWidth(measured);
  if (grapheme.endsWith("\u200d")) state.skipFirstCodePoint = true;
  return width;
}

function* ansiGraphemeTokens(source) {
  let index = 0;
  while (index < source.length) {
    if (source[index] === "\x1b") {
      const sequence = source.slice(index).match(SGR_AT_START_PATTERN)?.[0];
      if (sequence) {
        yield { type: "sgr", value: sequence };
        index += sequence.length;
        continue;
      }
    }
    const nextEscape = source.indexOf("\x1b", index);
    const end = nextEscape < 0 ? source.length : nextEscape;
    const chunkEnd = end === index ? index + 1 : end;
    for (const grapheme of segmentGraphemes(source.slice(index, chunkEnd))) {
      yield { type: "text", value: grapheme };
    }
    index = chunkEnd;
  }
}

function updateActiveSgr(activeSgr, sequence) {
  const parameters = sequence.slice(2, -1);
  const values = parameters === "" ? [0] : parameters.split(";").map(Number);
  if (values.includes(0)) {
    activeSgr.length = 0;
    const resetIndex = values.lastIndexOf(0);
    if (resetIndex < values.length - 1) {
      activeSgr.push(`\x1b[${values.slice(resetIndex + 1).join(";")}m`);
    }
    return;
  }
  activeSgr.push(sequence);
}
