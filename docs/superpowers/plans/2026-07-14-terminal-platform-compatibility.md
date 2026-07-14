# Terminal Platform Compatibility Implementation Plan

## Slice 1: capability negotiation

1. Add failing pure tests for platform/terminal profiles.
2. Implement `terminal-capabilities.js`.
3. Integrate color, keyboard negotiation, home directory, and animation policy.
4. Run only capability, ANSI, input, working-animation, and relevant render tests.
5. Commit and push.

## Slice 2: durable UI state

1. Add failing tests for unique temporary files, simulated Windows replacement,
   cleanup, and write failures.
2. Harden `saveUiStateStore` without changing the persisted schema.
3. Run only UI-state-store tests and syntax checks.
4. Commit and push.

## Slice 3: lifecycle recovery

1. Add process tests for non-TTY rejection and failure cleanup.
2. Make setup/restore idempotent and centralize fatal cleanup.
3. Contain bridge termination errors.
4. Run only relevant process tests and syntax checks.
5. Commit and push.

## Release verification

Run the three focused Node test groups, Python launcher/platform selectors, Ruff
on touched Python files if any, documentation governance, syntax checks, and a
real local pseudo-terminal smoke test. Fast-forward the verified commits to
`main` without touching unrelated worktrees.
