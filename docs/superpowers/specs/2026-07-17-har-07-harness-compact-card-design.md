# HAR-07.2 Harness Compact Completion Card Design

## Scope

This slice makes the authoritative typed `harness/receipt` visible inside the existing New UI
`CompletionReceiptCard`. It does not create a second card, open Explain/Replay details, add keyboard
interactions, restore receipts after reconnect, or change the Textual TUI. Those remain HAR-07.3
through HAR-07.6.

## Dependency boundary

- The generic `completion/receipt` remains authoritative for duration, user summary, validations,
  workspace changes, Git state, approvals, risks, and next actions.
- The typed `harness/receipt` remains authoritative for Harness completion status, trusted check
  results, acceptance-criterion evidence links, and Harness warnings.
- Both events use the durable ChatRun `run_id`. The Engine emits `harness/receipt` before the generic
  completion receipt, but the reducer must support either arrival order and later revisions.
- The UI computes only bounded counts and documented label/color mappings. It must not infer a new
  failure class or change either receipt.

## Selected design: one card, two immutable inputs

The reducer joins both receipts by exact `run_id` and stores the current Harness payload on the
existing completion-receipt message as `harnessReceipt`. If Harness arrives first, it remains in the
bounded cache until the generic receipt arrives. If Harness arrives later with a newer revision, the
same message is updated in place and the render cache is invalidated.

Once the typed Harness data is rendered, the Python Bridge stops adapting
`harness_completion_receipt` into a compatibility `ui/message`. The raw `engine/event` and typed
`harness/receipt` remain available, so this removes duplicate user-visible text without losing
diagnostics or protocol data.

## Compact visual hierarchy

The existing card title remains `完成回执`. Harness adds a compact block directly after the generic
summary:

1. Status row: `Harness 已验证|未验证|阻塞` plus check, criterion, and unique evidence-reference
   counts.
2. Non-passing checks: up to two rows with exact check id and status-specific Chinese wording.
3. Criterion shortfall: one row when one or more criteria are unsatisfied.
4. Warnings: up to two bounded rows, followed by an overflow count.

Semantic colors are mechanical:

- verified/passed/satisfied: green;
- unverified, missing, stale, timeout, cancellation, policy, infrastructure, warnings: yellow;
- blocked and explicit failed checks: red;
- identifiers and neutral counts: cyan or default text.

Infrastructure errors and policy blocks must never be labeled as test failures. With ANSI disabled,
the Chinese labels preserve the same distinctions.

## Width and compatibility

All rows use the existing ANSI-aware box renderer and wrapping rules. The card must remain within
80, 120, and 200 columns with Chinese wide characters. A generic completion receipt without a
Harness peer renders exactly as before. A Harness receipt without a generic peer remains cached and
does not create a visible message.

## Acceptance criteria

- One run produces exactly one visible `完成回执` card and no `Harness 完成回执` compatibility
  notice.
- Either event arrival order joins by exact `run_id`; unrelated runs never cross-attach.
- A newer Harness revision updates the existing card without adding a message; equal/older revisions
  are ignored.
- Verified, unverified, blocked, failed-check, and infrastructure-error states are distinguishable by
  text and semantic color.
- Check, criterion, and evidence counts are deterministic projections of the typed payload; evidence
  ids are de-duplicated.
- Rendering at 80/120/200 columns never exceeds the requested visible width.
- Existing generic receipt behavior and HAR-07.1 Explain/Replay caches remain unchanged.

