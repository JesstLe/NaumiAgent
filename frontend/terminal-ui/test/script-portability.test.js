import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { ANSI } from "../src/ansi.js";

test("syntax checker resolves package paths outside the package cwd", () => {
  const script = fileURLToPath(new URL("../scripts/check-syntax.js", import.meta.url));
  const result = spawnSync(process.execPath, [script], {
    cwd: tmpdir(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Syntax check passed/);
});

test("terminal UI rejects non-TTY launches before starting the bridge", () => {
  const entry = fileURLToPath(new URL("../src/index.js", import.meta.url));
  const env = {
    ...process.env,
    TERM: "xterm-256color",
    NAUMI_TERMINAL_UI_DEBUG_LOG: "0",
  };
  delete env.NAUMI_TERMINAL_UI_ALLOW_NON_TTY;
  const result = spawnSync(
    process.execPath,
    [
      entry,
      "--bridge-command-json",
      JSON.stringify([process.execPath, "-e", "process.exit(9)"]),
    ],
    {
      cwd: tmpdir(),
      env,
      encoding: "utf8",
      timeout: 3000,
    },
  );

  assert.equal(result.status, 2, result.stderr);
  assert.match(result.stderr, /需要交互式 TTY/);
  assert.equal(result.stdout, "");
});

test("bridge spawn failure restores the terminal before reporting the error", () => {
  const entry = fileURLToPath(new URL("../src/index.js", import.meta.url));
  const env = {
    ...process.env,
    TERM: "xterm-256color",
    NAUMI_TERMINAL_UI_ALLOW_NON_TTY: "1",
    NAUMI_TERMINAL_UI_DEBUG_LOG: "0",
    FORCE_COLOR: "0",
  };
  const missing = `${tmpdir()}/naumi-missing-bridge-${process.pid}-${Date.now()}`;
  const result = spawnSync(
    process.execPath,
    [entry, "--bridge-command-json", JSON.stringify([missing])],
    {
      cwd: tmpdir(),
      env,
      encoding: "utf8",
      timeout: 3000,
    },
  );

  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stdout, new RegExp(escapeRegex(ANSI.altOff)));
  assert.match(result.stderr, /终端 UI 已安全退出/);
});

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
