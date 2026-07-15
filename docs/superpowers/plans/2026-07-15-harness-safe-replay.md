# HAR-05 Safe Harness Replay Implementation Plan

> **Scope:** implement HAR-05 only. Do not advance ARC modules or HAR-06/HAR-07 in this plan.

**Goal:** Provide deterministic, cross-process replay of durable Harness facts without executing tools,
models, checks, or sessions.

**Architecture:** Add an immutable replay baseline to Harness Store v2, a dependency-free replay engine
that canonicalizes run facts and verifies referenced artifacts, then expose one service method through
both slash and Agent Tool surfaces.

**Tech stack:** Python 3.13, dataclasses, asyncio, aiosqlite, pytest, existing Harness Store/Explainer.

---

## Task 1: Define Replay values and canonicalization

**Files:**

- Create: `src/naumi_agent/harness/replay_models.py`
- Create: `src/naumi_agent/harness/replay.py`
- Create: `tests/unit/test_harness_replay.py`

1. Write failing tests for stable timeline ordering, canonical manifest digest, stable explanation digest,
   `chat-run://` summary verification, running/missing-start anomalies, and 50 identical replays.
2. Run only `tests/unit/test_harness_replay.py` and confirm RED because Replay types do not exist.
3. Implement frozen result models, canonical JSON helpers, explicit manifest/rule versions, timeline
   assembly, explanation serialization, and evidence verification.
4. Re-run the same file until GREEN.
5. Self-review status precedence, untrusted-content rendering, maximum field sizes, tie-break behavior,
   and absence of execution dependencies.

## Task 2: Persist immutable Replay baselines in Store v2

**Files:**

- Modify: `src/naumi_agent/harness/store.py`
- Modify: `src/naumi_agent/harness/explain.py`
- Modify: `tests/unit/test_harness_store.py`
- Create: `tests/unit/test_harness_replay_store.py`

1. Write failing tests for v1-to-v2 migration, idempotent baseline insert, conflicting overwrite rejection,
   cascade deletion, a new Store instance reading the same baseline, and unsupported future schema.
2. Run only the two touched Store test files and confirm RED.
3. Export store schema and explain rule versions; add `HarnessStoredReplayBaseline`, Store read/write APIs,
   the additive v2 table, and transactional migration.
4. Capture a baseline after a new run finishes. A capture failure must not replace the completed Run;
   it must be observable as a missing/legacy baseline during Replay.
5. Re-run the same Store tests until GREEN and ensure existing v1 data remains readable.

## Task 3: Add workspace-isolated Replay service

**Files:**

- Modify: `src/naumi_agent/harness/service.py`
- Modify: `src/naumi_agent/harness/replay.py`
- Modify: `tests/unit/test_harness_replay.py`

1. Write failing async tests for latest/explicit lookup, foreign workspace isolation, damaged Store,
   legacy baseline creation, missing/corrupt artifact, and changed rule output.
2. Confirm RED with only `tests/unit/test_harness_replay.py`.
3. Implement `HarnessService.replay_run()` using the Store and pure replay engine. Do not inject the check
   runner, router, registry, or session.
4. Resolve artifact paths strictly inside `workspace_root`; reject symlink escape and never render file
   contents.
5. Re-run focused tests until GREEN.

## Task 4: Add slash and Agent Tool surfaces

**Files:**

- Modify: `src/naumi_agent/harness/tools.py`
- Modify: `src/naumi_agent/main.py`
- Modify: `tests/unit/test_harness_tools.py`
- Modify: `tests/unit/test_harness_surfaces.py`

1. Write failing tests that require `harness_replay`, read-only/concurrency-safe metadata, argument
   validation, `/harness replay latest`, explicit run id, identical service result, and actionable usage.
2. Run only the two surface test files and confirm RED.
3. Register `HarnessReplayTool`, add slash routing, and use one `render_harness_replay()` function.
4. Re-run the two files until GREEN.

## Task 5: Focused verification and real cross-process acceptance

**Files:**

- Create: `tests/integration/test_harness_replay_store.py`
- Modify: `docs/development/harness/HAR-05-safe-replay.md`

1. Build a real temporary Git workspace and SQLite Store, finish a run with one real artifact and one
   normalized tool evidence record, then close the process.
2. Start a separate Python process with a new Store/Service instance and replay the run. Assert
   `reproduced`, stable digests, zero tool/model/check calls, and a Chinese receipt.
3. Delete the artifact and assert `partial`; recreate modified bytes and assert `corrupt`.
4. Run only:

   ```bash
   ruff check src/naumi_agent/harness/replay.py \
     src/naumi_agent/harness/replay_models.py \
     src/naumi_agent/harness/store.py \
     src/naumi_agent/harness/service.py \
     src/naumi_agent/harness/tools.py \
     tests/unit/test_harness_replay.py \
     tests/unit/test_harness_replay_store.py \
     tests/unit/test_harness_tools.py \
     tests/unit/test_harness_surfaces.py \
     tests/integration/test_harness_replay_store.py
   pytest -q \
     tests/unit/test_harness_replay.py \
     tests/unit/test_harness_replay_store.py \
     tests/unit/test_harness_tools.py \
     tests/unit/test_harness_surfaces.py \
     tests/integration/test_harness_replay_store.py
   ```

5. Do not run the full repository test suite.
6. Record implementation status, exact focused commands, real-scenario evidence, limitations, and
   self-review in HAR-05.
7. Commit HAR-05 independently, fast-forward main, push, and remove the feature worktree.

## Completion gate

- The result is not complete if Replay can invoke or reconstruct a destructive operation.
- The result is not complete if a foreign workspace run can be distinguished from a missing run.
- The result is not complete if artifact bytes or raw tool output appear in the receipt.
- The result is not complete if the first legacy baseline is reported as historically reproduced.
- The result is not complete without a real cross-process run and the focused tests above.
