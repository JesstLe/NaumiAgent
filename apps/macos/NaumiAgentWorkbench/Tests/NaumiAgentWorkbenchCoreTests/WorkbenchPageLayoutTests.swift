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

        #expect(layout.scale(for: 1440) == 1)
        #expect(layout.scale(for: 900) < 1)
        #expect(size.width <= 900)
        #expect(size.height < layout.baseHeight)
    }

    @Test func scaledLayoutUsesBothWindowDimensionsAndScalesUpWhenSpaceAllows() {
        let layout = WorkbenchScaledPageLayout.dashboard

        let large = layout.scaledSize(for: CGSize(width: 2048, height: 1000))
        let heightLimited = layout.scaledSize(for: CGSize(width: 2048, height: 640))
        let narrow = layout.scaledSize(for: CGSize(width: 900, height: 620))

        #expect(large.width > layout.baseWidth)
        #expect(large.width <= 2048)
        #expect(large.height <= 1000)
        #expect(heightLimited.height <= 640)
        #expect(heightLimited.width < large.width)
        #expect(narrow.width <= 900)
        #expect(narrow.height <= 620)
    }
}
