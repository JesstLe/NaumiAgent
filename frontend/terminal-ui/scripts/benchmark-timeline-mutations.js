#!/usr/bin/env node

import { createHash } from "node:crypto";
import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

import { PROTOCOL_REGISTRY_SHA256, PROTOCOL_VERSION } from "../src/protocol.js";
import { renderScreen } from "../src/render.js";
import { handleAssistantStream } from "../src/state.js";
import { timelineRowIndexDebug } from "../src/timeline-row-index.js";
import { createBenchmarkState } from "./benchmark-renderer.js";

export function runTimelineMutationBenchmark(options = {}) {
  const fixture = {
    messages: boundedInteger(options.messages, 10_000, 1, 20_000),
    tools: boundedInteger(options.tools, 1_000, 0, 5_000),
    log_chars: 0,
    width: boundedInteger(options.width, 120, 40, 400),
    height: boundedInteger(options.height, 40, 8, 200),
  };
  const iterations = boundedInteger(options.iterations, 100, 3, 2_000);
  const state = createBenchmarkState(fixture, { scrollOffset: fixture.messages * 2 });
  const env = { cwd: "/workspace", home: "/home/naumi", clockText: "12:34:56" };
  renderScreen(state, fixture.width, fixture.height, env);
  handleAssistantStream(state, { phase: "start" });
  renderScreen(state, fixture.width, fixture.height, env);

  const samples = [];
  for (let index = 0; index < iterations; index += 1) {
    handleAssistantStream(state, { phase: "token", content: "流式增量" });
    const startedAt = process.hrtime.bigint();
    renderScreen(state, fixture.width, fixture.height, env);
    samples.push(Number(process.hrtime.bigint() - startedAt) / 1_000_000);
  }
  const indexed = renderScreen(state, fixture.width, fixture.height, env);
  const legacy = renderScreen(state, fixture.width, fixture.height, {
    ...env,
    disableVirtualTimeline: true,
  });
  const ordered = samples.toSorted((left, right) => left - right);
  const debug = timelineRowIndexDebug(state);
  return {
    schema: "naumi.timeline-mutation-benchmark.v1",
    fixture,
    fixture_sha256: createHash("sha256").update(JSON.stringify(fixture)).digest("hex"),
    protocol_contract: {
      version: PROTOCOL_VERSION,
      registry_sha256: PROTOCOL_REGISTRY_SHA256,
    },
    runtime: { node: process.version, platform: process.platform, arch: process.arch },
    iterations,
    render_ms: {
      min: round(ordered[0]),
      p50: round(percentile(ordered, 0.5)),
      p95: round(percentile(ordered, 0.95)),
      max: round(ordered.at(-1)),
    },
    index: {
      build_count: debug?.buildCount ?? 0,
      append_count: debug?.appendCount ?? 0,
      partial_update_count: debug?.partialUpdateCount ?? 0,
      stores_rendered_lines: debug?.storesRenderedLines ?? true,
    },
    legacy_semantic_equivalent: JSON.stringify(indexed) === JSON.stringify(legacy),
  };
}

function percentile(ordered, value) {
  return ordered[Math.max(0, Math.ceil(ordered.length * value) - 1)];
}

function round(value) {
  return Number(value.toFixed(3));
}

function boundedInteger(value, fallback, minimum, maximum) {
  if (value === undefined) return fallback;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`benchmark 参数不是有效数字: ${value}`);
  return Math.min(maximum, Math.max(minimum, Math.trunc(parsed)));
}

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!key.startsWith("--")) throw new Error(`未知参数: ${key}`);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`${key} 缺少值`);
    values[key.slice(2).replaceAll("-", "_")] = value;
    index += 1;
  }
  return values;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const result = runTimelineMutationBenchmark({
    messages: args.messages,
    tools: args.tools,
    iterations: args.iterations,
    width: args.width,
    height: args.height,
  });
  const output = `${JSON.stringify(result, null, 2)}\n`;
  if (args.output) writeFileSync(args.output, output, "utf8");
  process.stdout.write(output);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
