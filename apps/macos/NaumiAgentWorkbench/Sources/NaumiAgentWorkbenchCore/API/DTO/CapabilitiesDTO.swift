import Foundation

/// Response from `GET /workbench/capabilities`.
public struct CapabilitiesDTO: Decodable, Equatable, Sendable {
    public let supportsDaemonManagement: Bool
    public let supportsWorkspaceRegistry: Bool
    public let supportsValidationRunner: Bool
    public let supportsCloudSync: Bool
    public let supportedLocales: [String]
    public let protocolVersion: Int

    public enum CodingKeys: String, CodingKey {
        case supportsDaemonManagement = "supports_daemon_management"
        case supportsWorkspaceRegistry = "supports_workspace_registry"
        case supportsValidationRunner = "supports_validation_runner"
        case supportsCloudSync = "supports_cloud_sync"
        case supportedLocales = "supported_locales"
        case protocolVersion = "protocol_version"
    }
}
