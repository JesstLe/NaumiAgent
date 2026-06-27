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
    public let runtimeChecklist: [SettingsChecklistItem]
    public let governanceChecklist: [SettingsChecklistItem]

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
        self.runtimeChecklist = SettingsDashboardPresentation.runtimeChecklist(
            appState: appState
        )
        self.governanceChecklist = SettingsDashboardPresentation.governanceChecklist(
            appState: appState
        )
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

    private static func runtimeChecklist(appState: AppState) -> [SettingsChecklistItem] {
        [
            SettingsChecklistItem(
                kind: .loopbackOnly,
                state: appState.daemonStatus?.host == "127.0.0.1" ? .passed : .blocked
            ),
            SettingsChecklistItem(
                kind: .protocolCompatible,
                state: appState.capabilities?.protocolVersion == DaemonController.supportedProtocolVersion ? .passed : .blocked
            ),
            SettingsChecklistItem(
                kind: .validationRunnerAvailable,
                state: appState.capabilities?.supportsValidationRunner == true ? .passed : .blocked
            ),
        ]
    }

    private static func governanceChecklist(appState: AppState) -> [SettingsChecklistItem] {
        [
            SettingsChecklistItem(kind: .humanApproval, state: .passed),
            SettingsChecklistItem(kind: .workbenchWritePath, state: .passed),
            SettingsChecklistItem(
                kind: .intentLockReady,
                state: appState.connectionState == .connected ? .passed : .warning
            ),
        ]
    }
}

public enum SettingsChecklistKind: Equatable, Sendable {
    case loopbackOnly
    case protocolCompatible
    case validationRunnerAvailable
    case humanApproval
    case workbenchWritePath
    case intentLockReady
}

public enum SettingsChecklistState: Equatable, Sendable {
    case passed
    case warning
    case blocked
}

public struct SettingsChecklistItem: Equatable, Sendable, Identifiable {
    public var id: SettingsChecklistKind { kind }
    public let kind: SettingsChecklistKind
    public let state: SettingsChecklistState

    public func title(locale: AppLocale) -> String {
        switch kind {
        case .loopbackOnly:
            return locale == .zhCN ? "仅监听本机回环地址" : "Loopback-only daemon"
        case .protocolCompatible:
            return locale == .zhCN ? "Workbench 协议兼容" : "Workbench protocol compatible"
        case .validationRunnerAvailable:
            return locale == .zhCN ? "验证运行器可用" : "Validation runner available"
        case .humanApproval:
            return locale == .zhCN ? "高风险动作人工审批" : "Human approval for high risk"
        case .workbenchWritePath:
            return locale == .zhCN ? "写操作经 Workbench API" : "Writes routed through Workbench API"
        case .intentLockReady:
            return locale == .zhCN ? "意图锁入口就绪" : "Intent lock entry ready"
        }
    }

    public func stateLabel(locale: AppLocale) -> String {
        switch state {
        case .passed:
            return locale == .zhCN ? "通过" : "Passed"
        case .warning:
            return locale == .zhCN ? "需确认" : "Check"
        case .blocked:
            return locale == .zhCN ? "阻塞" : "Blocked"
        }
    }

    public var systemImage: String {
        switch state {
        case .passed:
            return "checkmark.circle.fill"
        case .warning:
            return "exclamationmark.triangle.fill"
        case .blocked:
            return "xmark.octagon.fill"
        }
    }
}
