import { ANSI, color, compactText } from "../ansi.js";
import { boxComponent, line, renderComponent } from "./core.js";

export function PermissionCard({ permission }) {
  return {
    render(ctx) {
      return renderPermissionCard(permission, ctx.width, ctx);
    },
  };
}

export function renderPermissionCard(permission, width, ctx = { width }) {
  const payload = permission?.message ?? permission ?? {};
  const status = String(payload.status || "needs_confirmation");
  const tool = String(payload.tool_name || payload.tool || "tool");
  const reason = compactText(payload.reason || payload.message || "等待用户确认。", 180);
  const statusStyle = permissionStatusStyle(status);
  const children = [
    line(`${color(statusStyle, statusLabel(status))} permission: ${tool}`),
    line(color(ANSI.dim, `原因: ${reason}`)),
  ];
  if (payload.requires_confirmation) {
    const grant = payload.choices?.includes("grant_session") ? "  g=本会话授权" : "";
    children.push(line(color(
      ANSI.yellow,
      `操作: y=允许一次  n=拒绝${grant}  b/Shift+Tab=全权限`,
    )));
  } else if (payload.choice) {
    children.push(line(color(ANSI.dim, `结果: ${choiceLabel(payload.choice)}`)));
  }
  return renderComponent(boxComponent("permission", children), ctx);
}

function permissionStatusStyle(status) {
  if (["allowed", "granted", "bypass_enabled"].includes(status)) return ANSI.green;
  if (status === "denied") return ANSI.red;
  return ANSI.yellow;
}

function statusLabel(status) {
  if (status === "needs_confirmation") return "需要确认";
  if (status === "allowed") return "已允许";
  if (status === "granted") return "本会话已授权";
  if (status === "denied") return "已拒绝";
  if (status === "bypass_enabled") return "bypass";
  return status;
}

function choiceLabel(choice) {
  if (choice === "allow" || choice === "allow_once") return "允许";
  if (choice === "deny") return "拒绝";
  if (choice === "grant_session") return "本会话授权";
  if (choice === "bypass") return "bypass";
  return String(choice);
}
