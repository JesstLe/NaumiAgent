import Testing
@testable import NaumiAgentWorkbenchCore

struct WorkbenchPageLayoutTests {

    @Test func worktreesDefaultPreviewFitsInspectorAndOperationsColumns() {
        let layout = WorkbenchPageLayout.worktrees

        #expect(layout.railWidth == 286)
        #expect(layout.inspectorWidth == 306)
        #expect(layout.centralAvailableWidth(in: 1440) >= layout.operationsGridWidth)
    }

    @Test func dashboardLayoutScalesDownWithoutExceedingAvailableWidth() {
        let layout = WorkbenchScaledPageLayout.dashboard
        let size = layout.scaledSize(for: 900)

        #expect(layout.scale(for: 1440) == 1)
        #expect(layout.scale(for: 900) < 1)
        #expect(size.width <= 900)
        #expect(size.height < layout.baseHeight)
    }
}
