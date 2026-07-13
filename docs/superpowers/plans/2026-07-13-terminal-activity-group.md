# Terminal Run Activity Group Plan

## Goal

Aggregate one Agent run into one durable, incrementally updated activity card:

```text
run/started -> preparing -> generating -> executing
            <-> awaiting_permission -> summarizing
            -> completed | failed | cancelled
```

Every transition must be caused by a backend event. The frontend must not use a
timer to invent “thinking” or infer validation success from tool names.

## Existing Evidence Sources

- `run/started`, `run/completed`, `run/cancelled`, and correlated `error` define
  lifecycle boundaries.
- `runtime_status` messages expose backend `run_started`, `turn_start`, and
  `perf_phase` events with labels and measured durations.
- `tool_prepare`, `tool_use`, and `tool_result` expose stable tool call IDs and
  terminal status.
- `permission/request` and `permission/resolved` expose the only authoritative
  permission waiting interval.

Validation progress is excluded until the Bridge emits structured validation
events. Counting a command containing `pytest` would violate evidence rules.

## State Model

```text
RunActivity
  id
  requestId
  intent: chat | task
  taskId / missionId
  status: running | completed | failed | cancelled
  phase: preparing | generating | executing | awaiting_permission | summarizing
  phaseLabel
  turn
  model
  toolCalls: map(callId -> prepared | running | success | error | cancelled)
  permissionCount
  perfPhases: bounded newest list(label, durationMs)
  startedAtMs / completedAtMs / durationMs
```

- Only one `activeRunActivity` may exist because Bridge v1 owns one active run.
- The finished activity remains in `messages`; the active pointer becomes null.
- Tool counts are deduplicated by `tool_call_id`; missing IDs use a bounded
  deterministic fallback from the existing tool state logic.
- Perf phase history keeps the newest five entries and never grows unbounded.

## Rendering

- A dedicated `run_activity` message renders one compact card.
- Text always states phase and outcome; color is supplementary.
- Running card shows phase, turn/model when available, tool progress, permission
  count, and newest measured backend phases.
- Completed card shows actual duration and tool terminal counts.
- Failed and cancelled cards remain expanded enough to expose the terminal
  outcome; they are not styled as success.
- Rendering stays within the shared terminal width and participates in the
  existing render cache. Every in-place update invalidates its cache entry.

## Event Mapping

- `run/started`: create the group in `preparing`.
- backend `runtime_status(run_started|turn_start|perf_phase)`: update label,
  turn/model, and bounded measured phases.
- `assistant_stream(start)`: phase `generating` before tools, otherwise
  `summarizing`; this is an explicit response boundary from the backend.
- `tool_prepare|tool_use`: phase `executing`, update one call.
- `tool_result`: terminalize one call and stay `executing`.
- `permission/request`: phase `awaiting_permission`, increment requests once.
- `permission/resolved`: phase `executing`.
- terminal lifecycle event: terminalize the group and clear the active pointer.

## TDD And Verification

1. Reducer tests prove one group per run, stable tool-call dedupe, permission
   transitions, bounded perf history, and terminal cleanup.
2. Component tests prove text-only phase/outcome and width constraints.
3. Render-cache tests prove in-place phase and counter updates are visible.
4. Fake Bridge process test runs permission + tool lifecycle and observes one
   activity group update rather than duplicate groups.
5. Complete Terminal Node gate runs at the module checkpoint; this slice uses
   state/component/process focused tests before its independent commit.

## Explicit Exclusions

- Hidden chain-of-thought or raw model reasoning.
- Validation pass/fail aggregation without structured validation evidence.
- Multi-run concurrency before Bridge v2 supplies stable run IDs.
- Replacing individual tool cards; the group summarizes, while cards preserve
  inspectable evidence.
