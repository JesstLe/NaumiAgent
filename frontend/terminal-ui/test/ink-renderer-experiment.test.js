import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  createBenchmarkState,
  runRendererBenchmark,
} from "../scripts/benchmark-renderer.js";
import { runInkRendererBenchmark } from "../scripts/benchmark-ink-renderer.js";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { captureInkExperimentGoldenFrame } from "../src/experiments/ink-golden-capture.js";
import { renderInkExperimentScreen } from "../src/experiments/ink-renderer.js";
import { normalizeServerRecord } from "../src/protocol.js";
import { renderScreen } from "../src/render.js";
import { createInitialState, reduceServerEvent } from "../src/state.js";

const fixturePath = new URL(
  "../../../tests/fixtures/ui17/terminal-run-lifecycle-golden.json",
  import.meta.url,
);
const fixture = JSON.parse(readFileSync(fixturePath, "utf8"));
const coreViewsFixture = JSON.parse(readFileSync(new URL(
  "../../../tests/fixtures/cc02/ink-core-views-golden.json",
  import.meta.url,
), "utf8"));
const benchmarkOptions = {
  profile: "smoke",
  messages: 20,
  tools: 4,
  logChars: 2_000,
  iterations: 3,
  warmup: 0,
  width: 80,
  height: 16,
};

test("Ink experiment consumes the exact current renderer benchmark fixture contract", () => {
  const current = runRendererBenchmark(benchmarkOptions);
  const ink = runInkRendererBenchmark(benchmarkOptions);

  assert.equal(ink.schema, "naumi.renderer-benchmark.v1");
  assert.equal(ink.renderer, "react-ink-6");
  assert.deepEqual(ink.fixture, current.fixture);
  assert.equal(ink.fixture_sha256, current.fixture_sha256);
  assert.deepEqual(ink.protocol_contract, current.protocol_contract);
  assert.deepEqual(ink.scenarios.map((item) => item.name), [
    "tail",
    "deep_scroll",
    "paged_output",
  ]);
  for (const scenario of ink.scenarios) {
    assert.equal(scenario.rendered_lines, 16);
    assert.equal(scenario.viewport_bounded, true);
    assert(scenario.render_ms.p95 >= scenario.render_ms.min);
  }
});

test("Ink experiment renders bounded state without mutation", () => {
  const state = createBenchmarkState({
    messages: 20,
    tools: 4,
    log_chars: 2_000,
    width: 80,
    height: 16,
  });
  const before = JSON.stringify(state);
  const lines = renderInkExperimentScreen(state, 80, 16);

  assert.equal(lines.length, 16);
  assert(lines.every((line) => visibleWidth(line) === 80));
  assert.equal(JSON.stringify(state), before);
  assert.throws(() => renderInkExperimentScreen(state, 39, 16), /width 必须是 40-400/);
});

test("Ink experiment captures the shared lifecycle fixture deterministically", () => {
  const first = captureInkExperimentGoldenFrame(fixture);
  const second = captureInkExperimentGoldenFrame(fixture);

  assert.equal(first.schema, "naumi.terminal-golden-frame.v1");
  assert.equal(first.surface, "new_ui_experiment");
  assert.equal(first.renderer, "react-ink-6");
  assert.equal(first.line_count, fixture.capture.height);
  assert(first.max_visible_width <= fixture.capture.width);
  assert.deepEqual(first.missing_anchors, []);
  assert.equal(first.text, second.text);
  assert.equal(first.text_sha256, second.text_sha256);
});

test("Ink and current renderers preserve shared permission task and footer semantics", () => {
  assert.equal(coreViewsFixture.schema_version, 1);
  for (const scenario of coreViewsFixture.scenarios) {
    const state = createInitialState();
    state.welcome.dismissed = true;
    for (const rawRecord of scenario.records) {
      reduceServerEvent(state, normalizeServerRecord(rawRecord));
    }
    const currentText = renderScreen(
      state,
      coreViewsFixture.width,
      coreViewsFixture.height,
      { cwd: "/workspace/naumi", home: "/home/naumi", clockText: "12:34:56" },
    ).map(stripAnsi).join("\n");
    const inkText = renderInkExperimentScreen(
      state,
      coreViewsFixture.width,
      coreViewsFixture.height,
    ).map(stripAnsi).join("\n");

    for (const anchor of scenario.anchors) {
      assert(currentText.includes(anchor), `${scenario.id} current 缺少语义锚点: ${anchor}`);
      assert(inkText.includes(anchor), `${scenario.id} Ink 缺少语义锚点: ${anchor}`);
    }
  }
});
