#!/usr/bin/env node

import { createHash } from "node:crypto";
import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { visibleWidth } from "../src/ansi.js";
import { PROTOCOL_REGISTRY_SHA256, PROTOCOL_VERSION } from "../src/protocol.js";
import { renderScreen } from "../src/render.js";
import { createInitialState } from "../src/state.js";

const PROFILES = Object.freeze({
  smoke: Object.freeze({ messages: 1_000, tools: 100, logChars: 100_000, iterations: 12, warmup: 2 }),
  release: Object.freeze({ messages: 10_000, tools: 1_000, logChars: 10_000_000, iterations: 20, warmup: 3 }),
});

export function runRendererBenchmark(options = {}) {
  return runRendererBenchmarkWithAdapter(options, {
    renderer: "current-node",
    render: (state, width, height) => renderScreen(
      state,
      width,
      height,
      { cwd: "/workspace", home: "/home/naumi" },
    ),
  });
}

export function runRendererBenchmarkWithAdapter(options = {}, adapter = {}) {
  if (typeof adapter.renderer !== "string" || !adapter.renderer.trim()) {
    throw new Error("benchmark adapter 缺少 renderer 标识");
  }
  if (typeof adapter.render !== "function") {
    throw new Error("benchmark adapter 缺少 render 函数");
  }
  const profileName = String(options.profile || "smoke");
  const profile = PROFILES[profileName];
  if (!profile) throw new Error(`未知 benchmark profile: ${profileName}`);
  const fixture = {
    generator: "terminal-ui-mixed-cjk-v2-paged-output",
    messages: boundedInteger(options.messages, profile.messages, 1, 20_000),
    tools: boundedInteger(options.tools, profile.tools, 0, 5_000),
    log_chars: boundedInteger(options.logChars, profile.logChars, 0, 20_000_000),
    width: boundedInteger(options.width, 120, 40, 400),
    height: boundedInteger(options.height, 40, 8, 200),
  };
  const iterations = boundedInteger(options.iterations, profile.iterations, 3, 100);
  const warmup = boundedInteger(options.warmup, profile.warmup, 0, 20);
  const scenarios = [
    benchmarkScenario("tail", fixture, iterations, warmup, 0, false, adapter.render),
    benchmarkScenario(
      "deep_scroll",
      fixture,
      iterations,
      warmup,
      Math.max(1, fixture.messages * 2),
      false,
      adapter.render,
    ),
    benchmarkScenario("paged_output", fixture, iterations, warmup, 0, true, adapter.render),
  ];
  return {
    schema: "naumi.renderer-benchmark.v1",
    renderer: adapter.renderer.trim(),
    profile: profileName,
    fixture,
    fixture_sha256: createHash("sha256").update(JSON.stringify(fixture)).digest("hex"),
    protocol_contract: {
      version: PROTOCOL_VERSION,
      registry_sha256: PROTOCOL_REGISTRY_SHA256,
    },
    runtime: { node: process.version, platform: process.platform, arch: process.arch },
    scenarios,
  };
}

function benchmarkScenario(
  name,
  fixture,
  iterations,
  warmup,
  scrollOffset,
  includeLargeOutput,
  render,
) {
  const state = createBenchmarkState(fixture, { scrollOffset, includeLargeOutput });
  const rssBefore = process.memoryUsage().rss;
  const coldStartedAt = process.hrtime.bigint();
  let lines = render(state, fixture.width, fixture.height);
  const coldRenderMs = Number(process.hrtime.bigint() - coldStartedAt) / 1_000_000;
  for (let index = 0; index < warmup; index += 1) {
    render(state, fixture.width, fixture.height);
  }
  const samples = [];
  for (let index = 0; index < iterations; index += 1) {
    const startedAt = process.hrtime.bigint();
    lines = render(state, fixture.width, fixture.height);
    samples.push(Number(process.hrtime.bigint() - startedAt) / 1_000_000);
  }
  const ordered = samples.toSorted((left, right) => left - right);
  return {
    name,
    iterations,
    cold_render_ms: round(coldRenderMs),
    render_ms: {
      min: round(ordered[0]),
      p50: round(percentile(ordered, 0.5)),
      p95: round(percentile(ordered, 0.95)),
      max: round(ordered.at(-1)),
    },
    rss_delta_bytes: Math.max(0, process.memoryUsage().rss - rssBefore),
    rendered_lines: lines.length,
    viewport_bounded: lines.length === fixture.height
      && lines.every((line) => visibleWidth(line) <= fixture.width),
  };
}

export function createBenchmarkState(fixture, { scrollOffset = 0, includeLargeOutput = false } = {}) {
  const state = createInitialState();
  state.welcome.dismissed = true;
  state.followTail = scrollOffset === 0;
  state.scrollOffset = scrollOffset;
  for (let index = 0; index < fixture.messages; index += 1) {
    state.messages.push({
      kind: index % 2 === 0 ? "user" : "assistant",
      id: `message-${index}`,
      content: `第 ${index} 条消息 · renderer benchmark 中文宽字符 ${index % 17}`,
      deliveryStatus: index % 2 === 0 ? "accepted" : undefined,
    });
  }
  for (let index = 0; index < fixture.tools; index += 1) {
    state.messages.push({
      kind: "tool",
      id: `tool-${index}`,
      callId: `call-${index}`,
      name: "bash_run",
      status: "success",
      primary: `command ${index}`,
      durationMs: index,
      output: `tool output ${index}`,
    });
  }
  if (includeLargeOutput && fixture.log_chars > 0) {
    const preview = "日志行 abcdefghijklmnopqrstuvwxyz\n"
      .repeat(Math.ceil(Math.min(fixture.log_chars, 2_000) / 31))
      .slice(0, Math.min(fixture.log_chars, 2_000));
    state.messages.push({
      kind: "tool",
      id: "large-output",
      callId: "large-output",
      name: "bash_run",
      status: "success",
      primary: "large benchmark log",
      output: preview,
      outputLength: fixture.log_chars,
      outputBytes: fixture.log_chars,
      outputArtifactId: `out_${"a".repeat(32)}`,
      outputPageCount: Math.max(1, Math.ceil(fixture.log_chars / 8_192)),
      outputPageChars: 8_192,
      outputSha256: "b".repeat(64),
    });
  }
  return state;
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
    const name = key.slice(2).replaceAll("-", "_");
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`${key} 缺少值`);
    values[name] = value;
    index += 1;
  }
  return values;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const result = runRendererBenchmark({
    profile: args.profile,
    messages: args.messages,
    tools: args.tools,
    logChars: args.log_chars,
    iterations: args.iterations,
    warmup: args.warmup,
    width: args.width,
    height: args.height,
  });
  const output = `${JSON.stringify(result, null, 2)}\n`;
  if (args.output) writeFileSync(args.output, output, "utf8");
  process.stdout.write(output);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
