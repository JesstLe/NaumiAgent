import { ANSI, color, sanitizeTerminalText } from "../ansi.js";

const CODE_TOKEN_PATTERN = /\b(class|def|function)\b(\s+)([A-Za-z_$][\w$]*)|\b(const|let|var|return|if|else|for|while|import|from|async|await|try|catch|finally|throw|raise|with|as|switch|case|break|continue|new|extends|implements|public|private|protected|static|yield|lambda|in|is|and|or|not)\b|\b(True|False|None|null|undefined|true|false)\b|\b(\d+(?:\.\d+)?)\b/g;

export function renderSemanticInline(value) {
  const text = sanitizeTerminalText(value);
  let output = "";
  let plain = "";

  const flushPlain = () => {
    output += plain;
    plain = "";
  };

  for (let index = 0; index < text.length;) {
    if (text[index] === "\\" && index + 1 < text.length) {
      const mathClose = text[index + 1] === "(" ? "\\)" : text[index + 1] === "[" ? "\\]" : "";
      if (mathClose) {
        const end = text.indexOf(mathClose, index + 2);
        if (end >= 0) {
          flushPlain();
          const finish = end + mathClose.length;
          output += color(ANSI.magenta, text.slice(index, finish));
          index = finish;
          continue;
        }
      }
      plain += text.slice(index, index + 2);
      index += 2;
      continue;
    }

    if (text[index] === "`") {
      const end = findClosing(text, "`", index + 1);
      if (end >= 0) {
        flushPlain();
        output += color(ANSI.dim, "`");
        output += color(ANSI.yellow, text.slice(index + 1, end));
        output += color(ANSI.dim, "`");
        index = end + 1;
        continue;
      }
    }

    if (text[index] === "[") {
      const labelEnd = text.indexOf("](", index + 1);
      const urlEnd = labelEnd >= 0 ? text.indexOf(")", labelEnd + 2) : -1;
      if (urlEnd >= 0) {
        flushPlain();
        output += color(ANSI.blue, text.slice(index, labelEnd + 1));
        output += color(`${ANSI.dim}${ANSI.blue}`, text.slice(labelEnd + 1, urlEnd + 1));
        index = urlEnd + 1;
        continue;
      }
    }

    if (text[index] === "$") {
      const delimiter = text.startsWith("$$", index) ? "$$" : "$";
      const end = findClosing(text, delimiter, index + delimiter.length);
      if (end >= 0 && end > index + delimiter.length) {
        flushPlain();
        const finish = end + delimiter.length;
        output += color(ANSI.magenta, text.slice(index, finish));
        index = finish;
        continue;
      }
    }

    const strong = text.startsWith("**", index) ? "**" : text.startsWith("__", index) ? "__" : "";
    if (strong) {
      const end = findClosing(text, strong, index + strong.length);
      if (end >= 0 && end > index + strong.length) {
        flushPlain();
        output += color(ANSI.bold, text.slice(index, end + strong.length));
        index = end + strong.length;
        continue;
      }
    }

    const emphasis = text[index] === "*" || text[index] === "_" ? text[index] : "";
    if (emphasis) {
      const end = findClosing(text, emphasis, index + 1);
      if (end > index + 1) {
        flushPlain();
        output += color(ANSI.cyan, text.slice(index, end + 1));
        index = end + 1;
        continue;
      }
    }

    plain += text[index];
    index += 1;
  }
  flushPlain();
  return output;
}

export function renderSemanticMarkdownLine(value, context = {}) {
  const line = sanitizeTerminalText(value);
  const trimmed = line.trim();
  if ((trimmed.startsWith("$$") && trimmed.endsWith("$$") && trimmed.length > 4)
    || (trimmed.startsWith("\\[") && trimmed.endsWith("\\]") && trimmed.length > 4)) {
    return color(ANSI.magenta, line);
  }
  if (/^\s{0,3}#{1,6}\s+/.test(line)) {
    return color(`${ANSI.bold}${ANSI.cyan}`, line);
  }
  if (/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
    return color(ANSI.dim, line);
  }
  const quote = line.match(/^(\s*>\s?)(.*)$/);
  if (quote) {
    return `${color(ANSI.blue, quote[1])}${color(ANSI.dim, quote[2])}`;
  }
  const list = line.match(/^(\s*(?:[-+*]|\d+[.)])\s+)(.*)$/);
  if (list) {
    return `${color(ANSI.cyan, list[1])}${renderSemanticInline(list[2])}`;
  }
  if (isTableDivider(line)) {
    return color(ANSI.dim, line);
  }
  if (context.isTableHeader) {
    return color(`${ANSI.bold}${ANSI.cyan}`, line);
  }
  return renderSemanticInline(line);
}

export function renderSemanticCodeLine(value, _language = "") {
  const line = sanitizeTerminalText(value);
  let output = "";
  let plain = "";

  const flushPlain = () => {
    output += renderCodePlain(plain);
    plain = "";
  };

  for (let index = 0; index < line.length;) {
    const ch = line[index];
    if (ch === "#" || (ch === "/" && line[index + 1] === "/")) {
      flushPlain();
      output += color(ANSI.dim, line.slice(index));
      return output;
    }
    if (ch === "\"" || ch === "'" || ch === "`") {
      flushPlain();
      const end = findStringEnd(line, ch, index + 1);
      const finish = end < 0 ? line.length : end + 1;
      output += color(ANSI.green, line.slice(index, finish));
      index = finish;
      continue;
    }
    plain += ch;
    index += 1;
  }
  flushPlain();
  return output;
}

export function renderSemanticDiffLine(value) {
  const line = sanitizeTerminalText(value);
  if (/^(<<<<<<<|=======|>>>>>>>)/.test(line)) return color(`${ANSI.bold}${ANSI.red}`, line);
  if (line.startsWith("diff --git")) return color(`${ANSI.bold}${ANSI.cyan}`, line);
  if (line.startsWith("---") || line.startsWith("+++")) return color(ANSI.cyan, line);
  if (line.startsWith("@@")) return color(ANSI.magenta, line);
  if (line.startsWith("+")) return color(ANSI.green, line);
  if (line.startsWith("-")) return color(ANSI.red, line);
  if (/^(index |new file mode |deleted file mode |old mode |new mode |similarity index |rename from |rename to |Binary files )/.test(line)) {
    return color(ANSI.dim, line);
  }
  return color(ANSI.dim, line);
}

export function isTableDivider(value) {
  return /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$/.test(String(value ?? ""));
}

function renderCodePlain(text) {
  let output = "";
  let cursor = 0;
  CODE_TOKEN_PATTERN.lastIndex = 0;
  for (const match of text.matchAll(CODE_TOKEN_PATTERN)) {
    output += text.slice(cursor, match.index);
    if (match[1]) {
      output += color(ANSI.cyan, match[1]);
      output += match[2];
      output += color(ANSI.blue, match[3]);
    } else if (match[4]) {
      output += color(ANSI.cyan, match[4]);
    } else if (match[5]) {
      output += color(ANSI.yellow, match[5]);
    } else {
      output += color(ANSI.magenta, match[6]);
    }
    cursor = match.index + match[0].length;
  }
  return output + text.slice(cursor);
}

function findClosing(text, delimiter, start) {
  let cursor = start;
  while (cursor < text.length) {
    const found = text.indexOf(delimiter, cursor);
    if (found < 0) return -1;
    if (found === 0 || text[found - 1] !== "\\") return found;
    cursor = found + delimiter.length;
  }
  return -1;
}

function findStringEnd(text, quote, start) {
  for (let index = start; index < text.length; index += 1) {
    if (text[index] === "\\") {
      index += 1;
      continue;
    }
    if (text[index] === quote) return index;
  }
  return -1;
}
