import assert from "node:assert/strict";
import test from "node:test";

import { ANSI } from "../src/ansi.js";
import { createScreenPainter } from "../src/screen-painter.js";

test("screen painter clears once then updates only changed rows", () => {
  const writes = [];
  const painter = createScreenPainter({ write: (value) => writes.push(value) });

  const initial = painter.paint(["header", "working-0", "footer"], 80, 3);
  const animated = painter.paint(["header", "working-1", "footer"], 80, 3);
  const unchanged = painter.paint(["header", "working-1", "footer"], 80, 3);

  assert.deepEqual(initial, { mode: "full", changedRows: 3, written: true });
  assert.deepEqual(animated, { mode: "diff", changedRows: 1, written: true });
  assert.deepEqual(unchanged, { mode: "none", changedRows: 0, written: false });
  assert.equal(writes.length, 2);
  assert.equal(writes[0], `${ANSI.clear}header\nworking-0\nfooter`);
  assert.equal(writes[1], `${ANSI.cursorTo(2, 1)}working-1`);
  assert.equal(writes[1].includes("\x1b[2J"), false);
  assert.equal(writes[1].includes("header"), false);
  assert.equal(writes[1].includes("footer"), false);
});

test("screen painter reinitializes after terminal dimensions change", () => {
  const writes = [];
  const painter = createScreenPainter({ write: (value) => writes.push(value) });

  painter.paint(["one", "two"], 80, 2);
  const resized = painter.paint(["one", "two", "three"], 100, 3);

  assert.deepEqual(resized, { mode: "full", changedRows: 3, written: true });
  assert.equal(writes[1], `${ANSI.clear}one\ntwo\nthree`);
});

test("screen painter validates complete frames before writing", () => {
  const writes = [];
  const painter = createScreenPainter({ write: (value) => writes.push(value) });

  assert.throws(() => painter.paint(["only one"], 80, 2), /画面行数/);
  assert.throws(() => painter.paint("not-lines", 80, 2), /画面必须/);
  assert.deepEqual(writes, []);
});

test("screen painter retries a full frame after a failed write", () => {
  const writes = [];
  let fail = true;
  const painter = createScreenPainter({
    write(value) {
      writes.push(value);
      if (fail) {
        fail = false;
        throw new Error("terminal closed");
      }
    },
  });

  assert.throws(() => painter.paint(["frame"], 80, 1), /terminal closed/);
  assert.deepEqual(
    painter.paint(["frame"], 80, 1),
    { mode: "full", changedRows: 1, written: true },
  );
  assert.equal(writes.length, 2);
  assert(writes.every((value) => value.startsWith(ANSI.clear)));
});
