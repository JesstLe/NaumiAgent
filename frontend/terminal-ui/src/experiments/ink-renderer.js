import React from "react";
import { Box, Text, renderToString } from "ink";

import {
  compactText,
  padRight,
  sanitizeTerminalText,
  truncateAnsi,
} from "../ansi.js";

export const INK_EXPERIMENT_RENDERER = "react-ink-6";

export function renderInkExperimentScreen(state, width, height) {
  const columns = boundedDimension(width, "width", 40, 400);
  const rows = boundedDimension(height, "height", 8, 200);
  const timelineRows = Math.max(1, rows - 3);
  const visibleRows = selectVisibleRows(state, timelineRows);
  const children = [
    React.createElement(
      Text,
      { key: "header", bold: true, color: "cyan", wrap: "truncate-end" },
      "Naumi · Ink 实验 renderer",
    ),
    ...visibleRows.map((presentation, index) => React.createElement(MessageRow, {
      key: presentation.key || `message-${index}`,
      presentation,
    })),
  ];
  while (children.length < timelineRows + 1) {
    children.push(React.createElement(Text, { key: `blank-${children.length}` }, " "));
  }
  children.push(
    React.createElement(
      Text,
      { key: "status", dimColor: true, wrap: "truncate-end" },
      statusText(state),
    ),
    React.createElement(
      Text,
      { key: "composer", color: "green", wrap: "truncate-end" },
      `chat > ${sanitizeTerminalText(state?.input || "")}▌`,
    ),
  );
  const output = renderToString(
    React.createElement(
      Box,
      { flexDirection: "column", width: columns, height: rows, overflow: "hidden" },
      ...children,
    ),
    { columns },
  );
  return normalizeViewport(output, columns, rows);
}

function MessageRow({ presentation }) {
  return React.createElement(
    Text,
    {
      bold: presentation.bold,
      color: presentation.color,
      dimColor: presentation.dim,
      wrap: "truncate-end",
    },
    presentation.text,
  );
}

function presentMessageRows(message, state) {
  const kind = String(message?.kind || "event");
  if (kind === "user") {
    return [{ text: `你  ${oneLine(message.content)}`, color: "blue", bold: true }];
  }
  if (kind === "assistant") {
    return [{ text: `Naumi  ${oneLine(message.content)}`, color: "white" }];
  }
  if (kind === "activity") {
    return [{
      text: `活动 ${oneLine(message.status)} · ${oneLine(message.title)} · ${oneLine(message.details?.join(" · "))}`,
      color: message.status === "error" ? "red" : "cyan",
    }];
  }
  if (kind === "tool") {
    const status = oneLine(message.status || "running");
    return [{
      text: `工具 ${status} ${oneLine(message.name)} ${oneLine(message.primary)} · ${oneLine(message.output)}`,
      color: status === "error" ? "red" : (status === "success" ? "green" : "yellow"),
    }];
  }
  if (kind === "run_activity") {
    const completed = message.status === "completed";
    return [{
      text: `执行过程 · ${completed ? "已完成" : oneLine(message.status)} · ${oneLine(message.phaseLabel)}`,
      color: completed ? "green" : "yellow",
    }];
  }
  if (kind === "completion_receipt") {
    const receipt = message.receipt || {};
    return [{
      text: `完成回执 · ${receipt.outcome === "completed" ? "已完成" : oneLine(receipt.outcome)} · ${oneLine(receipt.summary)}`,
      color: receipt.outcome === "completed" ? "green" : "yellow",
      bold: true,
    }];
  }
  if (kind === "permission") {
    return permissionRows(message.message || message);
  }
  if (kind === "system" && message.title === "tasks") {
    return taskRows(state?.taskPanel);
  }
  return [{
    text: `${oneLine(kind)} · ${oneLine(message?.title || message?.content || message?.status)}`,
    dim: true,
  }];
}

function selectVisibleRows(state, limit) {
  const messages = Array.isArray(state?.messages) ? state.messages : [];
  const rows = messages.flatMap((message, messageIndex) => presentMessageRows(message, state).map(
    (row, rowIndex) => ({
      ...row,
      key: `${String(message?.id || `message-${messageIndex}`)}:${rowIndex}`,
    }),
  ));
  const offset = Math.max(0, Math.trunc(Number(state?.scrollOffset) || 0));
  const boundedOffset = Math.min(offset, Math.max(0, rows.length - 1));
  const end = Math.max(0, rows.length - boundedOffset);
  return rows.slice(Math.max(0, end - limit), end);
}

function permissionRows(payload) {
  const status = oneLine(payload?.status || "needs_confirmation");
  const statusLabel = permissionStatusLabel(status);
  const tool = oneLine(payload?.tool_name || payload?.tool || "tool");
  const terminal = ["denied"].includes(status) ? "red" : "yellow";
  const rows = [
    { text: `${statusLabel} permission: ${tool}`, color: terminal, bold: true },
    { text: `原因: ${oneLine(payload?.reason || "等待用户确认。")}`, dim: true },
  ];
  if (payload?.requires_confirmation !== false) {
    rows.push({ text: "操作: y=允许一次  n=拒绝  g=本会话授权  b/Shift+Tab=全权限", color: "yellow" });
  } else if (payload?.choice) {
    rows.push({ text: `结果: ${permissionChoiceLabel(payload.choice)}`, dim: true });
  }
  return rows;
}

function permissionStatusLabel(status) {
  if (status === "needs_confirmation") return "需要确认";
  if (status === "allowed") return "已允许";
  if (status === "granted") return "本会话已授权";
  if (status === "denied") return "已拒绝";
  if (status === "bypass_enabled") return "bypass";
  return status;
}

function permissionChoiceLabel(choice) {
  if (["allow", "allow_once"].includes(choice)) return "允许";
  if (choice === "deny") return "拒绝";
  if (choice === "grant_session") return "本会话授权";
  if (choice === "bypass") return "bypass";
  return oneLine(choice);
}

function taskRows(taskPanel) {
  const items = Array.isArray(taskPanel?.items) ? taskPanel.items : [];
  const rows = [{
    text: `tasks · 任务面板 · source=${oneLine(taskPanel?.source || "all")} · status=${oneLine(taskPanel?.status || "all")}`,
    color: "cyan",
    bold: true,
  }];
  if (items.length === 0) {
    rows.push({ text: "暂无任务", dim: true });
    return rows;
  }
  for (const item of items.slice(0, 12)) {
    const selected = item.id === taskPanel?.selectedId;
    rows.push({
      text: `${selected ? "●" : "○"} ${oneLine(item.id)} [${oneLine(item.status)}] ${oneLine(item.label)}`,
      color: selected ? "green" : undefined,
      bold: selected,
    });
  }
  return rows;
}

function statusText(state) {
  const mode = oneLine(state?.mode || "default");
  const runtime = state?.running ? "运行中" : "空闲";
  const model = oneLine(state?.status?.model || "-");
  return `mode: ${mode} | 运行: ${runtime} | model: ${model} | renderer: ${INK_EXPERIMENT_RENDERER}`;
}

function oneLine(value) {
  return compactText(sanitizeTerminalText(value).replace(/\s+/g, " "), 500);
}

function normalizeViewport(output, width, height) {
  const lines = String(output).split("\n").slice(0, height).map(
    (line) => padRight(truncateAnsi(line, width), width),
  );
  while (lines.length < height) lines.push(" ".repeat(width));
  return lines;
}

function boundedDimension(value, name, minimum, maximum) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(`${name} 必须是 ${minimum}-${maximum} 的整数`);
  }
  return parsed;
}
