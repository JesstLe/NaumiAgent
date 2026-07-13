# Terminal Chat-to-Task Submission Plan

## Goal

Close the first real conversation-to-task path in Terminal UI:

```text
current conversation -> explicit task submit -> Workbench Issue + backing Task
-> same timeline execution -> task-correlated result -> /tasks visibility
```

This must reuse `WorkbenchService`, `TaskStore`, the active Agent engine, and the existing JSONL Bridge. It must not create a frontend-only task, encode task creation only in a prompt, or fork a second execution engine.

## Product Decisions

### Two intents, one conversation

- `chat` sends the existing `submit` event and creates no Workbench object.
- `task` sends a new `task_submit` event and creates a Workbench Issue before Agent execution.
- Both intents use the current backend session, timeline, runtime mode, permissions, model, and completion lifecycle.
- A completed task remains linked to later chat turns through visible task metadata; follow-up chat does not silently create another Issue.

### Entry points

- `/task <non-ID text>` performs one explicit task submit.
- `/task <numeric-id>` and `/task #<numeric-id>` preserve the existing task-detail command.
- `/task create <text>` is an explicit equivalent and avoids ambiguity.
- `Ctrl+T` toggles the Composer's persistent `chat | task` intent for the current session.
- `/chat` returns the Composer to chat intent.
- In task intent, ordinary Enter/Ctrl+Enter submissions use `task_submit`; after accepted submission the intent returns to `chat` to prevent accidental duplicate task creation.

### Mission resolution

Task creation requires a Workbench Mission. The Bridge resolves it deterministically:

1. Use an explicit `mission_id` when provided and verify session ownership.
2. If omitted and exactly one open Mission exists, use it.
3. If no Mission exists, create one named from the first 36 characters of the task, with the full task text as goal.
4. If multiple open Missions exist, reject with `mission_required` and return concise candidates. Never guess.

The auto-created Mission is a real Workbench Mission with an audit event, not a UI placeholder.

### Issue defaults

- `title`: explicit title or first non-empty line, capped at 80 characters.
- `description`: exact submitted text.
- `acceptance_criteria`: explicit list or empty.
- `parallel_mode`: `exclusive` by default.
- `risk_level`: `medium` by default.
- `blocked_by`: explicit list or empty.

The Bridge validates enums and text bounds before mutation.

## Protocol

### Client event

```json
{
  "id": "task-submit-1",
  "type": "task_submit",
  "version": 1,
  "payload": {
    "text": "实现登录流程并补测试",
    "mission_id": "optional",
    "title": "optional",
    "acceptance_criteria": [],
    "blocked_by": [],
    "parallel_mode": "exclusive",
    "risk_level": "medium"
  }
}
```

### Server events

Before execution:

```text
user/message(request_id, content, intent=task)
task/created(request_id, mission, issue, task, workbench_snapshot)
run/started(request_id, task, task_id, mission_id, intent=task)
```

During execution, existing `ui/message`, permission, tool, todo, validation, and status events remain unchanged.

Terminal event:

```text
run/completed(request_id, status, task_id, mission_id, intent=task)
```

Errors before Issue creation produce only a correlated error. Errors after creation include `task_id` and preserve the Issue for retry/audit rather than deleting evidence.

## Backend State Transitions

1. Reject empty text or a second active run before any mutation.
2. Call `engine.get_or_create_session()` and scope `TaskStore` to that session.
3. Resolve/create Mission.
4. Call `engine.workbench_service.create_issue()`.
5. Mark the backing task `in_progress` before starting the Agent.
6. Build trusted `turn_context` containing task ID, Mission ID, title, risk, parallel mode, and acceptance criteria.
7. Call the existing `engine.run_streaming(text, callback, turn_context=...)`.
8. Mark task `completed` only for successful Agent terminal status; otherwise mark `blocked` with a public failure reason.
9. Emit a fresh Workbench snapshot and task-correlated terminal record.

Cancellation marks the backing task cancelled/blocked according to the existing TaskStatus vocabulary; it never leaves an `in_progress` task after the run is gone.

## Frontend State

Add:

```text
composerIntent: chat | task
activeTaskSubmission:
  requestId
  taskId
  missionId
  state: creating | running | completed | blocked
```

- Prompt prefix renders `chat >` or `task >`; role is not conveyed by color alone.
- `Ctrl+T` and `/chat` produce a concise local system notice.
- Local optimistic user message carries `intent=task` and reuses queued/accepted/failed/uncertain delivery states.
- `task/created` updates the same local message/card and Workbench bucket; it does not append duplicate user content.
- After task acceptance, Composer intent resets to chat and is persisted in the session UI snapshot only while a task draft remains unsent.
- `/tasks` immediately shows the new Issue through the existing real panel refresh.

## Failure and Recovery

- Mission ambiguity: keep task draft, show candidates, create nothing.
- Invalid Mission: keep draft, create nothing.
- Issue creation failure: mark optimistic message failed and allow explicit retry.
- Bridge disconnect before `task/created`: restore as uncertain; retry warns about possible duplication until Bridge v2 idempotency exists.
- Bridge disconnect after `task/created`: task ID is authoritative; retry must resume/link that task rather than create a second Issue. This requires task ID in the outbox snapshot.
- Agent failure after creation: Issue remains visible and backing task becomes blocked; user can follow up or retry from `/tasks`.
- Process shutdown/cancel: release run state and eliminate stale `in_progress` status.

## TDD Sequence

### 1. Protocol contract

- Add `TASK_SUBMIT` and `TASK_CREATED` enums.
- Validate payload text, list fields, enums, and request correlation.
- Prove malformed input causes zero Workbench mutations.

### 2. Bridge service integration

- Fake WorkbenchService tests for explicit, single, absent, and ambiguous Mission cases.
- Real SQLite test creates Mission, Task, Issue, and audit event.
- Agent success/failure/cancel tests prove terminal TaskStatus cleanup.

### 3. Frontend reducer and delivery

- Optimistic task message uses the existing request lifecycle.
- `task/created` accepts the message and stores task metadata without duplication.
- Correlated errors stay retryable.
- Workbench snapshot updates state immediately.

### 4. Composer intent and commands

- `Ctrl+T` toggles chat/task.
- `/task <text>` creates; `/task <id>` opens detail.
- `/chat` switches locally.
- Task intent resets only after accepted submission.
- Slash completion advertises both modes without intercepting exact commands twice.

### 5. Process and real-data acceptance

- Fake Bridge process proves task creating/running/completed UI states.
- Real `.venv` Bridge plus temporary real SQLite creates one Mission, one Task, one Issue, one audit event.
- `/tasks` reads the same task and displays its ID/status.
- Restart restores the conversation and does not create a duplicate Issue.

## Verification

- `ruff check` on changed Python modules.
- Focused protocol, Bridge, WorkbenchService, Terminal reducer/component/process tests.
- Full Terminal Node gate because input dispatch and outbox metadata change.
- Focused Python Bridge and Workbench tests; full repository pytest only after the complete chat/task module closes.
- Real process evidence with real SQLite and no model network dependency by substituting only the engine execution result, not Workbench storage.

## Explicit Exclusions

- Automatic Agent-proposed conversion requiring human approval UI.
- Linking a follow-up message to an existing Issue by natural-language inference.
- Multi-client idempotency and reconnect delta replay (Bridge v2).
- Inspector task-edit forms, bidding, leasing, validation, and approval flows.
