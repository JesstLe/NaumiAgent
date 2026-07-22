import RAW_CONTRACT from "../terminal-capability-contract.json" with { type: "json" };

const FALSE_VALUES = new Set(["0", "false", "no", "off"]);
const TRUECOLOR_VALUES = new Set(["24bit", "truecolor"]);

export const TERMINAL_CAPABILITY_CONTRACT = validateContract(RAW_CONTRACT);

export function detectTerminalCapabilities({
  platform = process.platform,
  env = process.env,
  stdinIsTTY = process.stdin.isTTY === true,
  stdoutIsTTY = process.stdout.isTTY === true,
} = {}) {
  const terminal = String(env.TERM ?? "").trim();
  const terminalProgram = String(env.TERM_PROGRAM ?? "").trim();
  const dumb = terminal.toLowerCase() === "dumb";
  const allowNonTTY = environmentFlag(env.NAUMI_TERMINAL_UI_ALLOW_NON_TTY);
  const interactive = allowNonTTY || (stdinIsTTY && stdoutIsTTY && !dumb);
  const ansiControl = interactive
    && !dumb
    && matchesRule("ansi_control", { terminal, terminalProgram, env });
  const alternateScreen = ansiControl && negotiatedBoolean(
    env.NAUMI_ALT_SCREEN,
    true,
  );
  const bracketedPaste = ansiControl && negotiatedBoolean(
    env.NAUMI_BRACKETED_PASTE,
    true,
  );
  const cursorControl = ansiControl;
  const fullScreen = ansiControl && alternateScreen && cursorControl;
  const windowsUnicode = platform !== "win32"
    || matchesRule("windows_unicode", { terminal, terminalProgram, env });
  const unicode = interactive && !dumb && windowsUnicode;
  const colorLevel = resolveColorLevel({
    ansiControl,
    terminal,
    env,
  });
  const reducedMotion = environmentFlag(env.NAUMI_REDUCE_MOTION);
  const ci = environmentFlag(env.CI);
  const synchronizedOutput = ansiControl && negotiatedBoolean(
    env.NAUMI_SYNCHRONIZED_OUTPUT,
    matchesRule("synchronized_output", { terminal, terminalProgram, env }),
  );
  const enhancedKeyboard = ansiControl
    && matchesRule("enhanced_keyboard", { terminal, terminalProgram, env });
  const mouseProtocol = ansiControl
    && matchesRule("mouse_sgr", { terminal, terminalProgram, env })
    ? "sgr"
    : "none";

  return validateProfile(Object.freeze({
    interactive,
    fullScreen,
    ansiControl,
    colorLevel,
    colors: colorLevel !== "none",
    unicode,
    mouseProtocol,
    alternateScreen,
    bracketedPaste,
    cursorControl,
    synchronizedOutput,
    enhancedKeyboard,
    animate: fullScreen && unicode && !ci && !reducedMotion,
    signalMode: interactive ? (platform === "win32" ? "windows" : "posix") : "none",
    home: resolveTerminalHome(env),
    terminal,
    terminalProgram,
  }));
}

export function resolveTerminalHome(env = process.env) {
  const home = String(env.HOME ?? "").trim();
  if (home) return home;
  const profile = String(env.USERPROFILE ?? "").trim();
  if (profile) return profile;
  const drive = String(env.HOMEDRIVE ?? "").trim();
  const path = String(env.HOMEPATH ?? "").trim();
  return drive && path ? `${drive}${path}` : "";
}

function resolveColorLevel({ ansiControl, terminal, env }) {
  if (!ansiControl) return "none";
  const forced = forcedColorLevel(env.FORCE_COLOR);
  if (forced !== null) return forced;
  if (Object.hasOwn(env, "NO_COLOR")) return "none";
  if (TRUECOLOR_VALUES.has(String(env.COLORTERM ?? "").trim().toLowerCase())) {
    return "truecolor";
  }
  if (terminal.toLowerCase().includes("256color")) return "ansi256";
  return "ansi16";
}

function forcedColorLevel(value) {
  if (value == null || String(value).trim() === "") return null;
  const normalized = String(value).trim().toLowerCase();
  if (FALSE_VALUES.has(normalized)) return "none";
  if (normalized === "3") return "truecolor";
  if (normalized === "2") return "ansi256";
  return "ansi16";
}

function negotiatedBoolean(value, fallback) {
  const explicit = optionalEnvironmentFlag(value);
  return explicit === null ? Boolean(fallback) : explicit;
}

function matchesRule(name, { terminal, terminalProgram, env }) {
  const rule = TERMINAL_CAPABILITY_CONTRACT.matchers[name];
  if (!rule) return false;
  const term = terminal.toLowerCase();
  const program = terminalProgram.toLowerCase();
  if ((rule.term_exact ?? []).includes(term)) return true;
  if ((rule.term_prefixes ?? []).some((prefix) => term.startsWith(prefix))) return true;
  if ((rule.program_contains ?? []).some((part) => program.includes(part))) return true;
  if ((rule.presence_env ?? []).some((key) => environmentFlag(env[key]))) return true;
  return (rule.windows_env ?? []).some((key) => environmentFlag(env[key]));
}

function validateProfile(profile) {
  const expected = [...TERMINAL_CAPABILITY_CONTRACT.profile_fields].sort();
  const observed = Object.keys(profile).sort();
  if (JSON.stringify(expected) !== JSON.stringify(observed)) {
    throw new Error("terminal capability profile 字段与 contract 不一致");
  }
  if (!TERMINAL_CAPABILITY_CONTRACT.color_levels.includes(profile.colorLevel)) {
    throw new Error("terminal capability colorLevel 无效");
  }
  if (!TERMINAL_CAPABILITY_CONTRACT.mouse_protocols.includes(profile.mouseProtocol)) {
    throw new Error("terminal capability mouseProtocol 无效");
  }
  if (!TERMINAL_CAPABILITY_CONTRACT.signal_modes.includes(profile.signalMode)) {
    throw new Error("terminal capability signalMode 无效");
  }
  if (!profile.ansiControl && (
    profile.alternateScreen
    || profile.bracketedPaste
    || profile.cursorControl
    || profile.synchronizedOutput
    || profile.enhancedKeyboard
    || profile.colorLevel !== "none"
  )) {
    throw new Error("terminal capability ANSI 子能力不能脱离 ansiControl");
  }
  if (profile.fullScreen !== Boolean(
    profile.ansiControl && profile.alternateScreen && profile.cursorControl
  )) {
    throw new Error("terminal capability fullScreen 派生值无效");
  }
  if (profile.animate && (!profile.fullScreen || !profile.unicode)) {
    throw new Error("terminal capability animate 派生值无效");
  }
  return profile;
}

function validateContract(value) {
  if (!value || value.schema_version !== 1 || typeof value.matchers !== "object") {
    throw new Error("terminal-capability-contract.json schema 无效");
  }
  for (const key of [
    "profile_fields",
    "color_levels",
    "mouse_protocols",
    "signal_modes",
    "required_matchers",
    "matcher_fields",
  ]) {
    if (!Array.isArray(value[key]) || value[key].length === 0) {
      throw new Error(`terminal-capability-contract.json ${key} 无效`);
    }
    if (value[key].some((entry) => typeof entry !== "string" || !entry)) {
      throw new Error(`terminal-capability-contract.json ${key} 包含无效值`);
    }
    if (new Set(value[key]).size !== value[key].length) {
      throw new Error(`terminal-capability-contract.json ${key} 存在重复值`);
    }
  }
  const matcherNames = Object.keys(value.matchers).sort();
  const requiredMatcherNames = [...value.required_matchers].sort();
  if (JSON.stringify(matcherNames) !== JSON.stringify(requiredMatcherNames)) {
    throw new Error("terminal-capability-contract.json matcher 集合无效");
  }
  for (const [name, rule] of Object.entries(value.matchers)) {
    if (!rule || typeof rule !== "object" || Array.isArray(rule)) {
      throw new Error(`terminal-capability-contract.json matcher ${name} 无效`);
    }
    for (const [key, entries] of Object.entries(rule)) {
      if (!value.matcher_fields.includes(key)) {
        throw new Error(`terminal-capability-contract.json matcher 字段 ${key} 未知`);
      }
      if (!Array.isArray(entries)
        || entries.some((entry) => typeof entry !== "string" || !entry)) {
        throw new Error(`terminal-capability-contract.json matcher 字段 ${key} 无效`);
      }
    }
  }
  return deepFreeze(value);
}

function deepFreeze(value) {
  if (!value || typeof value !== "object") return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.isFrozen(value) ? value : Object.freeze(value);
}

function optionalEnvironmentFlag(value) {
  if (value == null || String(value).trim() === "") return null;
  return environmentFlag(value);
}

function environmentFlag(value) {
  const normalized = String(value ?? "").trim().toLowerCase();
  return Boolean(normalized) && !FALSE_VALUES.has(normalized);
}
