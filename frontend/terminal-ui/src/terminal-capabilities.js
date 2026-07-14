const FALSE_VALUES = new Set(["0", "false", "no", "off"]);

export function detectTerminalCapabilities({
  platform = process.platform,
  env = process.env,
  stdinIsTTY = process.stdin.isTTY === true,
  stdoutIsTTY = process.stdout.isTTY === true,
} = {}) {
  const terminal = String(env.TERM ?? "").trim();
  const allowNonTTY = environmentFlag(env.NAUMI_TERMINAL_UI_ALLOW_NON_TTY);
  const dumb = terminal.toLowerCase() === "dumb";
  const interactive = allowNonTTY || (stdinIsTTY && stdoutIsTTY && !dumb);
  const windowsUnicode = platform !== "win32" || supportsWindowsUnicode(env);
  const unicode = interactive && !dumb && windowsUnicode;
  const forcedColor = optionalEnvironmentFlag(env.FORCE_COLOR);
  const noColor = Object.hasOwn(env, "NO_COLOR");
  const colors = interactive && !dumb && (forcedColor ?? !noColor);
  const reducedMotion = environmentFlag(env.NAUMI_REDUCE_MOTION);
  const ci = environmentFlag(env.CI);

  return Object.freeze({
    interactive,
    colors,
    unicode,
    enhancedKeyboard: interactive && !dumb && supportsEnhancedKeyboard(env),
    animate: interactive && unicode && !ci && !reducedMotion,
    home: resolveTerminalHome(env),
    terminal,
  });
}

export function resolveTerminalHome(env = process.env) {
  if (env.HOME) return String(env.HOME);
  if (env.USERPROFILE) return String(env.USERPROFILE);
  if (env.HOMEDRIVE || env.HOMEPATH) {
    return `${env.HOMEDRIVE ?? ""}${env.HOMEPATH ?? ""}`;
  }
  return "";
}

function supportsEnhancedKeyboard(env) {
  const term = String(env.TERM ?? "").toLowerCase();
  const program = String(env.TERM_PROGRAM ?? "").toLowerCase();
  return Boolean(env.KITTY_WINDOW_ID)
    || term === "xterm-kitty"
    || term === "foot"
    || term.startsWith("foot-")
    || ["wezterm", "ghostty", "kitty"].some((name) => program.includes(name));
}

function supportsWindowsUnicode(env) {
  const program = String(env.TERM_PROGRAM ?? "").toLowerCase();
  const conEmuAnsi = String(env.ConEmuANSI ?? "").toUpperCase();
  return Boolean(env.WT_SESSION)
    || Boolean(env.ANSICON)
    || conEmuAnsi === "ON"
    || ["wezterm", "ghostty", "vscode"].some((name) => program.includes(name));
}

function optionalEnvironmentFlag(value) {
  if (value == null || String(value).trim() === "") return null;
  return environmentFlag(value);
}

function environmentFlag(value) {
  const normalized = String(value ?? "").trim().toLowerCase();
  return Boolean(normalized) && !FALSE_VALUES.has(normalized);
}
