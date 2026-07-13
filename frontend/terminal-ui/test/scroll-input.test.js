import assert from "node:assert/strict";
import test from "node:test";

import {
  TRACKPAD_SCROLL_INTERVAL_MS,
  createTrackpadScrollFilter,
} from "../src/scroll-input.js";

test("trackpad filter accepts the first event then caps a same-direction burst", () => {
  let time = 100;
  const filter = createTrackpadScrollFilter({ now: () => time });

  assert.equal(filter.accept("down"), true);
  time += TRACKPAD_SCROLL_INTERVAL_MS - 1;
  assert.equal(filter.accept("down"), false);
  time += 1;
  assert.equal(filter.accept("down"), true);
});

test("trackpad filter accepts direction reversal immediately", () => {
  const filter = createTrackpadScrollFilter({ now: () => 100 });

  assert.equal(filter.accept("down"), true);
  assert.equal(filter.accept("up"), true);
});

test("trackpad filter rejects invalid directions and abnormal burst time", () => {
  let time = 100;
  const filter = createTrackpadScrollFilter({ now: () => time });

  assert.equal(filter.accept("down"), true);
  time = 90;
  assert.equal(filter.accept("down"), false);
  time = Number.NaN;
  assert.equal(filter.accept("down"), false);
  assert.equal(filter.accept("sideways"), false);
});
