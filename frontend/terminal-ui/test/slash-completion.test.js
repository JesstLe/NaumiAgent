import test from "node:test";
import assert from "node:assert/strict";
import {
  acceptSlashCompletion,
  dismissSlashCompletion,
  getSlashCompletionItems,
  isSlashCompletionOpen,
  moveSlashCompletionSelection,
  syncSlashCompletion,
} from "../src/slash-completion.js";
import { setInputText } from "../src/input-buffer.js";
import { createInitialState, getSlashCommandCompletions } from "../src/state.js";

test("slash completion closes for every whitespace argument separator", () => {
  assert.equal(getSlashCommandCompletions("/doctor ").length, 0);
  assert.equal(getSlashCommandCompletions("/doctor\t").length, 0);
  assert.equal(getSlashCommandCompletions("/doctor\n").length, 0);
});

test("slash completion selection wraps in both directions", () => {
  const state = createInitialState();
  state.slashCommands = [
    { command: "/debug", description: "debug" },
    { command: "/delete", description: "delete" },
    { command: "/doctor", description: "doctor" },
  ];
  setInputText(state, "/d");
  syncSlashCompletion(state);
  const count = getSlashCompletionItems(state).length;

  assert(count >= 3);
  assert.equal(getSlashCompletionItems(state)[0].selected, true);
  moveSlashCompletionSelection(state, "previous");
  assert.equal(state.slashCompletion.selectedIndex, count - 1);
  moveSlashCompletionSelection(state, "next");
  assert.equal(state.slashCompletion.selectedIndex, 0);
});

test("accepting a slash candidate edits the composer without sending", () => {
  const state = createInitialState();
  state.slashCommands = [
    { command: "/debug", description: "debug" },
    { command: "/doctor", description: "doctor" },
  ];
  setInputText(state, "/do");
  syncSlashCompletion(state);

  assert.equal(acceptSlashCompletion(state), true);
  assert.equal(state.input, "/doctor");
  assert.equal(isSlashCompletionOpen(state), false);
  assert.deepEqual(state.messages, []);
});

test("slash completion preserves authoritative command safety metadata", () => {
  const state = createInitialState();
  state.slashCommands = [{
    schema_version: 1,
    command: "/write",
    aliases: [],
    description: "写入文件",
    category: "basic",
    source: "shared_runtime",
    readonly: false,
    permission_risk: "workspace_write",
    arguments: { takes_arguments: true, syntax: "<path> <content>", required: true },
  }];
  setInputText(state, "/wri");

  const [item] = getSlashCompletionItems(state).filter(
    (candidate) => candidate.command === "/write",
  );

  assert.equal(item.permission_risk, "workspace_write");
  assert.equal(item.readonly, false);
  assert.equal(item.source, "shared_runtime");
  assert.deepEqual(item.arguments, {
    takes_arguments: true,
    syntax: "<path> <content>",
    required: true,
  });
});

test("slash completion drops malformed authoritative safety metadata", () => {
  const malformed = {
    schema_version: 1,
    command: "/unsafe",
    aliases: ["not-a-command"],
    description: "伪装为只读",
    category: "control",
    source: "shared_runtime",
    readonly: true,
    permission_risk: "tool_execution",
    arguments: { takes_arguments: false, syntax: "", required: false },
  };

  assert.equal(
    getSlashCommandCompletions("/", [malformed]).some(
      (item) => item.command === "/unsafe",
    ),
    false,
  );
});

test("an exact canonical command falls through to normal submission", () => {
  const state = createInitialState();
  setInputText(state, "/retry");
  syncSlashCompletion(state);

  assert.equal(acceptSlashCompletion(state), false);
  assert.equal(state.input, "/retry");
  assert.equal(isSlashCompletionOpen(state), true);
});

test("dismissal preserves input and editing reopens at the first candidate", () => {
  const state = createInitialState();
  setInputText(state, "/d");
  syncSlashCompletion(state);
  moveSlashCompletionSelection(state, "next");

  assert.equal(dismissSlashCompletion(state), true);
  assert.equal(state.input, "/d");
  assert.equal(isSlashCompletionOpen(state), false);

  setInputText(state, "/do");
  syncSlashCompletion(state);
  assert.equal(isSlashCompletionOpen(state), true);
  assert.equal(state.slashCompletion.selectedIndex, 0);
});
