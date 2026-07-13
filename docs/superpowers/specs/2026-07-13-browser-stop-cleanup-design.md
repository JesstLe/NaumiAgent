# Browser Stop Cleanup Design

## Goal

`browser_stop` and application shutdown must always reach a terminal state. Saving cookies,
trace files, or video is best effort: failures are visible as warnings but cannot leave the
conversation locked or prevent `/q` from exiting.

## Existing Failure

The attached-browser path currently terminates an auto-launched Chrome process before it
stops the CDP screencast, saves `storage_state`, and stops tracing. On Windows this can leave
a Playwright CDP request waiting indefinitely. Cancelling the run then enters shutdown,
which retries cleanup against a closed context and lets `Tracing.stop` abort shutdown.

The terminal UI also clears its local `running` flag for every generic error. A
`run_in_progress` rejection therefore renders the footer as idle while the bridge still has
an active run.

## Cleanup Lifecycle

For an active browser session, cleanup proceeds in this order:

1. Detach network and download observers.
2. Stop attached screencast while CDP is still available.
3. Save browser storage state.
4. Stop tracing.
5. Hide the active-browser border.
6. Close the Playwright browser connection.
7. Terminate Chrome only when NaumiAgent launched it.
8. Stop the Playwright driver.
9. Flush logs, capture the artifact summary, and reset runtime state.

Every external async step has a bounded timeout. Timeout, `TargetClosedError`, and artifact
failures become structured cleanup warnings. Mandatory state reset and process teardown run
from `finally` blocks.

## Result Contract

`BrowserRuntime.stop()` returns:

- `alreadyStopped`: whether no active browser resources existed at entry.
- `artifacts`: the last available artifact summary.
- `warnings`: ordered warning objects with `step`, `code`, and a safe message.

Repeated calls are valid. Stale `browser`, `context`, page, tracing, screencast, driver, or
launcher state is cleared even if the underlying target has already closed.

## Engine And Bridge Lifecycle

Engine shutdown treats each resource family independently. Browser cleanup failure cannot
skip MCP, session-store, or remaining shutdown work.

For normal submitted runs, every `run/started` produces one `run/completed`, including model
or tool exceptions. The completion payload carries `status=failed` and the user-safe error.
Cancellation caused by application shutdown is followed by the shutdown event instead.

The terminal UI changes `running` only from lifecycle events (`run/started`,
`run/completed`, replay, and shutdown). A generic error is informational and cannot override
the authoritative run lifecycle.

## Verification

- Unit tests cover normal cleanup, already-closed targets, a CDP call that never responds,
  artifact failure, repeated stop, and engine shutdown continuation.
- Bridge tests prove failed runs emit `run/completed` and `run_in_progress` preserves the
  active state.
- Node tests prove generic errors do not render an active run as idle.
- A real Windows TUI browser search completes, releases the input lock, and exits through
  `/q` without leftover NaumiAgent/Playwright processes.

