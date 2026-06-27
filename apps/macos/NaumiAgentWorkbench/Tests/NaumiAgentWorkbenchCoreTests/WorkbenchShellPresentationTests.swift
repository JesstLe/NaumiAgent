import Testing
@testable import NaumiAgentWorkbenchCore

struct WorkbenchShellPresentationTests {

    @Test func shellUsesNativeMacWindowChrome() {
        let presentation = WorkbenchShellPresentation()

        #expect(presentation.showsSyntheticWindowControls == false)
        #expect(presentation.placesNavigationBelowTitleBar == true)
        #expect(presentation.leadingContentInset == 14)
        #expect(presentation.navigationRoutes == AppRoute.topNavigationRoutes)
    }
}
