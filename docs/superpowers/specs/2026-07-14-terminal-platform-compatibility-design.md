# Terminal Platform Compatibility Design

## Goal

Make the default Node Terminal UI behave predictably in mainstream interactive
terminals on macOS, Linux, and Windows while preserving Textual as the automatic
fallback and retaining the retired Prompt Toolkit implementation.

## Evidence and scope

The current UI already uses portable Node/Python process APIs and passes the
Python bridge command as JSON. The remaining compatibility risks are concentrated
in terminal feature negotiation, Windows file replacement, home-directory
discovery, and terminal cleanup after failures.

This slice does not add terminal-specific image protocols such as Kitty graphics,
Sixel, or iTerm inline images. The working indicator remains character based.

## Design

### 1. Terminal capability profile

Add a pure module that derives one immutable profile from `platform`, terminal
environment variables, and stdin/stdout TTY state. It will report:

- whether the UI can run interactively;
- whether ANSI colors should be emitted (`NO_COLOR` and `FORCE_COLOR` aware);
- whether the terminal supports the enhanced keyboard protocol;
- whether Unicode and animation are appropriate;
- the effective home directory (`HOME`, then `USERPROFILE`, then
  `HOMEDRIVE` + `HOMEPATH`).

The full-screen UI requires both stdin and stdout to be TTYs. Unsupported
non-interactive launches fail before the bridge starts so the Python launcher can
perform its existing Textual fallback.

Enhanced keyboard negotiation is enabled only for known supporting terminals
(Kitty, WezTerm, Ghostty, and foot). Bracketed paste and alternate-screen control
remain part of the baseline interactive profile.

### 2. ANSI and rendering integration

Color SGR codes are configured once from the capability profile while structural
screen-control sequences remain enabled. `TERM=dumb` and `NO_COLOR` therefore do
not leak styling codes, but ordinary full-screen terminals retain cursor and
screen control.

Rendering receives the effective home directory and the animation capability,
so Windows paths can still be shortened and reduced-motion behavior remains
consistent.

### 3. Cross-platform local state persistence

Replace the fixed `.tmp` write with a same-directory unique temporary file.
Attempt atomic rename first. If Windows rejects replacement of an existing file,
move the destination to a rollback backup, install the new file, and restore the
backup if that second move fails. Temporary and backup files are always cleaned.
Persistence errors are contained and reported as a false return instead of
crashing the interactive UI.

### 4. Lifecycle hardening

Terminal setup and restoration become idempotent. Cleanup runs for ordinary exit,
bridge exit, signals, uncaught exceptions, and unhandled rejections. Child
termination errors are contained. Fatal errors are written after terminal
restoration so the user sees a readable diagnosis rather than a corrupted screen.

## Verification

Each implementation slice gets pure unit tests with simulated macOS, Linux,
Windows Terminal, legacy Windows, non-TTY, `NO_COLOR`, and replacement failures.
Process-level tests cover early rejection and cleanup. Verification remains
targeted; the full suite is intentionally not run.
