import Foundation
import Observation

/// Dependency container for the SwiftUI app.
@Observable
@MainActor
public final class AppEnvironment: Sendable {
    public let apiClient: WorkbenchAPIClient
    public let eventClient: WorkbenchEventClient
    public let appState: AppState
    public let daemonController: DaemonController
    public let refreshCoordinator: WorkbenchRefreshCoordinator

    public init(
        appState: AppState = AppState(),
        apiClient: WorkbenchAPIClient = WorkbenchAPIClient(),
        eventClient: WorkbenchEventClient? = nil
    ) {
        self.appState = appState
        self.apiClient = apiClient
        self.eventClient = eventClient ?? WorkbenchEventClient(baseURL: apiClient.baseURL)
        self.daemonController = DaemonController(
            appState: appState,
            apiProvider: apiClient,
            eventProvider: self.eventClient
        )
        self.refreshCoordinator = WorkbenchRefreshCoordinator(
            daemonController: daemonController
        )
    }
}
