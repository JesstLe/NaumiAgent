# Mac Workbench Top Navigation UI Audit

Date: 2026-06-27
Locale: zh-CN
Viewport: 1440 x 900 logical points

## Scope

This audit checked the current SwiftUI Mac workbench preview across the primary top navigation routes:

- Dashboard
- Task Market
- Worktrees
- Reviews
- Timeline
- Settings

The check focused on titlebar separation, top navigation stability, page-level header consistency, clipping, unreadable backgrounds, and obvious text or control overlap.

## Accepted Screenshots

- `screenshots/01-dashboard-zh.png`
- `screenshots/02-task-market-zh.png`
- `screenshots/03-worktrees-zh.png`
- `screenshots/04-reviews-zh.png`
- `screenshots/05-timeline-zh.png`
- `screenshots/06-settings-zh.png`

## Findings Fixed

1. Native titlebar and product navigation were competing for the same top area.
   - Fix: the app now uses the native macOS titlebar for window chrome and a Core-level `WorkbenchShellView` for the product navigation below it.

2. Dashboard drew a second app-level header directly under the global navigation.
   - Fix: removed the duplicate Dashboard header row so the canvas starts cleanly below the shared navigation.

3. Worktrees and Timeline placed refresh actions into SwiftUI `.toolbar`, which can land in the native titlebar.
   - Fix: moved refresh buttons into each page header and skipped real network refresh in preview fixture mode.

4. Worktrees and Timeline rendered transparent content as black in local snapshots.
   - Fix: set explicit `windowBackgroundColor` backgrounds for both pages.

5. Task Market exceeded the 1440-point preview width, clipping the left filter rail and right inspector.
   - Fix: redistributed widths, tightened table columns, and constrained bid-card controls.

6. Settings lacked a clear page header.
   - Fix: added a compact Settings header and subtitle above the grouped form.

## Verification Notes

The macOS `screencapture` command is blocked on this machine with `could not create image from display`, so the accepted screenshots were generated with the package-local `NaumiAgentWorkbenchSnapshot` tool. The tool renders the same Core `WorkbenchShellView` used by the app with preview fixtures, so the screenshots cover the actual SwiftUI shell and pages. Native OS chrome is still verified structurally by app configuration and tests rather than by screenshot.
