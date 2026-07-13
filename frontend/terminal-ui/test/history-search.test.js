import test from "node:test";
import assert from "node:assert/strict";
import {
  acceptHistorySearch,
  appendHistorySearchQuery,
  backspaceHistorySearchQuery,
  cancelHistorySearch,
  cycleHistorySearch,
  moveHistorySearchSelection,
  openHistorySearch,
} from "../src/history-search.js";
import { getInputCursor, setInputText } from "../src/input-buffer.js";
import { createInitialState } from "../src/state.js";

test("history search returns newest unique Unicode and multiline matches", () => {
  const state = createInitialState();
  state.inputHistory = [
    "检查 API",
    "修复测试\n然后提交",
    "CHECK api latency",
    "检查 API",
  ];

  openHistorySearch(state);
  appendHistorySearchQuery(state, "api");

  assert.equal(state.historySearch.open, true);
  assert.equal(state.historySearch.query, "api");
  assert.deepEqual(state.historySearch.matches, ["检查 API", "CHECK api latency"]);
  assert.equal(state.historySearch.selectedIndex, 0);
});

test("history search cycles older results and arrows move without overflow", () => {
  const state = createInitialState();
  state.inputHistory = ["first", "second", "third"];
  openHistorySearch(state);

  cycleHistorySearch(state);
  assert.equal(state.historySearch.selectedIndex, 1);
  cycleHistorySearch(state);
  assert.equal(state.historySearch.selectedIndex, 2);
  cycleHistorySearch(state);
  assert.equal(state.historySearch.selectedIndex, 0);

  moveHistorySearchSelection(state, "newer");
  assert.equal(state.historySearch.selectedIndex, 0);
  moveHistorySearchSelection(state, "older");
  assert.equal(state.historySearch.selectedIndex, 1);
});

test("accepting history replaces the composer but does not imply submission", () => {
  const state = createInitialState();
  state.inputHistory = ["旧问题", "修复测试\n运行验证"];
  setInputText(state, "尚未发送的草稿", 2);

  openHistorySearch(state);
  appendHistorySearchQuery(state, "修复");

  assert.equal(acceptHistorySearch(state), true);
  assert.equal(state.historySearch.open, false);
  assert.equal(state.input, "修复测试\n运行验证");
  assert.equal(getInputCursor(state), Array.from("修复测试\n运行验证").length);
  assert.deepEqual(state.messages, []);
});

test("cancel restores the exact draft and grapheme cursor", () => {
  const state = createInitialState();
  setInputText(state, "A👩‍💻B", 2);
  openHistorySearch(state);
  appendHistorySearchQuery(state, "unused");

  assert.equal(cancelHistorySearch(state), true);
  assert.equal(state.input, "A👩‍💻B");
  assert.equal(getInputCursor(state), 2);
  assert.equal(state.historySearch.open, false);
});

test("query editing recovers from an empty result set", () => {
  const state = createInitialState();
  state.inputHistory = ["alpha", "beta"];
  openHistorySearch(state);

  appendHistorySearchQuery(state, "z");
  assert.deepEqual(state.historySearch.matches, []);
  assert.equal(acceptHistorySearch(state), false);

  assert.equal(backspaceHistorySearchQuery(state), true);
  assert.deepEqual(state.historySearch.matches, ["beta", "alpha"]);
  assert.equal(backspaceHistorySearchQuery(state), false);
});
