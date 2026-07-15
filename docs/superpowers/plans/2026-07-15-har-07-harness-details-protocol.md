# HAR-07.1b Harness Explain/Replay Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose durable Harness Explain and safe Replay as strict revisioned JSONL request/response messages and bounded New UI state without adding visible cards.

**Architecture:** The Python protocol validates exact run ids before the Bridge calls the existing workspace-scoped `HarnessService`. A dedicated serializer emits bounded schema-v1 lookups; the JavaScript protocol independently validates the same whitelist and the reducer keeps the newest revision per run. Explicit requests always resend the authoritative immutable revision so packet loss is recoverable and duplicate delivery is harmless.

**Tech Stack:** Python 3.13, dataclasses, asyncio JSONL Bridge, pytest, Node.js 20 ESM, `node:test`.

## Global Constraints

- Implement HAR-07.1b only; do not add cards, detail rendering, keyboard interaction, resume subscription, TUI parity, or ARC changes.
- Both operations must reuse `HarnessService.explain_run()` and `HarnessService.replay_run()`; no model, tool, check, or chat execution is allowed.
- User-visible errors are Chinese and typed lookup failures do not require parsing Markdown.
- Public text is capped at 500 characters; request run ids use the existing 1-128 character Harness grammar.
- Explain caps: 20 failure classes, 20 findings, 50 checks, 100 evidence records; Replay caps: 200 timeline events, 100 artifacts, 50 anomalies, 50 differences.
- New UI keeps at most 100 Explain entries and 100 Replay entries and does not render a second card in this slice.
- Run only focused module tests and one real SQLite-to-Bridge-to-Node scenario; do not run the full test suite.

---

### Task 1: Python request contract and bounded response serializer

**Files:**
- Create: `src/naumi_agent/ui/harness_protocol.py`
- Modify: `src/naumi_agent/ui/protocol.py`
- Create: `tests/unit/test_ui_harness_protocol.py`
- Modify: `tests/unit/test_ui_protocol.py`

**Interfaces:**
- Consumes: `HarnessExplainLookup`, `HarnessReplayLookup`, and the existing `validate_run_id()` grammar.
- Produces: `ClientEventType.HARNESS_EXPLAIN_REQUEST`, `ClientEventType.HARNESS_REPLAY_REQUEST`, `ServerEventType.HARNESS_EXPLAIN`, `ServerEventType.HARNESS_REPLAY`, `harness_explain_payload(run_id, lookup, revision=1)`, and `harness_replay_payload(run_id, lookup, revision=1)`.

- [ ] **Step 1: Write failing request-normalization tests**

```python
def test_protocol_normalizes_harness_detail_requests() -> None:
    for event_type in (
        ClientEventType.HARNESS_EXPLAIN_REQUEST,
        ClientEventType.HARNESS_REPLAY_REQUEST,
    ):
        record = normalize_client_record({
            "type": event_type,
            "payload": {"run_id": "run:detail-1", "known_revision": "3"},
        })
        assert record["payload"] == {"run_id": "run:detail-1", "known_revision": 3}

@pytest.mark.parametrize("run_id", ["", " ", "../other", "x" * 129])
def test_protocol_rejects_invalid_harness_detail_run_ids(run_id: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        normalize_client_record({
            "type": ClientEventType.HARNESS_EXPLAIN_REQUEST,
            "payload": {"run_id": run_id},
        })
```

- [ ] **Step 2: Run RED request tests**

Run: `/Users/lv/Workspace/NaumiAgent/.venv/bin/pytest -q tests/unit/test_ui_protocol.py -k 'harness_detail'`

Expected: failure because the four enum members and payload normalization do not exist.

- [ ] **Step 3: Add enum members and a shared strict request normalizer**

```python
class ClientEventType(StrEnum):
    HARNESS_EXPLAIN_REQUEST = "harness/explain/request"
    HARNESS_REPLAY_REQUEST = "harness/replay/request"

class ServerEventType(StrEnum):
    HARNESS_EXPLAIN = "harness/explain"
    HARNESS_REPLAY = "harness/replay"

def _normalize_harness_detail_request(payload: dict[str, Any]) -> dict[str, Any]:
    run_id = validate_run_id(str(payload.get("run_id") or ""))
    raw_revision = payload.get("known_revision", 0)
    if isinstance(raw_revision, bool):
        raise ValueError("Harness known_revision 必须是非负整数。")
    try:
        known_revision = int(raw_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("Harness known_revision 必须是非负整数。") from exc
    if known_revision < 0 or known_revision > 2_147_483_647:
        raise ValueError("Harness known_revision 必须是非负整数。")
    return {"run_id": run_id, "known_revision": known_revision}
```

Both client event branches return this helper. Importing `validate_run_id` in the transport module is allowed because it reuses the canonical grammar rather than duplicating it.

- [ ] **Step 4: Write failing bounded-serializer tests**

Construct real `HarnessRunExplanation` and `HarnessReplayResult` dataclasses. Assert exact top-level and nested keys, enum-to-string conversion, `revision == 1`, public text truncation, every collection boundary, non-negative durations, and typed `not_found`/`unavailable` payloads without `explanation` or `result`.

- [ ] **Step 5: Run RED serializer tests**

Run: `/Users/lv/Workspace/NaumiAgent/.venv/bin/pytest -q tests/unit/test_ui_harness_protocol.py`

Expected: collection error because `naumi_agent.ui.harness_protocol` does not exist.

- [ ] **Step 6: Implement explicit whitelist serializers**

```python
def harness_explain_payload(
    run_id: str,
    lookup: HarnessExplainLookup,
    *,
    revision: int = 1,
) -> dict[str, Any]:
    payload = _lookup_header(run_id, lookup.status, lookup.message, revision)
    if lookup.status == "ok" and lookup.explanation is not None:
        payload["explanation"] = _serialize_explanation(lookup.explanation)
    return payload

def harness_replay_payload(
    run_id: str,
    lookup: HarnessReplayLookup,
    *,
    revision: int = 1,
) -> dict[str, Any]:
    payload = _lookup_header(run_id, lookup.status, lookup.message, revision)
    if lookup.status == "ok" and lookup.result is not None:
        payload["result"] = _serialize_replay(lookup.result)
    return payload
```

Implement each nested mapping field explicitly. `_lookup_header()` validates the run id, lookup status, and positive revision; `_text()`, `_texts()`, and `_nonnegative_int()` enforce the documented limits. Do not call `asdict()` over an entire lookup.

- [ ] **Step 7: Run focused GREEN checks**

Run: `/Users/lv/Workspace/NaumiAgent/.venv/bin/pytest -q tests/unit/test_ui_protocol.py -k 'harness_detail' tests/unit/test_ui_harness_protocol.py`

Run: `/Users/lv/Workspace/NaumiAgent/.venv/bin/ruff check src/naumi_agent/ui/protocol.py src/naumi_agent/ui/harness_protocol.py tests/unit/test_ui_protocol.py tests/unit/test_ui_harness_protocol.py`

Expected: all selected tests pass and Ruff reports no errors.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/naumi_agent/ui/protocol.py src/naumi_agent/ui/harness_protocol.py tests/unit/test_ui_protocol.py tests/unit/test_ui_harness_protocol.py
git commit -m "feat(ui): define harness detail protocol"
```

### Task 2: Bridge dispatch and explicit revision resend

**Files:**
- Modify: `src/naumi_agent/ui/bridge.py`
- Modify: `tests/unit/test_ui_bridge.py`

**Interfaces:**
- Consumes: normalized `{run_id, known_revision}` requests and Task 1 payload serializers.
- Produces: `query_harness_explain(payload, request_id=...)` and `query_harness_replay(payload, request_id=...)`, each emitting exactly one correlated typed response.

- [ ] **Step 1: Write failing Bridge dispatch tests**

Attach a real service-shaped object to `_FakeEngine.harness_service` whose async methods return real lookup dataclasses. Send both client request events through `handle_client_record()` and assert the response type, original request id, run id, revision, lookup status, and nested result status. Send the same request again with `known_revision: 1` and assert the same payload is resent.

- [ ] **Step 2: Write failing unavailable/error tests**

Assert an engine without `harness_service` receives a typed `unavailable` response. Attach a service that raises `RuntimeError("private path")` and assert the Bridge remains alive, emits a Chinese typed unavailable message, and does not expose `private path`.

- [ ] **Step 3: Run RED Bridge tests**

Run: `/Users/lv/Workspace/NaumiAgent/.venv/bin/pytest -q tests/unit/test_ui_bridge.py -k 'harness_explain_request or harness_replay_request or harness_detail_unavailable'`

Expected: failure because the Bridge does not dispatch the new events.

- [ ] **Step 4: Implement dispatch and read-only lookup methods**

```python
if event_type == ClientEventType.HARNESS_EXPLAIN_REQUEST:
    await self.query_harness_explain(payload, request_id=request_id)
    return
if event_type == ClientEventType.HARNESS_REPLAY_REQUEST:
    await self.query_harness_replay(payload, request_id=request_id)
    return

async def query_harness_explain(self, payload: dict[str, Any], *, request_id: str) -> None:
    run_id = str(payload["run_id"])
    service = getattr(self.engine, "harness_service", None)
    if service is None:
        lookup = HarnessExplainLookup(status="unavailable", message=_HARNESS_UNAVAILABLE)
    else:
        try:
            lookup = await service.explain_run(run_id)
        except Exception as exc:
            self._trace_harness_lookup_failure("explain", exc)
            lookup = HarnessExplainLookup(status="unavailable", message=_HARNESS_UNAVAILABLE)
    await self.emit(
        ServerEventType.HARNESS_EXPLAIN,
        harness_explain_payload(run_id, lookup),
        request_id=request_id,
    )
```

Implement the symmetric Replay method. The trace records only operation and exception class, never the exception message. `known_revision` is intentionally advisory in schema v1; explicit requests always resend revision 1.

- [ ] **Step 5: Run focused GREEN checks**

Run: `/Users/lv/Workspace/NaumiAgent/.venv/bin/pytest -q tests/unit/test_ui_bridge.py -k 'harness_explain_request or harness_replay_request or harness_detail_unavailable or typed_harness_receipt'`

Run: `/Users/lv/Workspace/NaumiAgent/.venv/bin/ruff check src/naumi_agent/ui/bridge.py tests/unit/test_ui_bridge.py`

Expected: all selected tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/naumi_agent/ui/bridge.py tests/unit/test_ui_bridge.py
git commit -m "feat(ui): serve harness detail lookups"
```

### Task 3: New UI normalization, revision state, and real durable scenario

**Files:**
- Modify: `frontend/terminal-ui/protocol-contract.json`
- Modify: `frontend/terminal-ui/src/protocol.js`
- Modify: `frontend/terminal-ui/src/state.js`
- Modify: `frontend/terminal-ui/test/protocol.test.js`
- Modify: `frontend/terminal-ui/test/state.test.js`
- Create: `tests/integration/test_ui_harness_details_protocol.py`
- Modify: `docs/development/harness/HAR-07-completion-ui.md`

**Interfaces:**
- Consumes: Task 1/2 schema-v1 responses.
- Produces: `normalizeHarnessExplain()`, `normalizeHarnessReplay()`, `state.harnessExplanations`, and `state.harnessReplays`.

- [ ] **Step 1: Write failing Node protocol tests**

Add both request and response events to the contract assertions. Normalize one full Explain and one full Replay fixture containing extra private fields and oversized arrays; assert the documented whitelist and caps. Assert rejection for incompatible schema, revision `0`, invalid lookup status, missing successful result, non-boolean flags, malformed arrays, and invalid result status.

- [ ] **Step 2: Run RED Node protocol tests**

Run from `frontend/terminal-ui`: `node --test --test-name-pattern='harness explain|harness replay|protocol contract' test/protocol.test.js`

Expected: failure because the contract and normalizers do not know the new event types.

- [ ] **Step 3: Implement JSON contract and strict normalizers**

Add the two client and two server event names plus `harness_explain` and `harness_replay` sections that list fields, statuses, and limits. Route each server event in `normalizeServerPayload()`. Use a shared strict header validator and explicit nested mapping functions; unknown fields are dropped and all strings/collections use the documented caps.

- [ ] **Step 4: Write failing reducer revision tests**

```javascript
test("typed harness details keep newest bounded revisions without rendering", () => {
  const state = createInitialState();
  reduceServerEvent(state, { type: "harness/explain", payload: explainFixture(2) });
  reduceServerEvent(state, { type: "harness/explain", payload: explainFixture(1) });
  reduceServerEvent(state, { type: "harness/replay", payload: replayFixture(3) });
  assert.equal(state.harnessExplanations.run.revision, 2);
  assert.equal(state.harnessReplays.run.revision, 3);
  assert.equal(state.messages.length, 0);
});
```

Also insert 105 distinct runs into each cache and assert each retains exactly 100 newest inserted keys.

- [ ] **Step 5: Run RED reducer tests**

Run from `frontend/terminal-ui`: `node --test --test-name-pattern='typed harness details' test/state.test.js`

Expected: failure because the two caches and reducer cases do not exist.

- [ ] **Step 6: Implement bounded revision caches**

Initialize two null-prototype dictionaries. Route both events to a shared `addHarnessDetail(cache, payload)` helper that requires a run id and positive revision, ignores equal/older revisions, replaces newer revisions, and evicts the oldest key beyond 100. Do not append to `state.messages` or clear render caches.

- [ ] **Step 7: Add the real SQLite-to-Bridge-to-Node integration test**

Create a temporary Git workspace and actual `HarnessStore`. Persist and finish a `HarnessCompletionContract`, construct a new `HarnessService` over a new Store instance, attach it to a minimal Bridge engine, and send both typed requests. Assert two correlated responses and no tool/check/model calls. Pipe each JSON record to a Node subprocess importing `normalizeServerRecord()` and assert Node returns the same run id, revision, lookup status, Explain verified flag, and Replay status. Request a run from another workspace and assert `not_found`.

- [ ] **Step 8: Update HAR-07 progress accurately**

Move HAR-07.1b from “尚未完成” to an implemented section that names the four messages, immutable revision resend, bounded New UI caches, and real durable integration proof. Leave HAR-07.2 through HAR-07.6 pending.

- [ ] **Step 9: Run final focused verification**

Run: `/Users/lv/Workspace/NaumiAgent/.venv/bin/pytest -q tests/unit/test_ui_protocol.py -k 'harness' tests/unit/test_ui_harness_protocol.py tests/unit/test_ui_bridge.py -k 'harness' tests/integration/test_ui_harness_details_protocol.py`

Run from `frontend/terminal-ui`: `node --test --test-name-pattern='harness|protocol contract' test/protocol.test.js test/state.test.js`

Run from repo root: `/Users/lv/Workspace/NaumiAgent/.venv/bin/ruff check src/naumi_agent/ui/protocol.py src/naumi_agent/ui/harness_protocol.py src/naumi_agent/ui/bridge.py tests/unit/test_ui_protocol.py tests/unit/test_ui_harness_protocol.py tests/unit/test_ui_bridge.py tests/integration/test_ui_harness_details_protocol.py`

Run from `frontend/terminal-ui`: `node scripts/check-syntax.js`

Expected: all selected Python/Node tests pass, Ruff is clean, and all JavaScript files parse. Do not run the full suite.

- [ ] **Step 10: Self-review and commit Task 3**

Review every acceptance criterion in the design, inspect `git diff --check`, confirm no visible card or ARC files changed, and list any remaining HAR-07 work in the final report.

```bash
git add frontend/terminal-ui/protocol-contract.json frontend/terminal-ui/src/protocol.js frontend/terminal-ui/src/state.js frontend/terminal-ui/test/protocol.test.js frontend/terminal-ui/test/state.test.js tests/integration/test_ui_harness_details_protocol.py docs/development/harness/HAR-07-completion-ui.md
git commit -m "feat(ui): recover harness detail responses"
```
