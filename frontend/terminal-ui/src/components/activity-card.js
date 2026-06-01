import { ANSI, color, compactText } from "../ansi.js";
import { boxComponent, line, renderComponent } from "./core.js";

export function ActivityCard({ activity }) {
  return {
    render(ctx) {
      return renderActivityCard(activity, ctx.width, ctx);
    },
  };
}

export function renderActivityCard(activity, width, ctx = { width }) {
  const status = activity.status === "done" ? color(ANSI.green, "done") : color(ANSI.cyan, "running");
  const title = compactText(activity.title || "正在处理", 120);
  const children = [line(`${status} ${title}`)];

  for (const detail of activity.details ?? []) {
    if (detail) children.push(line(color(ANSI.dim, compactText(detail, 160))));
  }

  return renderComponent(boxComponent("activity", children), ctx);
}
