import test from "node:test";
import assert from "node:assert/strict";
import {
  INPUT_KEYS,
  backspaceInput,
  clearInput,
  createInputTokenizerState,
  deleteInputForward,
  getInputCursor,
  getInputCursorLocation,
  insertInputNewline,
  insertInputText,
  moveInputCursor,
  moveInputCursorToLineBoundary,
  moveInputCursorVertical,
  navigateInputHistory,
  rememberSubmittedInput,
  renderInputWithCursor,
  setInputText,
  splitInputChunk,
  splitInputStreamChunk,
  tokenizeInputChunk,
  truncateInputText,
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
  assert.deepEqual(splitInputChunk("OA"), [INPUT_KEYS.upAlt]);
  assert.deepEqual(splitInputChunk("ob"), [INPUT_KEYS.downAlt]);
  assert.deepEqual(splitInputChunk(`${INPUT_KEYS.shiftTab}${INPUT_KEYS.pageUp}${INPUT_KEYS.pageDown}`), [
    INPUT_KEYS.shiftTab,
    INPUT_KEYS.pageUp,
    INPUT_KEYS.pageDown,
  ]);
  assert.deepEqual(splitInputChunk(INPUT_KEYS.ctrlI), [INPUT_KEYS.ctrlI]);
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
  assert.deepEqual(splitInputChunk(`${INPUT_KEYS.ctrlR}${INPUT_KEYS.ctrlT}${INPUT_KEYS.escape}${INPUT_KEYS.tab}`), [
    INPUT_KEYS.ctrlR,
    INPUT_KEYS.ctrlT,
    INPUT_KEYS.escape,
    INPUT_KEYS.tab,
  ]);
});

test("stream tokenizer buffers split SS3 cursor keys from trackpads", () => {
  let pending = "";

  let parsed = splitInputStreamChunk("\x1b", pending);
  assert.deepEqual(parsed.keys, []);
  pending = parsed.pending;

  parsed = splitInputStreamChunk("O", pending);
  assert.deepEqual(parsed.keys, []);
  pending = parsed.pending;

  parsed = splitInputStreamChunk("A", pending);
  assert.deepEqual(parsed.keys, [INPUT_KEYS.upAlt]);
  assert.equal(parsed.pending, "");

  parsed = splitInputStreamChunk("\x1bO", "");
  assert.deepEqual(parsed.keys, []);
  parsed = splitInputStreamChunk("B", parsed.pending);
  assert.deepEqual(parsed.keys, [INPUT_KEYS.downAlt]);

  parsed = splitInputStreamChunk("\x1bo", "");
  assert.deepEqual(parsed.keys, []);
  parsed = splitInputStreamChunk("a", parsed.pending);
  assert.deepEqual(parsed.keys, [INPUT_KEYS.upAlt]);

  parsed = splitInputStreamChunk("\x1bo", "");
  assert.deepEqual(parsed.keys, []);
  parsed = splitInputStreamChunk("b", parsed.pending);
  assert.deepEqual(parsed.keys, [INPUT_KEYS.downAlt]);
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

test("multiline input inserts a newline at the unicode cursor", () => {
  const state = createInitialState();
  setInputText(state, "第一行第二行", 3);

  insertInputNewline(state);

  assert.equal(state.input, "第一行\n第二行");
  assert.equal(getInputCursor(state), 4);
  assert.deepEqual(getInputCursorLocation(state), { line: 1, column: 0 });
});

test("vertical cursor movement preserves the preferred column across short lines", () => {
  const state = createInitialState();
  setInputText(state, "abcd\n你我\n12345", 3);

  assert.equal(moveInputCursorVertical(state, "down"), true);
  assert.deepEqual(getInputCursorLocation(state), { line: 1, column: 2 });
  assert.equal(moveInputCursorVertical(state, "down"), true);
  assert.deepEqual(getInputCursorLocation(state), { line: 2, column: 3 });
  assert.equal(moveInputCursorVertical(state, "down"), false);
});

test("line boundaries differ from whole-buffer home and end", () => {
  const state = createInitialState();
  setInputText(state, "alpha\nbeta\ngamma", 8);

  moveInputCursorToLineBoundary(state, "start");
  assert.deepEqual(getInputCursorLocation(state), { line: 1, column: 0 });
  moveInputCursorToLineBoundary(state, "end");
  assert.deepEqual(getInputCursorLocation(state), { line: 1, column: 4 });

  moveInputCursor(state, "home");
  assert.equal(getInputCursor(state), 0);
  moveInputCursor(state, "end");
  assert.equal(getInputCursor(state), Array.from(state.input).length);
});

test("horizontal editing resets the preferred vertical column", () => {
  const state = createInitialState();
  setInputText(state, "abcd\n你我\n12345", 3);
  moveInputCursorVertical(state, "down");

  moveInputCursor(state, "left");
  assert.equal(moveInputCursorVertical(state, "down"), true);
  assert.deepEqual(getInputCursorLocation(state), { line: 2, column: 1 });
});

test("cursor movement and deletion keep combining graphemes intact", () => {
  const state = createInitialState();
  setInputText(state, "e\u0301x");

  assert.equal(getInputCursor(state), 2);
  moveInputCursor(state, "left");
  assert.equal(getInputCursor(state), 1);
  assert.equal(backspaceInput(state), true);
  assert.equal(state.input, "x");
  assert.equal(getInputCursor(state), 0);
});

test("bracketed paste is emitted once across arbitrary chunks", () => {
  const tokenizer = createInputTokenizerState();

  assert.deepEqual(tokenizeInputChunk("\x1b[20", tokenizer), []);
  assert.deepEqual(tokenizeInputChunk("0~第一行\n", tokenizer), []);
  assert.deepEqual(tokenizeInputChunk("第二行\x1b[20", tokenizer), []);
  assert.deepEqual(tokenizeInputChunk("1~", tokenizer), [
    { type: "paste", value: "第一行\n第二行" },
  ]);
  assert.deepEqual(tokenizer, { pendingEscape: "", pasteBuffer: null });
});

test("text surrounding bracketed paste retains input order", () => {
  const tokenizer = createInputTokenizerState();

  assert.deepEqual(
    tokenizeInputChunk("before\x1b[200~middle\ntext\x1b[201~after", tokenizer),
    [
      { type: "key", value: "before" },
      { type: "paste", value: "middle\ntext" },
      { type: "key", value: "after" },
    ],
  );
});

test("modified enter sequences normalize to explicit composer keys", () => {
  const tokenizer = createInputTokenizerState();

  assert.deepEqual(
    tokenizeInputChunk("\x1b[13;2u\x1b[27;5;13~\x1b[27;2;13~\x1b[13;5u", tokenizer),
    [
      { type: "key", value: INPUT_KEYS.shiftEnter },
      { type: "key", value: INPUT_KEYS.ctrlEnter },
      { type: "key", value: INPUT_KEYS.shiftEnter },
      { type: "key", value: INPUT_KEYS.ctrlEnter },
    ],
  );
});

test("draft truncation respects grapheme boundaries", () => {
  assert.equal(truncateInputText("Ae\u0301你", 2), "Ae\u0301");
  assert.equal(truncateInputText("Ae\u0301你", 0), "");
});
