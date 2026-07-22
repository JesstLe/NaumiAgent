import assert from "node:assert/strict";
import test from "node:test";

import { stripAnsi } from "../src/ansi.js";
import {
  acceptCommandQuickOpen,
  appendCommandQuickOpenQuery,
  backspaceCommandQuickOpenQuery,
  closeCommandQuickOpen,
  getCommandQuickOpenItems,
  moveCommandQuickOpenSelection,
  openCommandQuickOpen,
  searchCommandEntries,
} from "../src/command-quick-open.js";
import { renderCommandQuickOpenPage } from "../src/components/command-quick-open-page.js";
import { renderScreen } from "../src/render.js";
import {
  createInitialState,
  handleInteractionRequest,
  handlePermissionRequest,
} from "../src/state.js";

const COMMANDS = [
  command("/help", { aliases: ["/h"], risk: "read_only", description: "显示帮助" }),
  command("/world", { risk: "tool_execution", description: "世界模型审计" }),
  command("/write", {
    risk: "workspace_write",
    description: "写入工作区文件",
    syntax: "<path> <content>",
  }),
  command("/delete", { risk: "destructive", description: "删除会话" }),
];

test("command QuickOpen ranks canonical alias fuzzy and localized risk metadata", () => {
  assert.equal(searchCommandEntries(COMMANDS, "/h")[0].command, "/help");
  assert.equal(searchCommandEntries(COMMANDS, "wr")[0].command, "/write");
  assert.deepEqual(
    searchCommandEntries(COMMANDS, "工作区写入").map((item) => item.command),
    ["/write"],
  );
});

test("command QuickOpen cancel preserves draft and accept only fills composer", () => {
  const state = createInitialState();
  state.slashCommands = COMMANDS;
  state.input = "保留草稿";
  state.inputCursor = 2;

  openCommandQuickOpen(state);
  appendCommandQuickOpenQuery(state, "wr");
  assert.equal(getCommandQuickOpenItems(state)[0].command, "/write");
  assert.equal(closeCommandQuickOpen(state), true);
  assert.equal(state.input, "保留草稿");
  assert.equal(state.inputCursor, 2);

  openCommandQuickOpen(state);
  appendCommandQuickOpenQuery(state, "del");
  assert.equal(moveCommandQuickOpenSelection(state, "next"), true);
  assert.equal(moveCommandQuickOpenSelection(state, "previous"), true);
  assert.equal(acceptCommandQuickOpen(state), true);
  assert.equal(state.input, "/delete");
  assert.equal(state.commandQuickOpen.open, false);
});

test("command QuickOpen query backspace respects grapheme clusters", () => {
  const state = createInitialState();
  openCommandQuickOpen(state);
  appendCommandQuickOpenQuery(state, "e\u0301");
  assert.equal(backspaceCommandQuickOpenQuery(state), true);
  assert.equal(state.commandQuickOpen.query, "");
});

test("command QuickOpen page renders textual safety and no-auto-execute contract", () => {
  const state = createInitialState();
  state.slashCommands = COMMANDS;
  openCommandQuickOpen(state);
  appendCommandQuickOpenQuery(state, "write");

  const rendered = stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n"));
  assert.match(rendered, /命令 QuickOpen/);
  assert.match(rendered, /\/write <path> <content>/);
  assert.match(rendered, /工作区写入/);
  assert.match(rendered, /不会自动发送或执行/);

  state.route = { name: "workbench", originAnchor: null };
  const screen = stripAnsi(renderScreen(state, 100, 30).join("\n"));
  assert.equal(renderScreen(state, 100, 30).length, 30);
  assert.match(screen, /命令 QuickOpen/);
  assert.doesNotMatch(screen, /Workbench 权威视图/);
});

test("blocking permission or interaction closes command QuickOpen", () => {
  const permissionState = createInitialState();
  openCommandQuickOpen(permissionState);
  handlePermissionRequest(permissionState, {
    request_id: "perm-1",
    payload: { tool_name: "bash_run", choices: ["allow_once", "deny"] },
  });
  assert.equal(permissionState.commandQuickOpen.open, false);
  assert.equal(permissionState.permission.requestId, "perm-1");

  const interactionState = createInitialState();
  openCommandQuickOpen(interactionState);
  handleInteractionRequest(interactionState, {
    request_id: "ask-1",
    payload: {
      request_id: "ask-1",
      question: "请选择",
      options: [{ label: "继续", value: "continue" }],
    },
  });
  assert.equal(interactionState.commandQuickOpen.open, false);
  assert.equal(interactionState.interaction.requestId, "ask-1");
});

function command(commandName, { aliases = [], risk, description, syntax = "" }) {
  return {
    command: commandName,
    aliases,
    description,
    schema_version: 1,
    category: "control",
    source: "shared_runtime",
    readonly: risk === "read_only",
    permission_risk: risk,
    arguments: {
      takes_arguments: Boolean(syntax),
      syntax,
      required: syntax.startsWith("<"),
    },
  };
}
