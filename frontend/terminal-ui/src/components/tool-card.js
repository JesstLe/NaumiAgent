import { ANSI, color } from "../ansi.js";
import { boxComponent, line, renderComponent } from "./core.js";
import { ToolOutput } from "./markdown.js";

export function ToolCard({ tool }) {
  return {
    render(ctx) {
      return renderToolCard(tool, ctx.width, ctx);
    },
  };
}

export function renderToolCard(tool, width, ctx = { width }) {
  const title = `${tool.name}${tool.primary ? ` ${tool.primary}` : ""}`;
  const statusStyle = tool.status === "success" ? ANSI.green : tool.status === "running" ? ANSI.cyan : ANSI.red;
  const titleLine = `${color(statusStyle, tool.status === "running" ? "running" : tool.status)} ${title}`;
  const output = tool.output ? ToolOutput({ text: tool.output, foldKey: `tool:${tool.callId || tool.id || tool.name}` }) : null;
  const children = [line(titleLine), output];
  if (tool.outputLength > (tool.output?.length ?? 0)) {
    children.push(line(color(ANSI.dim, `... 已截断，完整输出 ${tool.outputLength} 字符`)));
  }
  return renderComponent(boxComponent("tool", children), ctx);
}
