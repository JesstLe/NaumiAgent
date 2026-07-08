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
    public let connectionSettingsStore: WorkbenchConnectionSettingsStore
    public var connectionSettings: WorkbenchConnectionSettings

    public init(
        appState: AppState = AppState(),
        apiClient: WorkbenchAPIClient? = nil,
        eventClient: WorkbenchEventClient? = nil,
        connectionSettingsStore: WorkbenchConnectionSettingsStore = .default
    ) {
        self.appState = appState
        self.connectionSettingsStore = connectionSettingsStore
        let settings = connectionSettingsStore.load()
        self.connectionSettings = settings

        let resolvedBaseURL = settings.baseURL
            ?? URL(string: WorkbenchConnectionSettings.defaultBaseURLString)!
        let resolvedToken = settings.resolvedBearerToken

        let resolvedClient = apiClient ?? WorkbenchAPIClient(
            baseURL: resolvedBaseURL,
            bearerToken: resolvedToken
        )
        self.apiClient = resolvedClient
        self.eventClient = eventClient ?? WorkbenchEventClient(
            baseURL: resolvedBaseURL,
            bearerToken: resolvedToken
        )
        self.daemonController = DaemonController(
            appState: appState,
            apiProvider: resolvedClient,
            eventProvider: self.eventClient
        )
        self.refreshCoordinator = WorkbenchRefreshCoordinator(
            daemonController: daemonController
        )
    }

    /// Persists new connection settings and re-points both API and event
    /// clients at the updated endpoint without restarting the app. A fresh
    /// connection refresh follows so the user sees the result immediately.
    public func updateConnection(_ settings: WorkbenchConnectionSettings) async {
        do {
            try connectionSettingsStore.save(settings)
        } catch {
            appState.lastError = .networkFailure(error.localizedDescription)
        }
        connectionSettings = settings
        guard let baseURL = settings.baseURL ?? URL(string: WorkbenchConnectionSettings.defaultBaseURLString) else {
            return
        }
        await apiClient.updateConnection(baseURL: baseURL, bearerToken: settings.resolvedBearerToken)
        await eventClient.updateConnection(baseURL: baseURL, bearerToken: settings.resolvedBearerToken)
        await daemonController.stopEventStream()
        await daemonController.refreshConnection()
    }
}
