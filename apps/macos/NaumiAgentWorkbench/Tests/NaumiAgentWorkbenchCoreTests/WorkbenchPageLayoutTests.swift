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

        #expect(layout.scale(for: 1600) > 1)
        #expect(layout.scale(for: 900) < 1)
        #expect(size.width <= 900)
        #expect(size.height < layout.baseHeight)
    }

    @Test func scaledLayoutUsesWindowWidthSoHeightDoesNotChangeColumnScale() {
        let layout = WorkbenchScaledPageLayout.dashboard

        let native = layout.scaledSize(for: CGSize(width: layout.baseWidth, height: layout.baseHeight))
        let sameWidthTall = layout.scaledSize(for: CGSize(width: 1180, height: 900))
        let sameWidthShort = layout.scaledSize(for: CGSize(width: 1180, height: 620))
        let narrow = layout.scaledSize(for: CGSize(width: 900, height: 620))

        #expect(abs(native.width - layout.baseWidth) < 0.001)
        #expect(abs(native.height - layout.baseHeight) < 0.001)
        #expect(abs(sameWidthTall.width - sameWidthShort.width) < 0.001)
        #expect(abs(sameWidthTall.height - sameWidthShort.height) < 0.001)
        #expect(abs(narrow.width - 900) < 0.001)
        #expect(narrow.height < 620)
    }

    @Test func scaledViewportKeepsTheFullWorkbenchFitted() {
        let layout = WorkbenchScaledPageLayout.dashboard

        let compact = layout.viewport(for: CGSize(width: 1180, height: 760))
        let wide = layout.viewport(for: CGSize(width: 2048, height: 1152))
        let wideButShort = layout.viewport(for: CGSize(width: 2048, height: 900))

        #expect(compact.scaledSize.width <= compact.containerSize.width)
        #expect(compact.scaledSize.height <= compact.containerSize.height)
        #expect(abs(wide.scaledSize.width - wide.containerSize.width) < 0.001)
        #expect(compact.showsVerticalScroll == false)
        #expect(wide.showsVerticalScroll == false)
        #expect(wideButShort.scaledSize.width <= wideButShort.containerSize.width)
        #expect(wideButShort.scaledSize.height > wideButShort.containerSize.height)
        #expect(wideButShort.showsVerticalScroll == true)
    }
}
