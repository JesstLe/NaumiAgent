# Codex-Inspired Component Language Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the approved Codex-inspired component language to the Workbench shell and Dashboard without changing the existing page layout or data behavior.

**Architecture:** Introduce a small SwiftUI component layer that owns neutral surfaces, borders, compact selection, status presentation, and the Naumi brand mark. Reuse it in the shell and Dashboard rather than creating a new layout. Package the selected image as a macOS app icon independently of the title-bar mark.

**Tech Stack:** Swift 6, SwiftUI, Swift Testing, macOS 14, `sips`, `iconutil`, existing snapshot executable and packaging shell scripts.

## Global Constraints

- Preserve `WorkbenchShellPresentation` and `WorkbenchScaledPageLayout` dimensions.
- Keep user-visible copy Chinese-first and preserve existing `AppStrings` contracts.
- Do not change API, event-stream, daemon-launch, selection-command, or preview-versus-real-data behavior.
- Run targeted tests after each task; run package and screenshot verification after the final task.

---

### Task 1: Establish the Component Theme and Brand Mark

**Files:**
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Components/WorkbenchComponentTheme.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Components/NaumiBrandMark.swift`
- Create: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/WorkbenchComponentThemeTests.swift`

**Interfaces:**
- Produces: `WorkbenchComponentTheme.cornerRadius`, `compactCornerRadius`, `selectionStripeWidth`, semantic surface colors, reusable surface/list/chip/icon-button modifiers, and `NaumiBrandMark`.
- Consumes: SwiftUI only.

- [ ] Write the failing theme-contract test:

```swift
import Testing
@testable import NaumiAgentWorkbenchCore

@Suite struct WorkbenchComponentThemeTests {
    @Test func codexComponentMetricsStayCompact() {
        #expect(WorkbenchComponentTheme.cornerRadius == 8)
        #expect(WorkbenchComponentTheme.compactCornerRadius == 6)
        #expect(WorkbenchComponentTheme.selectionStripeWidth == 3)
    }
}
```

- [ ] Run `./scripts/test.sh --filter WorkbenchComponentThemeTests` and confirm it fails because the theme is absent.
- [ ] Implement only the public values used above, then add the semantic `Color` tokens and SwiftUI primitives.
- [ ] Re-run `./scripts/test.sh --filter WorkbenchComponentThemeTests` and confirm the suite passes.
- [ ] Commit with `git commit -m "feat: add workbench component theme"`.

### Task 2: Apply the Theme to the Shell and Dashboard

**Files:**
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/App/WorkbenchShellView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Sources/NaumiAgentWorkbenchCore/Features/Dashboard/DashboardView.swift`
- Modify: `apps/macos/NaumiAgentWorkbench/Tests/NaumiAgentWorkbenchCoreTests/WorkbenchShellPresentationTests.swift`

**Interfaces:**
- Consumes: `WorkbenchComponentTheme`, `NaumiBrandMark`, existing `AppState`, `DashboardSnapshotPresentation`, and selection methods.
- Produces: unchanged `WorkbenchShellView` and `DashboardView` public APIs with updated visual surfaces.

- [ ] Add a failing `WorkbenchChromePresentation` test that asserts `brandTitle == "NaumiAgent Workbench"` and `showsBrandMark == true`.
- [ ] Run `./scripts/test.sh --filter WorkbenchShellPresentationTests` and confirm it fails because the branding contract is absent.
- [ ] Add the brand mark and title to `TopNavigationBar`. Keep the segmented route picker, service controls, dimensions, and Help text.
- [ ] Replace the Dashboard dotted field with a flat surface, use a hairline connector, apply neutral grouped surfaces to task rows, canvas nodes, inspector sections, and audit rows, and show selection with a narrow leading stripe. Preserve all existing frame widths, selection handlers, actions, and content order.
- [ ] Run `./scripts/test.sh --filter 'WorkbenchComponentThemeTests|WorkbenchShellPresentationTests|DashboardSnapshotPresentationTests|DashboardIssueSelectionCommandTests|DashboardAgentSelectionCommandTests|DashboardWorktreeSelectionCommandTests'` and confirm every selected suite passes.
- [ ] Commit with `git commit -m "feat: apply codex-inspired dashboard components"`.

### Task 3: Package the Selected App Icon and Visually Verify

**Files:**
- Create: `apps/macos/NaumiAgentWorkbench/Resources/AppIcon.icns`
- Modify: `apps/macos/NaumiAgentWorkbench/scripts/package-dev-app.sh`
- Modify: `apps/macos/NaumiAgentWorkbench/scripts/run-preview.sh`
- Verify: `docs/mac-app/ui-audit/screenshots/large/01-dashboard-zh.png`
- Verify: `docs/mac-app/ui-audit/screenshots/compact/01-dashboard-zh.png`

**Interfaces:**
- Consumes: `docs/assets/mac-agent-workbench/naumiagent-workbench-logo-selected.png`.
- Produces: `AppIcon.icns` in each preview and development app bundle, with `CFBundleIconFile` set to `AppIcon`.

- [ ] Run `test -f dist/NaumiAgentWorkbench.app/Contents/Resources/AppIcon.icns` and confirm it fails before packaging changes.
- [ ] Create standard iconset PNG sizes with `sips`, build `Resources/AppIcon.icns` with `iconutil -c icns`, copy the icon in both app-bundle scripts, and set `CFBundleIconFile` to `AppIcon` in both generated Info.plist files.
- [ ] Run `./scripts/package-dev-app.sh`, then run `plutil -extract CFBundleIconFile raw dist/NaumiAgentWorkbench.app/Contents/Info.plist` and `test -f dist/NaumiAgentWorkbench.app/Contents/Resources/AppIcon.icns`; expect `AppIcon` and a present icon file.
- [ ] Generate desktop and compact Dashboard screenshots using `swift run -c release NaumiAgentWorkbenchSnapshot --locale zh --route dashboard --out docs/mac-app/ui-audit/screenshots/large --width 2048 --height 1152` and the same command with `screenshots/compact --width 1365 --height 900`.
- [ ] Inspect both renders for preserved three-column geometry, no clipped title/logo, no dotted canvas, no colored card borders, and no overlap.
- [ ] Run `codesign --verify --deep --strict --verbose=2 dist/NaumiAgentWorkbench.app` and `unzip -t dist/NaumiAgentWorkbench-dev.zip`.
- [ ] Commit with `git commit -m "feat: add codex-inspired workbench chrome"` and push `codex/mac-workbench-mvp`.
