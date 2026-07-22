import test from "node:test";
import assert from "node:assert/strict";
import { runRendererBenchmark } from "../scripts/benchmark-renderer.js";

test("current renderer benchmark emits comparable bounded scenario metrics", () => {
  const result = runRendererBenchmark({
    profile: "smoke",
    messages: 20,
    tools: 4,
    logChars: 2_000,
    iterations: 3,
    warmup: 0,
    width: 80,
    height: 16,
  });

  assert.equal(result.schema, "naumi.renderer-benchmark.v1");
  assert.equal(result.renderer, "current-node");
  assert.match(result.fixture_sha256, /^[a-f0-9]{64}$/);
  assert.deepEqual(result.scenarios.map((item) => item.name), [
    "tail",
    "deep_scroll",
    "large_output",
  ]);
  for (const scenario of result.scenarios) {
    assert.equal(scenario.iterations, 3);
    assert.equal(scenario.rendered_lines, 16);
    assert.equal(scenario.viewport_bounded, true);
    assert(scenario.cold_render_ms >= 0);
    assert(scenario.render_ms.min >= 0);
    assert(scenario.render_ms.p95 >= scenario.render_ms.min);
    assert(scenario.render_ms.max >= scenario.render_ms.p95);
  }
});

test("current renderer benchmark rejects unknown profiles and invalid numeric input", () => {
  assert.throws(() => runRendererBenchmark({ profile: "unknown" }), /未知 benchmark profile/);
  assert.throws(
    () => runRendererBenchmark({ profile: "smoke", messages: "many" }),
    /不是有效数字/,
  );
});
