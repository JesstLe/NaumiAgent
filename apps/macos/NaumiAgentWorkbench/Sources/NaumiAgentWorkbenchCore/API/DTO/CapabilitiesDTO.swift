import Foundation

/// Response from `GET /workbench/capabilities`.
public struct CapabilitiesDTO: Decodable, Equatable, Sendable {
    public let supportsDaemonManagement: Bool
    public let supportsWorkspaceRegistry: Bool
    public let supportsValidationRunner: Bool
    public let supportsCloudSync: Bool
    public let supportedLocales: [String]
    public let defaultLocale: String
    public let protocolVersion: Int

    public enum CodingKeys: String, CodingKey {
        case supportsDaemonManagement = "supports_daemon_management"
        case supportsWorkspaceRegistry = "supports_workspace_registry"
        case supportsValidationRunner = "supports_validation_runner"
        case supportsCloudSync = "supports_cloud_sync"
        case supportedLocales = "supported_locales"
        case defaultLocale = "default_locale"
        case protocolVersion = "protocol_version"
    }

    public init(
        supportsDaemonManagement: Bool,
        supportsWorkspaceRegistry: Bool,
        supportsValidationRunner: Bool,
        supportsCloudSync: Bool,
        supportedLocales: [String],
        defaultLocale: String = "zh-CN",
        protocolVersion: Int
    ) {
        self.supportsDaemonManagement = supportsDaemonManagement
        self.supportsWorkspaceRegistry = supportsWorkspaceRegistry
        self.supportsValidationRunner = supportsValidationRunner
        self.supportsCloudSync = supportsCloudSync
        self.supportedLocales = supportedLocales
        self.defaultLocale = defaultLocale
        self.protocolVersion = protocolVersion
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        supportsDaemonManagement = try container.decode(
            Bool.self,
            forKey: .supportsDaemonManagement
        )
        supportsWorkspaceRegistry = try container.decode(
            Bool.self,
            forKey: .supportsWorkspaceRegistry
        )
        supportsValidationRunner = try container.decode(
            Bool.self,
            forKey: .supportsValidationRunner
        )
        supportsCloudSync = try container.decode(Bool.self, forKey: .supportsCloudSync)
        supportedLocales = try container.decode([String].self, forKey: .supportedLocales)
        defaultLocale = try container.decodeIfPresent(
            String.self,
            forKey: .defaultLocale
        ) ?? "zh-CN"
        protocolVersion = try container.decode(Int.self, forKey: .protocolVersion)
    }
}
