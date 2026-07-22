import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  captureNewUiGoldenFrame,
  validateCaptureFixture,
} from "../src/golden-capture.js";

const fixturePath = new URL(
  "../../../tests/fixtures/ui17/terminal-run-lifecycle-golden.json",
  import.meta.url,
);
const fixture = JSON.parse(readFileSync(fixturePath, "utf8"));

test("New UI golden capture is deterministic and viewport bounded", () => {
  const first = captureNewUiGoldenFrame(fixture);
  const second = captureNewUiGoldenFrame(fixture);

  assert.equal(first.schema, "naumi.terminal-golden-frame.v1");
  assert.equal(first.surface, "new_ui");
  assert.equal(first.renderer, "node-ansi");
  assert.equal(first.width, fixture.capture.width);
  assert.equal(first.height, fixture.capture.height);
  assert.equal(first.line_count, fixture.capture.height);
  assert(first.max_visible_width <= fixture.capture.width);
  assert.deepEqual(first.missing_anchors, []);
  assert.match(first.ansi, /\x1b\[[0-9;]*m/);
  assert.doesNotMatch(first.text, /\x1b\[/);
  assert.equal(first.ansi, second.ansi);
  assert.equal(first.text, second.text);
  assert.equal(first.ansi_sha256, second.ansi_sha256);
  assert.equal(first.text_sha256, second.text_sha256);
});

test("New UI golden capture reports semantic drift without hiding it", () => {
  const changed = structuredClone(fixture);
  changed.capture.required_anchors.push("不应存在的锚点");

  const frame = captureNewUiGoldenFrame(changed);

  assert.deepEqual(frame.missing_anchors, ["不应存在的锚点"]);
});

test("New UI golden capture rejects malformed fixtures and viewport extremes", () => {
  assert.throws(
    () => validateCaptureFixture({ schema_version: 1 }),
    /缺少 capture schema v1/,
  );
  assert.throws(
    () => captureNewUiGoldenFrame(fixture, { width: 39 }),
    /width 必须是 40-400 的整数/,
  );
  assert.throws(
    () => captureNewUiGoldenFrame(fixture, { height: 201 }),
    /height 必须是 12-200 的整数/,
  );
});
