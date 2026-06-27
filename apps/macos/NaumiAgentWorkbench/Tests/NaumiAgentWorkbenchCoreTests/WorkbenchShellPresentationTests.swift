import Testing
@testable import NaumiAgentWorkbenchCore

struct WorkbenchShellPresentationTests {

    @Test func shellUsesNativeMacWindowChrome() {
        let presentation = WorkbenchShellPresentation()

        #expect(presentation.showsSyntheticWindowControls == false)
        #expect(presentation.leadingContentInset >= 72)
        #expect(presentation.navigationRoutes == AppRoute.topNavigationRoutes)
    }
}
