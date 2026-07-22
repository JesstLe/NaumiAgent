import assert from "node:assert/strict";
import test from "node:test";

import {
  TERMINAL_CAPABILITY_CONTRACT,
  detectTerminalCapabilities,
  resolveTerminalHome,
} from "../src/terminal-capabilities.js";

test("shared terminal capability contract is immutable at every level", () => {
  assert.equal(Object.isFrozen(TERMINAL_CAPABILITY_CONTRACT), true);
  assert.equal(Object.isFrozen(TERMINAL_CAPABILITY_CONTRACT.profile_fields), true);
  assert.equal(Object.isFrozen(TERMINAL_CAPABILITY_CONTRACT.matchers), true);
  assert.equal(Object.isFrozen(TERMINAL_CAPABILITY_CONTRACT.matchers.ansi_control), true);
});

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
    fullScreen: true,
    ansiControl: true,
    colorLevel: "ansi256",
    colors: true,
    unicode: true,
    mouseProtocol: "sgr",
    alternateScreen: true,
    bracketedPaste: true,
    cursorControl: true,
    synchronizedOutput: false,
    enhancedKeyboard: false,
    animate: true,
    signalMode: "posix",
    home: "/Users/naumi",
    terminal: "xterm-256color",
    terminalProgram: "",
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
  assert.equal(profile.colorLevel, "ansi16");
  assert.equal(profile.signalMode, "windows");
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
  assert.equal(testHarness.fullScreen, false);
  assert.equal(testHarness.alternateScreen, false);
  assert.equal(testHarness.bracketedPaste, false);
  assert.equal(testHarness.synchronizedOutput, false);
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

test("truecolor synchronized output and explicit safe downgrades are negotiated", () => {
  const profile = detectTerminalCapabilities({
    platform: "darwin",
    env: {
      TERM: "xterm-256color",
      TERM_PROGRAM: "iTerm.app",
      COLORTERM: "truecolor",
      NAUMI_ALT_SCREEN: "0",
      NAUMI_BRACKETED_PASTE: "0",
    },
    stdinIsTTY: true,
    stdoutIsTTY: true,
  });

  assert.equal(profile.colorLevel, "truecolor");
  assert.equal(profile.synchronizedOutput, true);
  assert.equal(profile.alternateScreen, false);
  assert.equal(profile.bracketedPaste, false);
  assert.equal(profile.fullScreen, false);
  assert.equal(profile.animate, false);
});

test("unknown terminals fail closed even when control overrides request enablement", () => {
  const profile = detectTerminalCapabilities({
    platform: "linux",
    env: {
      TERM: "unknown-terminal",
      NAUMI_ALT_SCREEN: "1",
      NAUMI_BRACKETED_PASTE: "1",
      NAUMI_SYNCHRONIZED_OUTPUT: "1",
    },
    stdinIsTTY: true,
    stdoutIsTTY: true,
  });

  assert.equal(profile.interactive, true);
  assert.equal(profile.ansiControl, false);
  assert.equal(profile.fullScreen, false);
  assert.equal(profile.colorLevel, "none");
  assert.equal(profile.alternateScreen, false);
  assert.equal(profile.bracketedPaste, false);
  assert.equal(profile.synchronizedOutput, false);
});

test("home directory resolution covers Windows drive and path fallback", () => {
  assert.equal(resolveTerminalHome({ HOME: "/home/a" }), "/home/a");
  assert.equal(resolveTerminalHome({ USERPROFILE: "C:\\Users\\a" }), "C:\\Users\\a");
  assert.equal(
    resolveTerminalHome({ HOMEDRIVE: "D:", HOMEPATH: "\\Profiles\\a" }),
    "D:\\Profiles\\a",
  );
  assert.equal(resolveTerminalHome({}), "");
  assert.equal(resolveTerminalHome({ HOME: "  ", HOMEDRIVE: "C:" }), "");
});
