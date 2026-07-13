# Terminal Slash Completion Keyboard Plan

## Goal

Complete the existing slash-command footer with deterministic keyboard ownership. Users must be able to select a command without a mouse, accept it without executing it, cancel the panel, and then deliberately submit.

## Contract

- The panel is open only when the composer begins with `/`, contains no whitespace, has candidates, and has not been dismissed for the current input.
- `Down` and `Tab` select the next candidate; `Up` selects the previous candidate. Selection wraps at both ends.
- `Enter` accepts a selected canonical command when the composer is still partial and dismisses completion for that input. If the composer already equals the selected canonical command, Enter falls through to normal submission.
- `Ctrl+Enter` remains the explicit unconditional submit shortcut.
- `Esc` dismisses the panel while preserving the exact composer text and cursor.
- Any composer edit after dismissal re-enables completion and resets selection to the first candidate.
- History search and permission prompts keep higher priority than slash completion.
- The selected row has a non-color marker so selection remains visible without ANSI colors.
- A whitespace separator, including a trailing space or newline, closes completion.

## Design

Create `src/slash-completion.js` as a pure interaction layer over the existing backend/local command registry:

- derive visible candidates from `getSlashCommandCompletions`
- synchronize selection when input changes
- move selection with wrapping
- accept the selected command through the Unicode-safe input setter
- dismiss only for the current input value

Store transient selection state in `createInitialState()`. Do not persist it: a reopened process should derive a fresh panel from the restored composer.

Update `index.js` keyboard priority in this order:

1. process exit and permission response
2. active history search
3. `Ctrl+R` history opening
4. explicit `Ctrl+Enter` submit
5. active slash completion
6. normal composer and timeline keys

Update `CommandCompletionFooter` to use the interaction module and prefix the selected candidate with `>`.

## TDD Tasks

1. Unit tests: panel derivation, trailing whitespace closure, wrapped selection, dismissal/edit reset, exact acceptance, and no message side effects.
2. Component tests: selected marker, candidate changes, width bounds, and hidden state after dismissal.
3. Process test: type `/d`, choose `/doctor`, accept with Enter, assert no `doctor` protocol call, then submit with a second Enter and assert exactly one call.
4. Regression: history search still owns `Tab/Up/Down/Enter/Esc`; `Ctrl+Enter` still submits directly.
5. Verification: syntax, affected Node tests, full Node gate, real Bridge `/doctor` keyboard path.

## Exclusions

- Argument, file, model, and path completion.
- Fuzzy scoring and recently-used command ranking.
- Mouse selection.
- Chat/task mode and Bridge v2 protocol changes.
