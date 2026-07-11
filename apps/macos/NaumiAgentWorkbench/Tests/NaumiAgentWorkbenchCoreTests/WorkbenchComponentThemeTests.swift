import Testing
@testable import NaumiAgentWorkbenchCore

@Suite struct WorkbenchComponentThemeTests {
    @Test func codexComponentMetricsStayCompact() {
        #expect(WorkbenchComponentTheme.cornerRadius == 8)
        #expect(WorkbenchComponentTheme.compactCornerRadius == 6)
        #expect(WorkbenchComponentTheme.selectionStripeWidth == 3)
    }

    @Test func componentSurfaceStylesKeepSemanticRolesDistinct() {
        #expect(WorkbenchSurfaceStyle.canvas != .group)
        #expect(WorkbenchSurfaceStyle.group != .selectedRow)
        #expect(WorkbenchSurfaceStyle.rail != .canvas)
    }
}
