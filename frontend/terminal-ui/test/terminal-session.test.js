import assert from "node:assert/strict";
import test from "node:test";

import { ANSI } from "../src/ansi.js";
import { createTerminalSession } from "../src/terminal-session.js";

test("terminal session negotiates controls and restores exactly once", () => {
  const writes = [];
  const rawModes = [];
  const inputListeners = [];
  const resizeListeners = [];
  const stdin = {
    isTTY: true,
    setRawMode(value) { rawModes.push(value); },
    resume() {},
    setEncoding() {},
    on(event, listener) { inputListeners.push([event, listener]); },
    off(event, listener) { inputListeners.push([`off:${event}`, listener]); },
  };
  const stdout = {
    write(value) { writes.push(value); },
    on(event, listener) { resizeListeners.push([event, listener]); },
    off(event, listener) { resizeListeners.push([`off:${event}`, listener]); },
  };
  const onInput = () => {};
  const onResize = () => {};
  const session = createTerminalSession({
    stdin,
    stdout,
    capabilities: { enhancedKeyboard: false },
  });

  assert.equal(session.setup({ onInput, onResize }), true);
  assert.equal(session.setup({ onInput, onResize }), false);
  assert.equal(session.active, true);
  assert.match(writes[0], new RegExp(escapeRegex(ANSI.altOn)));
  assert.match(writes[0], new RegExp(escapeRegex(ANSI.bracketedPasteOn)));
  assert.doesNotMatch(writes[0], new RegExp(escapeRegex(ANSI.keyboardDisambiguateOn)));
  assert.deepEqual(rawModes, [true]);

  assert.equal(session.restore(), true);
  assert.equal(session.restore(), false);
  assert.equal(session.active, false);
  assert.deepEqual(rawModes, [true, false]);
  assert.equal(inputListeners.at(-1)[0], "off:data");
  assert.equal(resizeListeners.at(-1)[0], "off:resize");
  assert.equal(writes.filter((value) => value.includes(ANSI.altOff)).length, 1);
});

test("terminal session emits enhanced keyboard controls only when supported", () => {
  const writes = [];
  const stream = {
    isTTY: false,
    write(value) { writes.push(value); },
    resume() {},
    setEncoding() {},
    on() {},
    off() {},
  };
  const session = createTerminalSession({
    stdin: stream,
    stdout: stream,
    capabilities: { enhancedKeyboard: true },
  });

  session.setup({ onInput() {}, onResize() {} });
  session.restore();

  assert.match(writes[0], new RegExp(escapeRegex(ANSI.keyboardDisambiguateOn)));
  assert.match(writes[1], new RegExp(escapeRegex(ANSI.keyboardDisambiguateOff)));
});

test("terminal setup failure rolls back screen controls and raw mode", () => {
  const writes = [];
  const rawModes = [];
  const stdin = {
    isTTY: true,
    setRawMode(value) {
      rawModes.push(value);
      if (value) throw new Error("raw mode failed");
    },
    resume() {},
    setEncoding() {},
    on() {},
    off() {},
  };
  const stdout = {
    write(value) { writes.push(value); },
    on() {},
    off() {},
  };
  const session = createTerminalSession({
    stdin,
    stdout,
    capabilities: { enhancedKeyboard: false },
  });

  assert.throws(
    () => session.setup({ onInput() {}, onResize() {} }),
    /raw mode failed/,
  );
  assert.equal(session.active, false);
  assert.deepEqual(rawModes, [true, false]);
  assert.equal(writes.filter((value) => value.includes(ANSI.altOff)).length, 1);
});

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
