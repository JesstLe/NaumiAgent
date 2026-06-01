import { ANSI, color, visibleWidth, wrapAnsiLine } from "../ansi.js";

export function createRenderContext({ width, env = {}, state = null }) {
  return { width, env, state };
}

export function stack(children, ctx, { gap = 0 } = {}) {
  const lines = [];
  for (const child of children) {
    const rendered = renderComponent(child, ctx);
    if (!rendered.length) continue;
    if (gap && lines.length) {
      for (let i = 0; i < gap; i += 1) lines.push("");
    }
    lines.push(...rendered);
  }
  return lines;
}

export function renderComponent(component, ctx) {
  if (component == null || component === false) return [];
  if (typeof component === "string") return wrapAnsiLine(component, ctx.width);
  if (Array.isArray(component)) return component.flatMap((child) => renderComponent(child, ctx));
  if (typeof component === "function") return component(ctx);
  if (typeof component.render === "function") return component.render(ctx);
  if (Array.isArray(component.lines)) return component.lines.flatMap((line) => wrapAnsiLine(line, ctx.width));
  return wrapAnsiLine(String(component), ctx.width);
}

export function line(text) {
  return { lines: [text] };
}

export function textBlock(lines) {
  return { lines };
}

export function boxComponent(title, children) {
  return {
    render(ctx) {
      return boxLines(title, stack(Array.isArray(children) ? children : [children], {
        ...ctx,
        width: Math.max(1, ctx.width - 6),
      }), ctx.width);
    },
  };
}

export function boxLines(title, inner, width) {
  const boxWidth = Math.max(30, width - 2);
  const top = `+ ${title} ${"-".repeat(Math.max(0, boxWidth - visibleWidth(title) - 4))}+`;
  const bottom = `+${"-".repeat(Math.max(0, boxWidth - 1))}+`;
  const body = inner.flatMap((line) => wrapAnsiLine(line, boxWidth - 4)).map((line) => {
    const rawPad = Math.max(0, boxWidth - 4 - visibleWidth(line));
    return `| ${line}${" ".repeat(rawPad)} |`;
  });
  return ["", color(ANSI.blue, top), ...body, color(ANSI.blue, bottom)];
}
