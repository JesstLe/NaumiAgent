import assert from "node:assert/strict";
import test from "node:test";

import {
  detectTerminalCapabilities,
  resolveTerminalHome,
} from "../src/terminal-capabilities.js";

test("macOS and Linux interactive terminals use the portable baseline", () => {
  const mac = detectTerminalCapabilities({
    platform: "darwin",
    env: { TERM: "xterm-256color", HOME: "/Users/naumi" },
    stdinIsTTY: true,
    stdoutIsTTY: true,
  });
  const linux = detectTerminalCapabilities({
    platform: "linux",
    env: { TERM: "xterm-256color", HOME: "/home/naumi" },
    stdinIsTTY: true,
    stdoutIsTTY: true,
  });

  assert.deepEqual(mac, {
    interactive: true,
    colors: true,
    unicode: true,
    enhancedKeyboard: false,
    animate: true,
    home: "/Users/naumi",
    terminal: "xterm-256color",
  });
  assert.equal(linux.interactive, true);
  assert.equal(linux.unicode, true);
  assert.equal(linux.home, "/home/naumi");
});

test("enhanced keyboard protocol is limited to known supporting terminals", () => {
  for (const env of [
    { TERM: "xterm-kitty", KITTY_WINDOW_ID: "1" },
    { TERM: "xterm-256color", TERM_PROGRAM: "WezTerm" },
    { TERM: "xterm-ghostty", TERM_PROGRAM: "ghostty" },
    { TERM: "foot-extra" },
  ]) {
    const profile = detectTerminalCapabilities({
      platform: "linux",
      env,
      stdinIsTTY: true,
      stdoutIsTTY: true,
    });
    assert.equal(profile.enhancedKeyboard, true, JSON.stringify(env));
  }
});

test("Windows Terminal uses USERPROFILE without assuming a POSIX HOME", () => {
  const profile = detectTerminalCapabilities({
    platform: "win32",
    env: {
      WT_SESSION: "session",
      USERPROFILE: "C:\\Users\\naumi",
    },
    stdinIsTTY: true,
    stdoutIsTTY: true,
  });

  assert.equal(profile.interactive, true);
  assert.equal(profile.colors, true);
  assert.equal(profile.unicode, true);
  assert.equal(profile.enhancedKeyboard, false);
  assert.equal(profile.home, "C:\\Users\\naumi");
});

test("non-interactive and dumb terminals are rejected unless explicitly allowed", () => {
  const piped = detectTerminalCapabilities({
    platform: "linux",
    env: { TERM: "xterm-256color" },
    stdinIsTTY: false,
    stdoutIsTTY: false,
  });
  const dumb = detectTerminalCapabilities({
    platform: "linux",
    env: { TERM: "dumb" },
    stdinIsTTY: true,
    stdoutIsTTY: true,
  });
  const testHarness = detectTerminalCapabilities({
    platform: "win32",
    env: { TERM: "dumb", NAUMI_TERMINAL_UI_ALLOW_NON_TTY: "1" },
    stdinIsTTY: false,
    stdoutIsTTY: false,
  });

  assert.equal(piped.interactive, false);
  assert.equal(dumb.interactive, false);
  assert.equal(testHarness.interactive, true);
  assert.equal(testHarness.colors, false);
  assert.equal(testHarness.unicode, false);
  assert.equal(testHarness.animate, false);
});

test("NO_COLOR FORCE_COLOR CI and reduced motion are negotiated independently", () => {
  const noColor = detectTerminalCapabilities({
    platform: "linux",
    env: { TERM: "xterm-256color", NO_COLOR: "1" },
    stdinIsTTY: true,
    stdoutIsTTY: true,
  });
  const forced = detectTerminalCapabilities({
    platform: "linux",
    env: { TERM: "xterm-256color", NO_COLOR: "1", FORCE_COLOR: "1" },
    stdinIsTTY: true,
    stdoutIsTTY: true,
  });
  const reduced = detectTerminalCapabilities({
    platform: "linux",
    env: { TERM: "xterm-256color", CI: "true", NAUMI_REDUCE_MOTION: "1" },
    stdinIsTTY: true,
    stdoutIsTTY: true,
  });

  assert.equal(noColor.colors, false);
  assert.equal(forced.colors, true);
  assert.equal(reduced.colors, true);
  assert.equal(reduced.animate, false);
});

test("home directory resolution covers Windows drive and path fallback", () => {
  assert.equal(resolveTerminalHome({ HOME: "/home/a" }), "/home/a");
  assert.equal(resolveTerminalHome({ USERPROFILE: "C:\\Users\\a" }), "C:\\Users\\a");
  assert.equal(
    resolveTerminalHome({ HOMEDRIVE: "D:", HOMEPATH: "\\Profiles\\a" }),
    "D:\\Profiles\\a",
  );
  assert.equal(resolveTerminalHome({}), "");
});
