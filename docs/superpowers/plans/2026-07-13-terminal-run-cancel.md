# Terminal Run Cancellation Plan

## Goal

Make cancellation a run lifecycle action instead of an application exit:

```text
running + first Ctrl+C -> run_cancel -> accepted -> cancelling -> run/cancelled
idle + Ctrl+C -> shutdown UI
cancelling + second Ctrl+C -> force exit UI
```

The Bridge remains the only owner of the active `asyncio.Task`. The Terminal UI
must not infer cancellation from elapsed time or mark a disconnected run failed.

## Protocol

- Client event: `run_cancel` with an optional public reason.
- Accepted request: `ack` with `event=run_cancel`, `status=accepted`, and the
  target submission request ID.
- Terminal event: `run/cancelled` with the target request ID, intent, and any
  Workbench task identity.
- No active run: correlated `no_active_run` error without mutating state.

## Backend

1. Track bounded metadata for the current run: submission request ID, intent,
   optional task ID, and optional Mission ID.
2. Reject cancellation when `_run_task` is missing or already terminal.
3. Emit acceptance before calling `Task.cancel()` so the UI receives immediate
   feedback.
4. Await the cancelled task. For Workbench submissions, reuse the existing
   cancellation branch that marks the backing Task `blocked` and emits a fresh
   snapshot.
5. Emit `run/cancelled`, clear active run metadata, and keep the Bridge alive.
6. Shutdown still cancels an active run but does not emit an interactive
   cancellation acknowledgement.

## Frontend

- Add `cancelPending` and `cancelRequestId` presentation state.
- While a run is active, first `Ctrl+C` sends one `run_cancel`, displays
  `正在停止当前运行...`, and leaves the composer/session intact.
- Repeated `Ctrl+C` while cancellation is pending performs the existing force
  exit path.
- `run/cancelled` clears running, permission, live tool preparation, and pending
  cancellation state; linked task messages become `blocked`.
- `run/completed`, correlated cancellation errors, session replay, and shutdown
  also clear stale cancellation state.
- The footer says `运行: 正在停止` while cancellation is pending.

## TDD And Verification

1. Protocol contract rejects malformed `run_cancel` payloads.
2. Bridge tests cover no active run, normal run cancellation, Workbench task
   cancellation, idempotent repeated requests, and continued use after cancel.
3. Reducer tests cover request state, terminal cleanup, and linked task status.
4. Process test starts a long fake run, presses `Ctrl+C`, observes cancellation,
   runs `/doctor` afterward, then exits while idle.
5. Run the complete Terminal Node gate and focused Bridge/Ruff gate at module
   checkpoint; commit and push this feature independently.

## Explicit Exclusions

- Hard-killing arbitrary subprocesses outside the active Agent run.
- Resuming a cancelled run from an intermediate model token.
- Bridge v2 reconnect arbitration for cancellation sent during transport loss.
