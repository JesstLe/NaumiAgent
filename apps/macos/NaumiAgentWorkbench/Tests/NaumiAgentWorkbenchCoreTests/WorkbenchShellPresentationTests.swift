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
        #expect(presentation.minimumWindowWidth == 1180)
        #expect(presentation.minimumWindowHeight == 760)
        #expect(presentation.navigationRoutes == AppRoute.topNavigationRoutes)
    }
}
