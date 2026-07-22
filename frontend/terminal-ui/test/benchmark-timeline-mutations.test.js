import test from "node:test";
import assert from "node:assert/strict";

import { runTimelineMutationBenchmark } from "../scripts/benchmark-timeline-mutations.js";

test("timeline mutation benchmark proves bounded incremental updates", () => {
  const result = runTimelineMutationBenchmark({
    messages: 100,
    tools: 10,
    iterations: 3,
    width: 80,
    height: 16,
  });
  assert.equal(result.schema, "naumi.timeline-mutation-benchmark.v1");
  assert.equal(result.index.build_count, 1);
  assert.equal(result.index.append_count, 1);
  assert.equal(result.index.partial_update_count, 3);
  assert.equal(result.index.stores_rendered_lines, false);
  assert.equal(result.legacy_semantic_equivalent, true);
  assert(result.render_ms.p95 >= result.render_ms.min);
});

test("timeline mutation benchmark rejects invalid numeric input", () => {
  assert.throws(
    () => runTimelineMutationBenchmark({ messages: "many" }),
    /不是有效数字/,
  );
});
