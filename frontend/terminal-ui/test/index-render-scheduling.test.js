import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const source = fs.readFileSync(new URL("../src/index.js", import.meta.url), "utf8");

test("terminal entrypoint delegates first-frame and normal redraw scheduling", () => {
  assert.match(source, /import \{ createRedrawScheduler \} from "\.\/redraw-scheduler\.js"/);
  assert.match(source, /const redrawScheduler = createRedrawScheduler\(\{ onRedraw: redraw \}\)/);
  assert.doesNotMatch(source, /let redrawTimer\b/);
  assert.match(source, /redrawScheduler\.settleInitial\(\)/);
  assert.match(
    source,
    /const paint = screenPainter\.paint[\s\S]*redrawScheduler\.markPainted\(\)/,
  );
  assert.match(
    source,
    /function restoreTerminal\(\)[\s\S]*redrawScheduler\.cancel\(\)/,
  );
});
