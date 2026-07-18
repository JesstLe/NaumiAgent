import { ANSI, color, compactText } from "../ansi.js";
import { boxComponent, line, renderComponent } from "./core.js";

export function InteractionCard({ interaction }) {
  return {
    render(ctx) {
      return renderInteractionCard(interaction, ctx.width, ctx);
    },
  };
}

export function renderInteractionCard(interaction, width, ctx = { width }) {
  const payload = interaction?.message ?? interaction ?? {};
  const answered = payload.status === "answered";
  const expired = payload.status === "expired";
  const queued = payload.status === "queued";
  const cancelled = payload.status === "cancelled";
  let statusLabel = "等待你的选择";
  if (answered) statusLabel = "已回答";
  else if (expired) statusLabel = "已超时";
  else if (queued) statusLabel = "排队等待";
  else if (cancelled) statusLabel = "已取消";
  let statusStyle = ANSI.yellow;
  if (answered) statusStyle = ANSI.green;
  else if (cancelled || expired) statusStyle = ANSI.red;
  const children = [
    line(color(statusStyle, statusLabel)),
    line(color(ANSI.cyan, compactText(payload.header || "需要确认", 80))),
    line(compactText(payload.question || "请选择一个选项。", 500)),
  ];
  if (answered) {
    const answer = payload.kind === "custom"
      ? payload.custom_text
      : payload.label || payload.value;
    children.push(line(color(ANSI.green, `回答: ${compactText(answer || "-", 500)}`)));
  } else if (expired) {
    children.push(line(color(ANSI.red, compactText(payload.reason || "等待回答超时。", 300))));
  } else if (!cancelled) {
    for (const [index, option] of (payload.options ?? []).entries()) {
      const description = option.description ? ` · ${compactText(option.description, 220)}` : "";
      children.push(line(color(ANSI.dim, `${index + 1}. ${compactText(option.label, 80)}${description}`)));
    }
    if (payload.allow_custom) {
      children.push(line(color(ANSI.dim, `${(payload.options?.length ?? 0) + 1}. ${payload.custom_label || "其他"}`)));
    }
  }
  return renderComponent(boxComponent("用户交互", children), ctx);
}
