# Phase 8: TUI Integration

## Objective

Update NaumiAgent's Textual TUI to support the new browser capabilities: task run monitoring, browser state display, security scan results.

## Files to Modify

- `src/naumi_agent/tui/app.py`

## Changes

### 1. Browser Task Panel

Add a new panel or tab in the TUI for monitoring browser task runs:

- Task list showing recent runs (status, summary, timestamps)
- Live task progress (step counter, current action, screenshot preview)
- Walkthrough display for completed tasks
- Reply/abort/resume controls for waiting tasks

### 2. Browser State Widget

Show current browser state:
- Active/inactive indicator
- Current URL and title
- Browser mode (headless/headful/attached)
- Screenshot thumbnail
- Console errors count
- Network errors count

### 3. Security Scan Results

Display security scan results:
- Summary by severity (critical/high/medium/low/info)
- Module-by-module findings
- SARIF report download link
- Baseline comparison results

### 4. New TUI Commands

Map the same slash commands from Phase 7 into the TUI:
- `/browse`, `/autobrowse`, `/browser-stop`, `/browser-state`, `/browser-screenshot`
- `/tasks`, `/task`, `/task-reply`, `/task-abort`, `/task-resume`
- `/scan`, `/scan-full`, `/scan-report`, `/scan-baseline`
- `/btemplate-list`, `/btemplate-run`, `/btemplate-compare`

### 5. Event Display

Show browser events in the TUI event stream:
- Task progress updates (step N/M)
- Browser actions (goto, click, type, etc.)
- CAPTCHA detection alerts
- Security scan progress

## Implementation Notes

- Use Textual's `VerticalScroll` for scrollable task list
- Use `Static` widgets for status indicators
- Use `Rich` formatting for security findings (colored by severity)
- Screenshot preview can use textual-image or a base64 image widget
- Consider adding a separate tab for "Browser" vs "Chat"

## Testing

- Manual testing with real browser tasks
- Test TUI rendering with mocked task data
- Test slash command handling

## Checklist

- [ ] Browser task monitoring panel
- [ ] Browser state widget
- [ ] Security scan results display
- [ ] All new slash commands in TUI
- [ ] Event display for browser actions
- [ ] `ruff check` clean
