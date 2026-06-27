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

    @Test func scaledLayoutFitsBothWidthAndHeightSoColumnsRemainVisible() {
        let layout = WorkbenchScaledPageLayout.dashboard

        let native = layout.scaledSize(for: CGSize(width: 1360, height: 720))
        let wideButShort = layout.scaledSize(for: CGSize(width: 2048, height: 1048))
        let narrow = layout.scaledSize(for: CGSize(width: 900, height: 620))

        #expect(abs(native.width - layout.baseWidth) < 0.001)
        #expect(abs(native.height - layout.baseHeight) < 0.001)
        #expect(wideButShort.width <= 2048)
        #expect(wideButShort.height <= 1048)
        #expect(wideButShort.width < 2048)
        #expect(narrow.width <= 900)
        #expect(narrow.height <= 620)
    }
}
