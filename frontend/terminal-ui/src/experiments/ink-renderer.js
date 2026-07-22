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
  const visibleMessages = selectVisibleMessages(state, timelineRows);
  const children = [
    React.createElement(
      Text,
      { key: "header", bold: true, color: "cyan", wrap: "truncate-end" },
      "Naumi · Ink 实验 renderer",
    ),
    ...visibleMessages.map((message, index) => React.createElement(MessageRow, {
      key: String(message?.id || `message-${index}`),
      message,
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

function MessageRow({ message }) {
  const presentation = presentMessage(message);
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

function presentMessage(message) {
  const kind = String(message?.kind || "event");
  if (kind === "user") {
    return { text: `你  ${oneLine(message.content)}`, color: "blue", bold: true };
  }
  if (kind === "assistant") {
    return { text: `Naumi  ${oneLine(message.content)}`, color: "white" };
  }
  if (kind === "activity") {
    return {
      text: `活动 ${oneLine(message.status)} · ${oneLine(message.title)} · ${oneLine(message.details?.join(" · "))}`,
      color: message.status === "error" ? "red" : "cyan",
    };
  }
  if (kind === "tool") {
    const status = oneLine(message.status || "running");
    return {
      text: `工具 ${status} ${oneLine(message.name)} ${oneLine(message.primary)} · ${oneLine(message.output)}`,
      color: status === "error" ? "red" : (status === "success" ? "green" : "yellow"),
    };
  }
  if (kind === "run_activity") {
    const completed = message.status === "completed";
    return {
      text: `执行过程 · ${completed ? "已完成" : oneLine(message.status)} · ${oneLine(message.phaseLabel)}`,
      color: completed ? "green" : "yellow",
    };
  }
  if (kind === "completion_receipt") {
    const receipt = message.receipt || {};
    return {
      text: `完成回执 · ${receipt.outcome === "completed" ? "已完成" : oneLine(receipt.outcome)} · ${oneLine(receipt.summary)}`,
      color: receipt.outcome === "completed" ? "green" : "yellow",
      bold: true,
    };
  }
  if (kind === "permission") {
    return {
      text: `权限确认 · ${oneLine(message.toolName || message.title)} · ${oneLine(message.reason)}`,
      color: "yellow",
      bold: true,
    };
  }
  return {
    text: `${oneLine(kind)} · ${oneLine(message?.title || message?.content || message?.status)}`,
    dim: true,
  };
}

function selectVisibleMessages(state, limit) {
  const messages = Array.isArray(state?.messages) ? state.messages : [];
  const offset = Math.max(0, Math.trunc(Number(state?.scrollOffset) || 0));
  const end = Math.max(0, messages.length - offset);
  return messages.slice(Math.max(0, end - limit), end);
}

function statusText(state) {
  const mode = oneLine(state?.mode || "default");
  const runtime = state?.running ? "运行中" : "空闲";
  return `mode: ${mode} | 运行: ${runtime} | renderer: ${INK_EXPERIMENT_RENDERER}`;
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
