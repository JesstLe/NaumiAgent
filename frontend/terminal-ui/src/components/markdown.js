import { ANSI, color, colorCodeLine, colorDiffLine, looksLikeDiff, wrapAnsiLine } from "../ansi.js";

export function MarkdownExcerpt({ text }) {
  return {
    render(ctx) {
      return renderMarkdownExcerpt(text, ctx.width);
    },
  };
}

export function ToolOutput({ text }) {
  return {
    render(ctx) {
      return renderToolOutput(text, ctx.width);
    },
  };
}

export function renderToolOutput(text, width) {
  if (looksLikeDiff(text)) {
    return text.split("\n").slice(0, 60).map(colorDiffLine);
  }
  return renderMarkdownExcerpt(text, width).slice(0, 60);
}

export function renderMarkdownExcerpt(text, width) {
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
      if (codeLineCount < 40) {
        lines.push(colorCodeLine(line));
      } else {
        omitted += 1;
      }
      codeLineCount += 1;
      continue;
    }
    lines.push(line);
  }
  if (omitted) {
    lines.push(color(ANSI.dim, `... 已隐藏 ${omitted} 行代码`));
  }
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}
