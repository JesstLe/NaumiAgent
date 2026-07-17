# HAR-07.4a Harness Receipt Recovery Design

## Scope

This slice restores durable Harness completion receipts when a user explicitly resumes a persisted
session. It makes the HAR-07.2 combined completion card identical across a Bridge restart. It does
not restore transient focus/sidebar state, eagerly load Explain/Replay detail, add receipt keyboard
interactions, or implement the Textual parity gate.

## Current gap

`JsonlEngineBridge.resume_session()` already replays session messages and every durable generic
ChatRun completion receipt. Harness receipts are stored in a separate workspace-scoped SQLite Store
and are not replayed. A restored card therefore loses its authoritative Harness status, trusted
check results, criterion evidence counts, and warnings even though those facts remain durable.

## Selected design

### Store query

Add a bounded `HarnessStore.list_session_runs(workspace_root, session_id, limit)` query. Both
workspace and session are mandatory authority boundaries. Filtering only by session id is rejected
because separate workspaces may contain identical session-like ids. The query orders newest first,
uses the existing persisted run decoder, accepts limits 1-1000, and treats an absent pre-Harness
database as an empty result.

### Bridge replay ordering

After emitting `session/replayed` and historical chat messages, the Bridge loads at most 200 Harness
runs for the resumed workspace/session. It emits completed Harness receipts first, oldest to newest,
then emits generic ChatRun receipts oldest to newest. The frontend therefore caches typed Harness
facts invisibly before each generic receipt creates its one visible combined card; no intermediate
degraded card or duplicate compatibility message is produced.

Only persisted `HarnessCompletionReceipt` objects are emitted. Running or incomplete Harness rows do
not fabricate a completion state. All recovery events reuse schema v1/revision 1 and the resume
request id, so reducer idempotence remains `run_id + revision`.

If the Harness Store is unavailable or corrupt, session messages and generic receipts still recover.
The Bridge emits one fixed Chinese `harness_receipt_recovery_failed` error without exposing raw
database details, and records the exception in logs/debug evidence.

### Frontend session boundary

For a replay with `clear: true`, the reducer clears `harnessReceipts`, `harnessExplanations`, and
`harnessReplays` together with visible messages. This prevents invisible detail from a previously
open session surviving a deliberate clean resume. `clear: false` retains caches and relies on
revision idempotence, matching append-style replay behavior.

## Considered alternatives

- **List all workspace runs and filter in Bridge:** rejected because it performs unnecessary reads,
  weakens the Store authority boundary, and becomes unbounded as history grows.
- **Recover Harness receipt on every generic card with one request per run:** rejected because it
  creates N round trips, visible partial cards, and avoidable races.
- **Reconstruct Harness state from generic receipt fields:** rejected because the generic receipt is
  not the Harness Completion Gate authority.

## Acceptance criteria

- A new Bridge instance resuming a real persisted session emits Harness receipts before matching
  generic receipts and renders one combined card per ChatRun receipt.
- Only runs matching both canonical workspace and exact session id are recovered.
- Running Harness rows and Harness-only rows do not create visible completion cards.
- Repeating append-style resume does not duplicate cards or downgrade newer cached revisions.
- Clean resume drops previous-session Harness receipt/detail caches before replay.
- Store failure preserves message/generic receipt recovery and emits one stable, non-sensitive error.
- Real SQLite Session/ChatRun/Harness stores through Bridge JSONL, Node normalizer, reducer, and
  80/120/200-column renderer prove the restored Harness state.

