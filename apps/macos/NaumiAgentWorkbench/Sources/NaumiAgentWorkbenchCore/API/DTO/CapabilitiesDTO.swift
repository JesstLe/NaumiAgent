import Foundation

/// Response from `GET /workbench/capabilities`.
public struct CapabilitiesDTO: Decodable, Equatable, Sendable {
    public let supportsDaemonManagement: Bool
    public let supportsWorkspaceRegistry: Bool
    public let supportsValidationRunner: Bool
    public let supportsEventStream: Bool
    public let supportsCloudSync: Bool
    public let supportedLocales: [String]
    public let defaultLocale: String
    public let protocolVersion: Int
    public let supportedResources: [String]
    public let supportedActions: [String]
    public let routeTemplates: [String: String]
    public let allowedValidationCommands: [[String]]
    public let agentStaleThresholdSeconds: Int
    public let agentOfflineThresholdSeconds: Int

    public enum CodingKeys: String, CodingKey {
        case supportsDaemonManagement = "supports_daemon_management"
        case supportsWorkspaceRegistry = "supports_workspace_registry"
        case supportsValidationRunner = "supports_validation_runner"
        case supportsEventStream = "supports_event_stream"
        case supportsCloudSync = "supports_cloud_sync"
        case supportedLocales = "supported_locales"
        case defaultLocale = "default_locale"
        case protocolVersion = "protocol_version"
        case supportedResources = "supported_resources"
        case supportedActions = "supported_actions"
        case routeTemplates = "route_templates"
        case allowedValidationCommands = "allowed_validation_commands"
        case agentStaleThresholdSeconds = "agent_stale_threshold_seconds"
        case agentOfflineThresholdSeconds = "agent_offline_threshold_seconds"
    }

    public init(
        supportsDaemonManagement: Bool,
        supportsWorkspaceRegistry: Bool,
        supportsValidationRunner: Bool,
        supportsEventStream: Bool = true,
        supportsCloudSync: Bool,
        supportedLocales: [String],
        defaultLocale: String = "zh-CN",
        protocolVersion: Int,
        supportedResources: [String] = [],
        supportedActions: [String] = [],
        routeTemplates: [String: String] = [:],
        allowedValidationCommands: [[String]] = [],
        agentStaleThresholdSeconds: Int = 300,
        agentOfflineThresholdSeconds: Int = 900
    ) {
        self.supportsDaemonManagement = supportsDaemonManagement
        self.supportsWorkspaceRegistry = supportsWorkspaceRegistry
        self.supportsValidationRunner = supportsValidationRunner
        self.supportsEventStream = supportsEventStream
        self.supportsCloudSync = supportsCloudSync
        self.supportedLocales = supportedLocales
        self.defaultLocale = defaultLocale
        self.protocolVersion = protocolVersion
        self.supportedResources = supportedResources
        self.supportedActions = supportedActions
        self.routeTemplates = routeTemplates
        self.allowedValidationCommands = allowedValidationCommands
        self.agentStaleThresholdSeconds = agentStaleThresholdSeconds
        self.agentOfflineThresholdSeconds = agentOfflineThresholdSeconds
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
        supportsEventStream = try container.decodeIfPresent(
            Bool.self,
            forKey: .supportsEventStream
        ) ?? true
        supportsCloudSync = try container.decode(Bool.self, forKey: .supportsCloudSync)
        supportedLocales = try container.decode([String].self, forKey: .supportedLocales)
        defaultLocale = try container.decodeIfPresent(
            String.self,
            forKey: .defaultLocale
        ) ?? "zh-CN"
        protocolVersion = try container.decode(Int.self, forKey: .protocolVersion)
        supportedResources = try container.decodeIfPresent(
            [String].self,
            forKey: .supportedResources
        ) ?? []
        supportedActions = try container.decodeIfPresent(
            [String].self,
            forKey: .supportedActions
        ) ?? []
        routeTemplates = try container.decodeIfPresent(
            [String: String].self,
            forKey: .routeTemplates
        ) ?? [:]
        allowedValidationCommands = try container.decodeIfPresent(
            [[String]].self,
            forKey: .allowedValidationCommands
        ) ?? []
        agentStaleThresholdSeconds = try container.decodeIfPresent(
            Int.self,
            forKey: .agentStaleThresholdSeconds
        ) ?? 300
        agentOfflineThresholdSeconds = try container.decodeIfPresent(
            Int.self,
            forKey: .agentOfflineThresholdSeconds
        ) ?? 900
    }

    /// Whether a command argv is permitted by the daemon's validation allowlist.
    /// Used for client-side rejection with Chinese copy before submitting.
    public func isValidationCommandAllowed(_ argv: [String]) -> Bool {
        guard !allowedValidationCommands.isEmpty else { return true }
        for prefix in allowedValidationCommands where !prefix.isEmpty {
            if argv.count >= prefix.count && Array(argv.prefix(prefix.count)) == prefix {
                return true
            }
        }
        return false
    }

    public func supportsAction(_ action: String) -> Bool {
        supportedActions.contains(action)
    }

    public func routeTemplate(for actionOrResource: String) -> String? {
        routeTemplates[actionOrResource]
    }
}
