#!/usr/bin/env node

import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

import { runRendererBenchmarkWithAdapter } from "./benchmark-renderer.js";
import {
  INK_EXPERIMENT_RENDERER,
  renderInkExperimentScreen,
} from "../src/experiments/ink-renderer.js";

export function runInkRendererBenchmark(options = {}) {
  return runRendererBenchmarkWithAdapter(options, {
    renderer: INK_EXPERIMENT_RENDERER,
    render: renderInkExperimentScreen,
  });
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
  const result = runInkRendererBenchmark({
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
