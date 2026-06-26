import Foundation
import Observation

/// Dependency container for the SwiftUI app.
@Observable
@MainActor
public final class AppEnvironment: Sendable {
    public let apiClient: WorkbenchAPIClient
    public let appState: AppState
    public let daemonController: DaemonController

    public init(
        appState: AppState = AppState(),
        apiClient: WorkbenchAPIClient = WorkbenchAPIClient()
    ) {
        self.appState = appState
        self.apiClient = apiClient
        self.daemonController = DaemonController(
            appState: appState,
            apiProvider: apiClient
        )
    }
}
