import SwiftUI
import NaumiAgentWorkbenchCore

@main
struct NaumiAgentWorkbenchApp: App {
    @State private var environment = AppEnvironment()
    private let shellPresentation = WorkbenchShellPresentation()

    var body: some Scene {
        WindowGroup(shellPresentation.nativeWindowTitle) {
            WorkbenchShellView(environment: environment)
                .task {
                    switch WorkbenchPreviewLoader.requestedMode(from: CommandLine.arguments) {
                    case .disabled:
                        await environment.refreshCoordinator.startPeriodicRefresh()
                    case .enabled(let locale):
                        do {
                            try WorkbenchPreviewLoader.applyPreviewState(
                                locale: locale,
                                to: environment.appState
                            )
                            if let previewRoute = WorkbenchPreviewLoader.requestedRoute(
                                from: CommandLine.arguments
                            ) {
                                environment.appState.currentRoute = previewRoute
                            }
                        } catch {
                            environment.appState.connectionState = .disconnected
                        }
                    case .malformed:
                        environment.appState.connectionState = .disconnected
                    }
                }
        }
    }
}
