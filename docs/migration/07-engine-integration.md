# Phase 7: Engine Integration + Slash Commands

## Objective

Wire the new browser subsystem into NaumiAgent's engine, add slash commands, and update the CLI completer.

## Files to Modify

- `src/naumi_agent/orchestrator/engine.py` — register new tools, inject BrowserRuntime
- `src/naumi_agent/main.py` — add slash commands for browser tasks, security scans
- `src/naumi_agent/cli_completer.py` — add new commands

## Engine Changes

### 1. BrowserRuntime Lifecycle

Add `BrowserRuntime` as a managed resource in `AgentEngine`:

```python
class AgentEngine:
    def __init__(self, config):
        ...
        self._browser_runtime: BrowserRuntime | None = None

    @property
    def browser_runtime(self) -> BrowserRuntime:
        if self._browser_runtime is None:
            from naumi_agent.tools.browser.runtime import BrowserRuntime
            base_dir = Path(self.config.memory.session_db_path).parent / "browser"
            self._browser_runtime = BrowserRuntime(str(base_dir))
        return self._browser_runtime
```

### 2. Register New Tools

Replace old `create_browser_tools()` call with new tools:

```python
def _create_tools(self):
    ...
    # Replace old browser tools
    browser_session = BrowserSession()
    tools.extend(create_browser_tools(browser_session))

    # With new tools
    from naumi_agent.tools.browser.tools import create_browser_tools_v2
    tools.extend(create_browser_tools_v2(self.browser_runtime))
```

### 3. Task Runner Integration

Add `TaskRunner` as a lazy resource:

```python
@property
def task_runner(self) -> TaskRunner:
    if self._task_runner is None:
        from naumi_agent.tools.browser.orchestrator import TaskRunner
        self._task_runner = TaskRunner(
            base_dir=str(Path(self.config.memory.session_db_path).parent / "browser"),
            runtime=self.browser_runtime,
        )
    return self._task_runner
```

## New Slash Commands

### Browser Task Commands

| Command | Description | Implementation |
|---------|-------------|----------------|
| `/browse <url>` | Open URL and show SoM elements | Start browser → goto → display elements |
| `/autobrowse <task>` | Autonomous browser task | TaskRunner.create_run → watch → display result |
| `/browser-stop` | Stop browser session | runtime.stop() |
| `/browser-state` | Show browser debug state | runtime.get_debug_state() |
| `/browser-screenshot` | Take screenshot | runtime.screenshot_base64() → save/display |

### Task Run Commands

| Command | Description |
|---------|-------------|
| `/tasks` | List browser task runs |
| `/task <id>` | Show task run details |
| `/task-reply <id> <instruction>` | Reply to waiting task |
| `/task-abort <id>` | Abort a running task |
| `/task-resume <id>` | Resume from manual control |

### Security Scan Commands

| Command | Description |
|---------|-------------|
| `/scan <url>` | Quick security scan |
| `/scan-full <url>` | Full 25-module security scan |
| `/scan-report [format]` | Export latest scan report |
| `/scan-baseline <url>` | Save scan as baseline |

### Template Commands

| Command | Description |
|---------|-------------|
| `/btemplate-list` | List browser task templates |
| `/btemplate-run <id>` | Create run from template |
| `/btemplate-compare <id>` | Compare template runs |

## CLI Completer Updates

Add all new commands to `COMMANDS` list in `cli_completer.py`.

## Event System Integration

Add browser events to CLI/TUI event streams:
- `browser_task_started` — new task started
- `browser_task_progress` — step progress
- `browser_task_waiting` — needs human input
- `browser_task_completed` — task finished
- `browser_scan_started` — security scan started
- `browser_scan_completed` — scan finished with summary

## Testing

- Integration test: full engine → browser tools → real browser
- Test slash commands with mocked runtime
- Test event streaming

## Checklist

- [ ] BrowserRuntime lifecycle in engine
- [ ] All new tools registered
- [ ] TaskRunner integrated
- [ ] Slash commands implemented
- [ ] CLI completer updated
- [ ] Browser events in event stream
- [ ] All tests passing
- [ ] `ruff check` clean
