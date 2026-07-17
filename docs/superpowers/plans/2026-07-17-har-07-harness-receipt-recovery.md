# HAR-07.4a Harness Receipt Recovery Plan

## Scope

Recover the already-durable typed Harness receipt during explicit session resume. Keep detail views,
automatic reconnect subscriptions, receipt shortcuts, and TUI parity in later HAR-07 slices.

## TDD tasks

1. Add Harness Store RED tests for workspace+session filtering, ordering, limits, missing schema, and
   corrupt persisted rows.
2. Implement the bounded `list_session_runs()` query by reusing the existing authoritative decoder.
3. Add Bridge RED tests proving Harness-before-generic ordering, incomplete-run omission,
   cross-session/workspace isolation, stable failure reporting, and request-id correlation.
4. Implement resume recovery without changing ordinary live-run event behavior.
5. Add reducer RED tests proving clean replay clears all Harness caches while append replay preserves
   them for revision deduplication.
6. Add one real integration test using new Session/ChatRun/Harness Store instances and the actual
   Bridge-to-Node normalize/reduce/render chain at 80/120/200 columns.
7. Run only the touched Store, Bridge, reducer, integration, Ruff, compile, and JavaScript syntax
   checks; update HAR-07 progress, self-review, commit, merge, and push.

## Completion gate

The slice is complete only when a process restart plus explicit resume reconstructs the same combined
card from durable stores, without cross-workspace leakage, duplicate cards, or transient UI-state
restoration.

