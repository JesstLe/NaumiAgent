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
}
