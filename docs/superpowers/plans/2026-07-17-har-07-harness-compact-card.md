# HAR-07.2 Harness Compact Completion Card Plan

## Scope

Merge typed Harness facts into the existing New UI completion card. Do not implement the detail
view, recovery subscription, shortcuts, evidence browser, or Textual parity in this slice.

## TDD tasks

1. Add reducer RED tests for Harness-first, completion-first, unrelated run ids, newer revision
   replacement, and the invariant of one visible receipt message.
2. Implement exact-`run_id` state joining and render-cache invalidation while retaining the existing
   bounded Harness cache.
3. Add card RED tests for verified, unverified, blocked, failed check, infrastructure failure,
   evidence de-duplication, warning bounds, no-Harness compatibility, and 80/120/200 widths.
4. Extend the existing card and message component to consume both immutable receipts without
   classifying failures in the frontend.
5. Change the Bridge RED test to require one typed Harness event and no compatibility UI message,
   then suppress only that obsolete adapter output.
6. Run focused Node card/state tests, selected Python Bridge tests, frontend lint, targeted Ruff, and
   a Bridge-to-normalizer-to-render scenario. Do not run the full repository suite.
7. Update HAR-07 progress, self-review the UX and boundary cases, commit this one feature, merge it
   into current `main`, and push.

## Verification commands

- `node --test frontend/terminal-ui/test/completion-receipt-card.test.js`
- selected `frontend/terminal-ui/test/state.test.js` cases for Harness receipt joining
- selected `tests/unit/test_ui_bridge.py` cases for Harness and generic completion receipts
- `ruff check src/naumi_agent/ui/bridge.py tests/unit/test_ui_bridge.py`
- frontend lint for touched files

## Completion gate

The slice is complete only when one real event sequence produces one visible combined card, all
specified width snapshots stay bounded, and the old Harness compatibility notice is absent while
the typed event remains present.

