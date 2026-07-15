# HAR-07.1b Harness Explain/Replay Protocol Design

## Scope

This slice adds typed, revisioned request/response transport for the durable Harness Explain and
Replay facts that HAR-04 and HAR-05 already produce. It does not render a Harness card, add keyboard
shortcuts, subscribe on resume, or change the Textual TUI. Those remain HAR-07.2 through HAR-07.6.

## Dependency boundary

- HAR-05 is the authoritative durable source: `HarnessService.explain_run()` and
  `HarnessService.replay_run()` enforce workspace isolation and never rerun tools, models, checks, or
  chat sessions.
- HAR-07.1a supplies the `harness/receipt` run identity used by later UI interactions.
- HAR-07.1b only connects the existing service methods to the shared JSONL protocol and bounded New
  UI state. No ARC work is required for this vertical slice.

## Considered approaches

### A. Direct typed Bridge queries (selected)

The Bridge validates one exact run id, calls the existing Harness service, serializes a bounded
lookup, and emits one typed response. This keeps the Store and workspace boundary authoritative,
gives each payload an explicit schema, and is independently testable.

### B. Route queries through internal Engine events

This would add command events and response correlation to the ReAct runtime for a read-only UI
lookup. It creates coupling to active runs and makes recovery harder without improving correctness.

### C. Add one generic `harness/query` envelope

A generic discriminator would reduce event names but weaken field-level validation and force every
client to branch over heterogeneous results. Explain and Replay have stable, different schemas, so
separate messages are clearer.

## Protocol

### Client requests

- `harness/explain/request`
- `harness/replay/request`

Both payloads contain:

- `run_id`: required 1-128 character Harness run id. It accepts only letters, digits, `.`, `_`, `:`,
  and `-`; `latest` is not accepted on the typed UI path.
- `known_revision`: optional non-negative 32-bit integer, default `0`. It documents the client's
  current state and supports idempotent recovery.

An explicit request always returns the current authoritative payload, even when
`known_revision == revision`. This makes a lost response recoverable; the client reducer ignores the
duplicate by revision.

### Server responses

- `harness/explain`
- `harness/replay`

Every response contains `schema_version: 1`, `revision: 1`, the exact requested `run_id`,
`lookup_status` (`ok`, `not_found`, or `unavailable`), and a bounded Chinese `message`. Successful
Explain responses contain `explanation`; successful Replay responses contain `result`. Non-success
responses contain no fabricated result.

Revision `1` is sufficient for this slice because the UI entry point is a completed immutable
Harness receipt and both Explain and Replay are deterministic projections of that persisted run. A
future schema or mutable live-run subscription must introduce a durable monotonic revision rather
than guessing one from frontend state.

## Public payload limits

The Python serializer and JavaScript normalizer both enforce the same public limits:

- public text: 500 characters; run id: 128 characters;
- Explain: 20 failure classes, 20 findings, 50 checks, 100 evidence records;
- per finding: 50 check ids and 100 evidence ids;
- Replay: 200 timeline events, 100 artifacts, 50 anomalies, 50 differences;
- New UI cache: at most 100 Explain results and 100 Replay results.

Only documented fields cross the protocol. Dataclass internals, unknown mapping keys, absolute
private metadata, and unbounded Store values are dropped. URI/reference values are treated as public
text and bounded; the protocol never reads their targets.

## Bridge flow and errors

1. `normalize_client_record()` rejects missing, malformed, or oversized run ids and invalid
   revisions before service access.
2. The Bridge obtains `engine.harness_service`. If absent, it emits a typed `unavailable` lookup with
   a Chinese recovery message.
3. The existing service performs workspace-scoped durable lookup.
4. A dedicated UI serializer converts the dataclass lookup into a strict bounded payload.
5. The Bridge emits the response with the original request id. It does not emit a second
   `ui/message` and does not mutate the run.

Invalid client records remain ordinary protocol `bad_request` errors. Valid requests for missing or
temporarily unavailable data receive typed responses so a future detail view can render those states
without parsing error prose.

## Frontend state

`state.harnessExplanations` and `state.harnessReplays` are dictionaries keyed by `run_id`. A response
replaces the stored entry only when its revision is newer. Equal or older responses are no-ops. Each
dictionary evicts the oldest inserted run when it exceeds 100 entries. This slice stores data only;
it deliberately produces no second visible card.

## Verification

- Python protocol tests cover valid normalization plus empty, malformed, oversized run ids and
  invalid revisions.
- Python serializer tests cover whitelist behavior, limits, lookup errors, booleans, and numeric
  bounds.
- Bridge tests cover Explain and Replay dispatch, request-id correlation, explicit same-revision
  resend, unavailable service, and service exceptions without running tools or models.
- Node protocol tests cover both strict response schemas, field whitelists, limits, and malformed
  payload rejection.
- Node reducer tests cover newer/equal/older revisions, separate Explain/Replay caches, and the
  100-run cap without rendering messages.
- A real scenario writes a Harness run to a temporary SQLite Store, constructs a new HarnessService
  and Bridge, requests Explain and Replay, and passes the emitted JSON through the Node normalizer.

## Acceptance criteria

- A persisted Harness run can be explained and safely replayed through one correlated typed JSONL
  request/response per operation.
- Repeating a request with the received revision resends the same authoritative payload and does not
  duplicate frontend state.
- Missing/unavailable data is distinguishable without parsing Markdown or model prose.
- No request can access a run from another workspace.
- No Explain/Replay request invokes a model, tool, check command, or chat run.
- Existing `harness/receipt`, completion receipts, compatibility notices, and visible rendering are
  unchanged.
