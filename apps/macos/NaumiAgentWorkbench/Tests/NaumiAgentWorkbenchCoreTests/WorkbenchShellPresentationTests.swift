import CoreGraphics
import Testing
@testable import NaumiAgentWorkbenchCore

struct WorkbenchShellPresentationTests {

    @Test func shellUsesNativeMacWindowChrome() {
        let presentation = WorkbenchShellPresentation()

        #expect(presentation.showsSyntheticWindowControls == false)
        #expect(presentation.placesNavigationBelowTitleBar == true)
        #expect(presentation.leadingContentInset == 14)
        #expect(presentation.topNavigationHeight == 42)
        #expect(presentation.globalStatusHeight == 0)
        #expect(presentation.designCanvasWidth == 1440)
        #expect(presentation.minimumWindowWidth == 1180)
        #expect(presentation.minimumWindowHeight == 760)
        #expect(presentation.navigationRoutes == AppRoute.topNavigationRoutes)
        #expect(presentation.nativeWindowTitle.isEmpty)
    }

    @Test func shellNavigationScalesWithFixedDesignPages() {
        let presentation = WorkbenchShellPresentation()

        #expect(presentation.navigationScale(for: 1440) == 1)
        #expect(abs(presentation.navigationScale(for: 1180) - (1180.0 / 1440.0)) < 0.001)
        #expect(abs(presentation.scaledTopNavigationHeight(for: 1180) - 34.416) < 0.01)
        #expect(abs(presentation.scaledTopNavigationHeight(for: 2048) - 59.733) < 0.01)
    }

    @Test func shellNavigationUsesSameScaleAsPageWhenWindowHeightLimitsPreview() {
        let presentation = WorkbenchShellPresentation()
        let pageLayout = WorkbenchScaledPageLayout.dashboard
        let windowWidth = 2048.0
        let windowHeight = 900.0
        let scale = presentation.navigationScale(
            for: CGSize(width: windowWidth, height: windowHeight),
            pageLayout: pageLayout
        )
        let routeHeight = windowHeight - presentation.scaledTopNavigationHeight(
            for: CGSize(width: windowWidth, height: windowHeight),
            pageLayout: pageLayout
        )
        let pageScale = pageLayout.scale(
            for: CGSize(width: windowWidth, height: routeHeight)
        )

        #expect(abs(scale - pageScale) < 0.001)
        #expect(scale < presentation.navigationScale(for: windowWidth))
    }

    @Test func shellViewportScalesNavigationAndPageTogether() {
        let presentation = WorkbenchShellPresentation()
        let pageLayout = WorkbenchScaledPageLayout.dashboard

        let compact = presentation.shellViewport(
            for: CGSize(width: 1180, height: 760),
            pageLayout: pageLayout
        )
        let shortWide = presentation.shellViewport(
            for: CGSize(width: 2048, height: 900),
            pageLayout: pageLayout
        )

        #expect(compact.scaledSize.width <= compact.containerSize.width)
        #expect(compact.scaledSize.height <= compact.containerSize.height)
        #expect(abs(compact.navigationHeight - presentation.topNavigationHeight * compact.scale) < 0.001)
        #expect(abs(compact.pageHeight - pageLayout.baseHeight * compact.scale) < 0.001)
        #expect(abs(compact.scaledSize.height - (compact.navigationHeight + compact.pageHeight)) < 0.001)
        #expect(shortWide.scaledSize.width < shortWide.containerSize.width)
        #expect(shortWide.scaledSize.height <= shortWide.containerSize.height)
        #expect(shortWide.scale < presentation.navigationScale(for: 2048))
    }

    @Test func shellViewportFillsReferencePreviewHeightWithoutBottomVoid() {
        let presentation = WorkbenchShellPresentation()
        let pageLayout = WorkbenchScaledPageLayout.dashboard

        let viewport = presentation.shellViewport(
            for: CGSize(width: 1440, height: 900),
            pageLayout: pageLayout
        )

        #expect(abs(viewport.scale - 1) < 0.001)
        #expect(abs(viewport.scaledSize.width - 1440) < 0.001)
        #expect(abs(viewport.scaledSize.height - 900) < 0.001)
    }
}
