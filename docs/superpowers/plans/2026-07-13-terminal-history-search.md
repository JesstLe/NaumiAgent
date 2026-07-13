# Terminal Project History Search Implementation Plan

> **Scope:** Implement one Terminal UI M2 slice: persistent project input history and a keyboard-complete `Ctrl+R` reverse-search experience. Chat/task mode and Bridge v2 remain separate work.

## Goal

Users can recover previously submitted prompts across sessions and process restarts without leaving the composer. Search must be reversible, keyboard-driven, Unicode-safe, bounded on disk, and must never send a message merely because a search result was accepted.

## Product Contract

### Persistence

- Submitted non-empty input is stored once in the project-local Terminal UI state file.
- History is shared by sessions opened from the same project directory.
- Adjacent duplicate submissions are collapsed.
- The newest entries win when the bounded store reaches its limit.
- Corrupt entries are ignored; unknown future store versions remain read-only.
- Session drafts and delivery outbox remain session-scoped.

### Search lifecycle

- `Ctrl+R` opens reverse search and preserves the current draft and cursor.
- Typing and bracketed paste update the search query, not the composer draft.
- Matching is case-insensitive substring search over the full submitted text.
- Results are newest-first and deduplicated without destroying the stored order.
- Repeated `Ctrl+R`, `Down`, or `Tab` selects an older result.
- `Up` selects a newer result.
- `Enter` accepts the selected result into the composer and closes search; it does not submit.
- `Esc` cancels search and restores the original draft and cursor.
- Backspace edits the query. An empty result set remains visible and recoverable.
- Permission prompts keep priority over history search shortcuts.

### Presentation

- The footer shows a dedicated `历史搜索` panel while search is active.
- It displays the query, match count, selected result, and concise key help.
- Multiline history entries are flattened only for candidate preview; acceptance restores exact text.
- The slash-command completion panel is hidden while history search owns the keyboard.
- The normal help row advertises `Ctrl+R 历史`.

## Architecture

### `src/history-search.js`

Owns the transient state machine and pure operations:

- create/reset search state
- open/cancel search while preserving a draft snapshot
- update query and recompute matches
- move/cycle selection with clamped or wrapped behavior
- accept the selected exact history entry into the composer

The module uses the existing input-buffer setters so cursor math stays grapheme-safe.

### `src/input-buffer.js`

- Register raw control keys (`Ctrl+R`, `Esc`, `Tab`).
- Keep submitted-history normalization in one place.
- Export a bounded history normalizer for state-store boundaries.

### `src/ui-state-store.js`

- Bump the store schema from v2 to v3.
- Add project-level `input_history` beside `sessions`.
- Migrate v1/v2 stores without losing session snapshots.
- Enforce count and aggregate-size bounds before reads and writes.
- Expose explicit get/set methods instead of mutating store internals from `index.js`.

### `src/index.js`

- Load project history before the first interaction.
- Route active-search keys before normal composer navigation/submission.
- Persist history immediately after a successful local submission.
- Preserve search state through redraws, but close it on session replay/change because its preserved draft belongs to the previous composer snapshot.

### `src/components/footer.js`

- Render search state as its own semantic footer section.
- Suppress slash completion while search is open.
- Keep all lines within terminal width.

## Test-Driven Work

### Task 1: Search state and input protocol

Write failing unit tests for:

- tokenizer recognition of `Ctrl+R`, `Esc`, and `Tab`
- newest-first Unicode/multiline matching
- repeat-cycle and arrow selection
- acceptance without submission
- cancellation restoring exact draft/cursor
- empty matches and backspace recovery

Implement the smallest pure state machine that satisfies these contracts.

Targeted command:

```bash
node --test test/input-buffer.test.js test/history-search.test.js
```

### Task 2: Footer ownership and keyboard precedence

Write failing component tests for:

- active search panel content and width
- multiline preview flattening
- slash completion suppression
- help row shortcut

Implement `HistorySearchFooter` and add it before command completion.

Targeted command:

```bash
node --test test/components.test.js test/render.test.js
```

### Task 3: Project persistence and migration

Write failing store tests for:

- v3 round trip across sessions
- v2 and v1 migration
- malformed history filtering
- entry-count and aggregate-size bounds
- unknown future-version write protection

Implement v3 storage and explicit history accessors.

Targeted command:

```bash
node --test test/ui-state-store.test.js
```

### Task 4: Process integration

Add a process-level fixture scenario proving:

- submit two prompts
- exit and restart with the same project state file
- open `Ctrl+R`, search, accept without sending
- second Enter sends the accepted prompt
- Esc restores a pre-existing draft

Use the fake JSONL Bridge for deterministic key sequencing, then run a real Bridge smoke with `/doctor` followed by history recovery.

Targeted command:

```bash
node --test test/index-process.test.js
```

### Task 5: Documentation and release evidence

- Mark project history search complete in module 02.
- Keep chat/task linkage and Bridge v2 explicitly pending.
- Run syntax checks and the full Terminal UI Node suite because this slice changes shared keyboard dispatch and persisted schema.
- Run the focused Python Bridge suite to catch protocol regressions.
- Commit implementation and documentation separately, push the feature branch, merge to `main`, verify the merged result, and push `main`.

## Verification Matrix

| Risk | Evidence |
|---|---|
| Search accepts and accidentally sends | process test observes no Bridge echo until a second Enter |
| Draft lost on cancel | unit and process tests compare exact text and cursor |
| Session switch leaks search draft | state-reset test and process session replay case |
| History disappears after restart | store round trip and two-process test |
| Old state becomes unreadable | v1/v2 migration tests |
| Corrupt or huge store destabilizes TUI | sanitizer boundary tests |
| Narrow terminal overflows | component width assertions |
| Slash completion competes for keys | component and process precedence tests |

## Explicit Exclusions

- Fuzzy ranking, tokenization, and semantic history search.
- Shell-wide history shared outside the current project.
- Cloud sync or multi-client history conflict resolution.
- Chat/task composer modes and `/task` creation.
- Bridge v2 durable idempotency and reconnect sequence replay.
