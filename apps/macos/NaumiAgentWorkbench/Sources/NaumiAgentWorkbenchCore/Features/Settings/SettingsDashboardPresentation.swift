import Foundation

/// Dashboard-level summary for runtime settings and governance.
@MainActor
public struct SettingsDashboardPresentation: Equatable {
    public let runtimeEndpoint: String
    public let activeMissionTitle: String
    public let enabledCapabilityCount: Int
    public let governancePolicyCount: Int
    public let connectionState: AppState.ConnectionState
    public let supportedLocales: [String]

    public init(appState: AppState) {
        if let daemonStatus = appState.daemonStatus {
            self.runtimeEndpoint = "\(daemonStatus.host):\(daemonStatus.port)"
        } else {
            self.runtimeEndpoint = "-"
        }
        self.activeMissionTitle = appState.missions.first?.title ?? "-"
        self.enabledCapabilityCount = SettingsDashboardPresentation.enabledCapabilityCount(
            capabilities: appState.capabilities
        )
        self.governancePolicyCount = 3
        self.connectionState = appState.connectionState
        self.supportedLocales = appState.capabilities?.supportedLocales ?? []
    }

    private static func enabledCapabilityCount(capabilities: CapabilitiesDTO?) -> Int {
        guard let capabilities else { return 0 }
        return [
            capabilities.supportsDaemonManagement,
            capabilities.supportsWorkspaceRegistry,
            capabilities.supportsValidationRunner,
            capabilities.supportsCloudSync
        ]
        .filter { $0 }
        .count
    }
}
