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
}
