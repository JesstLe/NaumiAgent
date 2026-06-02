import test from "node:test";
import assert from "node:assert/strict";
import {
  INPUT_KEYS,
  backspaceInput,
  clearInput,
  deleteInputForward,
  getInputCursor,
  insertInputText,
  moveInputCursor,
  navigateInputHistory,
  rememberSubmittedInput,
  renderInputWithCursor,
  setInputText,
  splitInputChunk,
} from "../src/input-buffer.js";
import { createInitialState } from "../src/state.js";

test("input tokenizer preserves CSI keys and batches printable paste chunks", () => {
  assert.deepEqual(splitInputChunk(`你好${INPUT_KEYS.left}abc${INPUT_KEYS.delete}\n`), [
    "你好",
    INPUT_KEYS.left,
    "abc",
    INPUT_KEYS.delete,
    "\n",
  ]);
  assert.deepEqual(splitInputChunk(`${INPUT_KEYS.shiftTab}${INPUT_KEYS.pageUp}${INPUT_KEYS.pageDown}`), [
    INPUT_KEYS.shiftTab,
    INPUT_KEYS.pageUp,
    INPUT_KEYS.pageDown,
  ]);
  assert.deepEqual(splitInputChunk(`a${INPUT_KEYS.upAlt}b${INPUT_KEYS.downAlt}`), [
    "a",
    INPUT_KEYS.upAlt,
    "b",
    INPUT_KEYS.downAlt,
  ]);
  assert.deepEqual(splitInputChunk(`${INPUT_KEYS.homeSs3}${INPUT_KEYS.endSs3}`), [
    INPUT_KEYS.homeSs3,
    INPUT_KEYS.endSs3,
  ]);
});

test("input history navigates submitted commands and restores draft text", () => {
  const state = createInitialState();

  rememberSubmittedInput(state, "first");
  rememberSubmittedInput(state, "second");
  rememberSubmittedInput(state, "second");
  setInputText(state, "draft");

  assert.deepEqual(state.inputHistory, ["first", "second"]);
  assert.equal(navigateInputHistory(state, "up"), true);
  assert.equal(state.input, "second");
  assert.equal(navigateInputHistory(state, "up"), true);
  assert.equal(state.input, "first");
  assert.equal(navigateInputHistory(state, "down"), true);
  assert.equal(state.input, "second");
  assert.equal(navigateInputHistory(state, "down"), true);
  assert.equal(state.input, "draft");
  assert.equal(state.inputHistoryCursor, null);
});

test("editing recalled history exits history navigation", () => {
  const state = createInitialState();

  rememberSubmittedInput(state, "npm test");
  navigateInputHistory(state, "up");
  insertInputText(state, " --watch");

  assert.equal(state.input, "npm test --watch");
  assert.equal(state.inputHistoryCursor, null);
  assert.equal(navigateInputHistory(state, "down"), false);
});

test("input buffer edits around a cursor using unicode-safe positions", () => {
  const state = createInitialState();

  insertInputText(state, "helo");
  moveInputCursor(state, "left");
  insertInputText(state, "l");

  assert.equal(state.input, "hello");
  assert.equal(getInputCursor(state), 4);

  deleteInputForward(state);
  assert.equal(state.input, "hell");

  moveInputCursor(state, "end");
  insertInputText(state, "!");
  assert.equal(state.input, "hell!");
  assert.equal(renderInputWithCursor(state), "hell!▌");

  setInputText(state, "你我", 1);
  insertInputText(state, "和");
  assert.equal(state.input, "你和我");
  assert.equal(getInputCursor(state), 2);
});

test("input buffer clamps cursor and clears both text and cursor", () => {
  const state = createInitialState();

  setInputText(state, "abc", 99);
  assert.equal(getInputCursor(state), 3);
  moveInputCursor(state, "home");
  assert.equal(getInputCursor(state), 0);

  clearInput(state);
  assert.equal(state.input, "");
  assert.equal(state.inputCursor, 0);
});
