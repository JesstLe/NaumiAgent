import CoreGraphics
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

        #expect(layout.scale(for: 1440) > 1)
        #expect(layout.scale(for: 900) < 1)
        #expect(size.width <= 900)
        #expect(size.height < layout.baseHeight)
    }

    @Test func scaledLayoutKeepsThreeColumnsStableWhenHeightChanges() {
        let layout = WorkbenchScaledPageLayout.dashboard

        let normalHeight = layout.scaledSize(for: CGSize(width: 1360, height: 900))
        let shortHeight = layout.scaledSize(for: CGSize(width: 1360, height: 620))
        let narrow = layout.scaledSize(for: CGSize(width: 900, height: 620))

        #expect(abs(normalHeight.width - layout.baseWidth) < 0.001)
        #expect(abs(shortHeight.width - layout.baseWidth) < 0.001)
        #expect(abs(shortHeight.height - normalHeight.height) < 0.001)
        #expect(narrow.width <= 900)
        #expect(abs(narrow.height - layout.baseHeight * layout.scale(for: 900)) < 0.001)
    }
}
