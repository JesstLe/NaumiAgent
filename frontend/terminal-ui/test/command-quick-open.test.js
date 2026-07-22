import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

import { stripAnsi } from "../src/ansi.js";
import {
  acceptCommandQuickOpen,
  appendCommandQuickOpenQuery,
  backspaceCommandQuickOpenQuery,
  closeCommandQuickOpen,
  getCommandQuickOpenItems,
  moveCommandQuickOpenSelection,
  openCommandQuickOpen,
  recordRecentCommand,
  searchCommandEntries,
  searchTaskEntries,
  searchSessionEntries,
  sessionTemplate,
  switchCommandQuickOpenProvider,
} from "../src/command-quick-open.js";
import { renderCommandQuickOpenPage } from "../src/components/command-quick-open-page.js";
import { renderScreen } from "../src/render.js";
import {
  createInitialState,
  handleSubmitText,
  handleInteractionRequest,
  handlePermissionRequest,
  reduceServerEvent,
} from "../src/state.js";
import { normalizeServerRecord } from "../src/protocol.js";

const recencyGolden = JSON.parse(readFileSync(new URL(
  "../../../tests/fixtures/ui14/command-recency-golden.json",
  import.meta.url,
), "utf8"));
const taskGolden = JSON.parse(readFileSync(new URL(
  "../../../tests/fixtures/ui14/task-quick-open-golden.json",
  import.meta.url,
), "utf8"));

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

test("command QuickOpen recent ranking matches shared privacy-safe golden", () => {
  let recent = [];
  for (const submission of recencyGolden.submissions) {
    recent = recordRecentCommand(COMMANDS, recent, submission);
  }
  assert.deepEqual(recent, recencyGolden.expected_recent_commands);
  assert.doesNotMatch(JSON.stringify(recent), /private/);
  assert.deepEqual(
    searchCommandEntries(COMMANDS, "", 10, recent)
      .slice(0, 2).map((item) => item.command),
    recencyGolden.empty_query_order_prefix,
  );
  assert.deepEqual(
    searchCommandEntries(COMMANDS, recencyGolden.query, 10, recent)
      .slice(0, 1).map((item) => item.command),
    recencyGolden.query_order_prefix,
  );
});

test("submitted known commands update bounded recency while unknown text does not", () => {
  const state = createInitialState();
  state.slashCommands = COMMANDS;
  handleSubmitText(state, "/h", () => {});
  handleSubmitText(state, "/write secret", () => {});
  handleSubmitText(state, "/unknown secret", () => {});

  assert.deepEqual(state.commandQuickOpen.recentCommands, ["/write", "/help"]);
  openCommandQuickOpen(state);
  assert.equal(getCommandQuickOpenItems(state)[0].command, "/write");
  const rendered = stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n"));
  assert.match(rendered, /\/write.*最近/);
  assert.throws(
    () => recordRecentCommand(COMMANDS, [], "/help", 21),
    /limit/,
  );
});

test("task QuickOpen consumes correlated typed snapshot without opening task panel", () => {
  const state = createInitialState();
  state.input = "保留草稿";
  openCommandQuickOpen(state);
  assert.equal(switchCommandQuickOpenProvider(state, () => "quick-task-1"), "tasks");
  assert.equal(state.commandQuickOpen.taskLoading, true);

  const record = normalizeServerRecord({
    id: "server-task-1",
    request_id: "quick-task-1",
    type: "tasks/snapshot",
    version: 1,
    payload: taskGolden,
  });
  reduceServerEvent(state, record);

  assert.equal(state.commandQuickOpen.taskLoaded, true);
  assert.equal(state.taskPanel.snapshot, null);
  assert.equal(state.messages.length, 0);
  assert.deepEqual(
    getCommandQuickOpenItems(state).map((item) => item.task_id),
    taskGolden.expected_empty_order,
  );
  assert.deepEqual(
    searchTaskEntries(taskGolden.items, taskGolden.expected_localized_query)
      .map((item) => item.task_id),
    [taskGolden.expected_localized_result],
  );
  appendCommandQuickOpenQuery(state, taskGolden.expected_localized_query);
  const rendered = stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n"));
  assert.match(rendered, /任务 QuickOpen/);
  assert.match(rendered, /子智能体/);
  assert.match(rendered, /不会自动发送或执行/);
  assert.equal(acceptCommandQuickOpen(state), true);
  assert.equal(state.input, taskGolden.expected_template);
  assert.equal(state.commandQuickOpen.open, false);

  openCommandQuickOpen(state);
  assert.equal(state.commandQuickOpen.taskLoaded, false);
  assert.equal(state.commandQuickOpen.taskItems.length, 0);
  assert.equal(switchCommandQuickOpenProvider(state, () => "quick-task-2"), "tasks");
  assert.equal(state.commandQuickOpen.taskRequestId, "quick-task-2");
});

test("task QuickOpen keeps correlated load errors inside the overlay", () => {
  const state = createInitialState();
  openCommandQuickOpen(state);
  switchCommandQuickOpenProvider(state, () => "quick-task-error");

  reduceServerEvent(state, {
    id: "server-error",
    request_id: "quick-task-error",
    type: "error",
    payload: { code: "task_panel_failed", message: "任务快照暂不可用" },
  });

  assert.equal(state.commandQuickOpen.taskLoading, false);
  assert.match(state.commandQuickOpen.taskError, /暂不可用/);
  assert.equal(state.messages.length, 0);
  assert.match(
    stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n")),
    /任务快照暂不可用/,
  );
});

test("session QuickOpen consumes correlated workspace snapshot and only fills load", () => {
  const state = createInitialState();
  openCommandQuickOpen(state);
  const requests = { tasks: () => "task-request", sessions: () => "session-request" };
  assert.equal(switchCommandQuickOpenProvider(state, requests), "tasks");
  assert.equal(switchCommandQuickOpenProvider(state, requests), "sessions");
  assert.equal(state.commandQuickOpen.sessionLoading, true);

  reduceServerEvent(state, normalizeServerRecord({
    id: "server-sessions",
    request_id: "session-request",
    type: "sessions/list",
    payload: {
      schema_version: 1, generated_at: "2026-07-22T08:00:00+00:00",
      scope: "workspace", page: 1, page_size: 100, total: 2, query: "",
      items: [
        { session_id: "older", title: "旧会话", model: "m", updated_at: "2026-07-20", message_count: 2, user_message_count: 1, git_branch: "main", is_current: false, resumable: true },
        { session_id: "current", title: "当前会话", model: "m", updated_at: "2026-07-19", message_count: 2, user_message_count: 1, git_branch: "main", is_current: true, resumable: true },
      ], warnings: [],
    },
  }));

  assert.deepEqual(searchSessionEntries(state.commandQuickOpen.sessionItems, "当前").map((item) => item.session_id), ["current"]);
  assert.equal(getCommandQuickOpenItems(state)[0].session_id, "current");
  assert.match(stripAnsi(renderCommandQuickOpenPage(state, 100, 24).join("\n")), /会话 QuickOpen/);
  assert.equal(sessionTemplate(getCommandQuickOpenItems(state)[0]), "/load current");
  assert.equal(acceptCommandQuickOpen(state), true);
  assert.equal(state.input, "/load current");
  assert.equal(state.messages.length, 0);
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
