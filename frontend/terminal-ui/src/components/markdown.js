import { ANSI, color, looksLikeDiff, sanitizeTerminalText, wrapAnsiLine } from "../ansi.js";
import { CODE_FOLD_VISIBLE_LINES, DIFF_FOLD_VISIBLE_LINES, foldLines } from "./folds.js";
import {
  isTableDivider,
  renderSemanticCodeLine,
  renderSemanticDiffLine,
  renderSemanticMarkdownLine,
} from "./semantic-text.js";

export function MarkdownExcerpt({ text, foldKey = "" }) {
  return {
    render(ctx) {
      return renderMarkdownExcerpt(text, ctx.width, { foldKey, folds: ctx.state?.folds });
    },
  };
}

export function ToolOutput({ text, foldKey = "", format = "text", language = "" }) {
  return {
    render(ctx) {
      return renderToolOutput(text, ctx.width, {
        foldKey,
        folds: ctx.state?.folds,
        format,
        language,
      });
    },
  };
}

export function renderToolOutput(text, width, options = {}) {
  const normalized = normalizeToolOutputText(text, options);
  if (options.format === "diff" || looksLikeDiff(normalized)) {
    const folded = foldLines(text.split("\n"), {
      folds: options.folds,
      key: options.foldKey,
      visibleLines: DIFF_FOLD_VISIBLE_LINES,
      hiddenLabel: " diff",
    });
    const lines = folded.lines.map(renderSemanticDiffLine);
    if (folded.notice) {
      lines.push(color(ANSI.dim, folded.notice));
    }
    return lines;
  }
  return renderMarkdownExcerpt(normalized, width, options).slice(0, DIFF_FOLD_VISIBLE_LINES);
}

function normalizeToolOutputText(text, options = {}) {
  const raw = String(text ?? "");
  if (!raw || raw.includes("```")) return raw;
  if (options.format === "code") {
    const language = String(options.language || "text").trim() || "text";
    return `\`\`\`${language}\n${raw}\n\`\`\``;
  }
  if (options.format === "diff") {
    return `\`\`\`diff\n${raw}\n\`\`\``;
  }
  return raw;
}

export function renderMarkdownExcerpt(text, width, options = {}) {
  const lines = [];
  const raw = sanitizeTerminalText(text).split("\n");
  let inCode = false;
  let codeLanguage = "";
  let mathBlockClose = "";
  for (let index = 0; index < raw.length; index += 1) {
    const line = raw[index];
    if (line.startsWith("```")) {
      inCode = !inCode;
      codeLanguage = inCode ? line.slice(3).trim().toLowerCase() : "";
      lines.push(color(ANSI.dim, line));
      continue;
    }
    if (inCode) {
      lines.push(codeLanguage === "diff"
        ? renderSemanticDiffLine(line)
        : renderSemanticCodeLine(line, codeLanguage));
      continue;
    }
    if (mathBlockClose) {
      lines.push(color(ANSI.magenta, line));
      if (line.trim() === mathBlockClose) mathBlockClose = "";
      continue;
    }
    const mathClose = line.trim() === "$$" ? "$$" : line.trim() === "\\[" ? "\\]" : "";
    if (mathClose && raw.slice(index + 1).some((candidate) => candidate.trim() === mathClose)) {
      mathBlockClose = mathClose;
      lines.push(color(ANSI.magenta, line));
      continue;
    }
    lines.push(renderSemanticMarkdownLine(line, {
      isTableHeader: index + 1 < raw.length && isTableDivider(raw[index + 1]),
    }));
  }
  return foldMarkdownCodeBlocks(lines, width, options);
}

function foldMarkdownCodeBlocks(lines, width, options) {
  const result = [];
  let inCode = false;
  let codeBuffer = [];
  let codeBlockIndex = 0;

  const flushCode = () => {
    if (!codeBuffer.length) return;
    const key = options.foldKey ? `${options.foldKey}:code:${codeBlockIndex}` : "";
    const folded = foldLines(codeBuffer, {
      folds: options.folds,
      key,
      visibleLines: CODE_FOLD_VISIBLE_LINES,
      hiddenLabel: "代码",
    });
    result.push(...folded.lines);
    if (folded.notice) {
      result.push(color(ANSI.dim, folded.notice));
    }
    codeBuffer = [];
    codeBlockIndex += 1;
  };

  for (const line of lines) {
    if (String(line).includes("```")) {
      if (inCode) {
        flushCode();
        result.push(line);
        inCode = false;
      } else {
        result.push(line);
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeBuffer.push(line);
    } else {
      result.push(line);
    }
  }
  flushCode();
  return result.flatMap((line) => wrapAnsiLine(line, width));
}
