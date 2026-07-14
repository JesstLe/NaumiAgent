import { ANSI } from "./ansi.js";

export function createTerminalSession({ stdin, stdout, capabilities }) {
  let active = false;
  let inputListener = null;
  let resizeListener = null;

  function setup({ onInput, onResize }) {
    if (active) return false;
    active = true;
    inputListener = onInput;
    resizeListener = onResize;
    try {
      stdout.write(enableSequence(capabilities));
      if (stdin.isTTY && typeof stdin.setRawMode === "function") {
        stdin.setRawMode(true);
      }
      stdin.resume();
      stdin.setEncoding("utf8");
      stdin.on("data", inputListener);
      stdout.on("resize", resizeListener);
      return true;
    } catch (error) {
      restore();
      throw error;
    }
  }

  function restore() {
    if (!active) return false;
    active = false;
    removeListenerQuietly(stdin, "data", inputListener);
    removeListenerQuietly(stdout, "resize", resizeListener);
    inputListener = null;
    resizeListener = null;
    if (stdin.isTTY && typeof stdin.setRawMode === "function") {
      try {
        stdin.setRawMode(false);
      } catch {
        // Continue restoring cursor and screen even if raw-mode cleanup fails.
      }
    }
    try {
      stdout.write(disableSequence(capabilities));
    } catch {
      // Output may already be closed during process teardown.
    }
    return true;
  }

  return {
    get active() {
      return active;
    },
    setup,
    restore,
  };
}

function enableSequence(capabilities) {
  return ANSI.altOn
    + ANSI.bracketedPasteOn
    + (capabilities.enhancedKeyboard ? ANSI.keyboardDisambiguateOn : "")
    + ANSI.hideCursor;
}

function disableSequence(capabilities) {
  return (capabilities.enhancedKeyboard ? ANSI.keyboardDisambiguateOff : "")
    + ANSI.bracketedPasteOff
    + ANSI.showCursor
    + ANSI.altOff
    + ANSI.reset;
}

function removeListenerQuietly(stream, event, listener) {
  if (!listener || typeof stream.off !== "function") return;
  try {
    stream.off(event, listener);
  } catch {
    // Listener cleanup should not hide the original shutdown reason.
  }
}
