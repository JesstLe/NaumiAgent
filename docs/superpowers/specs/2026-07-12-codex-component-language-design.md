# Codex-Inspired Component Language

## Goal

Give NaumiAgent Workbench a quiet, dense, native macOS component language inspired by the supplied Codex reference while preserving every route, page layout, data source, action, and Chinese-first copy.

## Confirmed Visual Direction

- Warm-white application canvas with restrained cool-gray grouped surfaces.
- One-pixel neutral separators and maximum 8 pt corner radii.
- No gradients, decorative dot fields, colored card outlines, or drop shadows.
- Blue is reserved for the selected row and primary action; red, orange, and green appear only as small semantic dots, chips, or narrow leading accents.
- Tool actions use compact SF Symbol buttons with Help text rather than oversized text controls.
- The selected branching blue-teal-green NaumiAgent logo appears in the top application bar and is also used as the packaged app icon.

## Scope

Phase 1 changes the shared component primitives, application chrome, Dashboard, and app bundle icon. It does not change `WorkbenchShellPresentation`, `WorkbenchScaledPageLayout`, route selection, API models, refresh behavior, action handlers, or localized strings.

The Dashboard keeps its existing left task rail, central shared canvas, right inspector, and audit trail. The central canvas retains relationship lines, but removes the decorative dotted-paper background and uses thin blue-gray connector strokes.

## Component Contract

`WorkbenchComponentTheme` is the only source for core component metrics and semantic surface colors:

- `cornerRadius = 8`, `compactCornerRadius = 6`, `selectionStripeWidth = 3`.
- `canvas`, `rail`, `group`, `selectedRow`, `divider`, and `connector` are semantic colors, not per-page literal colors.

`WorkbenchSurface`, `WorkbenchListRow`, `WorkbenchStatusChip`, and `WorkbenchIconButton` consume those tokens. `NaumiBrandMark` is a small, code-native rendering of the selected three-node logo for the title bar; the original selected PNG is converted to the `.icns` used by the packaged app.

## Acceptance Criteria

1. The Dashboard screenshot retains the current three-column geometry and data positions.
2. There is no dotted canvas pattern, pastel card field, or colored card border on the Dashboard.
3. A selected task has a soft blue surface and a 3 pt leading blue accent; unselected rows use a neutral grouped surface.
4. Inspector sections and canvas nodes use the shared 1 pt neutral border and 8 pt-or-less radius.
5. The top bar displays the NaumiAgent mark and the packaged app has `AppIcon.icns` registered through `CFBundleIconFile`.
6. Existing localized UI strings, route changes, service controls, selection commands, and real-mode data continue to work.
7. Unit tests prove theme contract values and bundle packaging copies the app icon; the Dashboard preview screenshot is visually inspected at desktop and compact sizes.
