import { ANSI, color, colorCodeLine, colorDiffLine, looksLikeDiff, wrapAnsiLine } from "../ansi.js";
import { CODE_FOLD_VISIBLE_LINES, DIFF_FOLD_VISIBLE_LINES, foldLines } from "./folds.js";

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
    const lines = folded.lines.map(colorDiffLine);
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
  const raw = String(text ?? "").split("\n");
  let inCode = false;
  let codeLineCount = 0;
  let omitted = 0;
  for (const line of raw) {
    if (line.startsWith("```")) {
      if (inCode && omitted) {
        lines.push(color(ANSI.dim, `... 已隐藏 ${omitted} 行代码`));
        omitted = 0;
      }
      inCode = !inCode;
      codeLineCount = 0;
      lines.push(color(ANSI.dim, line));
      continue;
    }
    if (inCode) {
      lines.push(colorCodeLine(line));
      codeLineCount += 1;
      continue;
    }
    lines.push(line);
  }
  if (omitted) {
    lines.push(color(ANSI.dim, `... 已隐藏 ${omitted} 行代码`));
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
