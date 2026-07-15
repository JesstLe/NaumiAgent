# HAR-07.1a Typed Harness Receipt Protocol Plan

## Scope

Implement one prerequisite only: carry the already-persisted Harness completion receipt from the
Python bridge to terminal clients as a typed `harness/receipt` event. Keep the existing `ui/message`
notice during the compatibility window so current New UI and Textual TUI remain user-visible.

This slice does not implement the compact Harness card, detail view, explain/replay requests, keyboard
interaction, or resume revision replay. Those remain separate HAR-07 slices.

## Contract

- Server event: `harness/receipt`.
- Payload schema version: `1`.
- Required identity: non-empty `run_id` and valid Harness status.
- Valid status: `completed_verified|completed_unverified|blocked`.
- Bounded normalized arrays: changed files, checks, criteria, warnings.
- The bridge emits the typed event only after `harness_completion_receipt`, whose service path persists
  the receipt before publishing the engine event.
- Frontend state stores one receipt per run id and ignores identical or older revisions. This state is
  not rendered as a second card in this slice.

## TDD steps

1. Add Python RED tests for the new enum/contract entry and Bridge event ordering/payload.
2. Add Node RED tests for strict normalization and state deduplication.
3. Implement Python event emission, JSON contract, JS normalization, and bounded state storage.
4. Run only `tests/unit/test_ui_protocol.py`, selected `test_ui_bridge.py`,
   `frontend/terminal-ui/test/protocol.test.js`, and selected `state.test.js`.
5. Run a real Bridge method scenario with a persisted-shape Harness receipt and verify one typed event
   plus one compatibility UI message.

## Acceptance

- A valid receipt crosses Python JSONL and JS normalization without field loss.
- Missing run id, invalid status, incompatible schema, and oversized collections fail or truncate at a
  documented boundary.
- Duplicate receipt delivery does not create duplicate frontend state.
- Existing completion receipt and Harness system notice behavior remains unchanged.
- No frontend failure classification or receipt mutation is introduced.
