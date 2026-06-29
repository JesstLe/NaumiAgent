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
    public let intentLocks: [SettingsIntentLockRow]
    public let decisions: [SettingsDecisionRow]

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
        self.intentLocks = appState.intentLocks.map {
            SettingsIntentLockRow(
                id: $0.id,
                missionID: $0.missionID,
                rule: $0.rule,
                scopeSummary: SettingsDashboardPresentation.scopeSummary(for: $0, locale: appState.locale),
                riskLabel: $0.requireProposalForRisk,
                isActive: $0.active,
                createdAt: $0.createdAt
            )
        }
        self.decisions = appState.decisions.map {
            SettingsDecisionRow(
                id: $0.id,
                missionID: $0.missionID,
                kind: $0.kind,
                title: $0.title,
                actor: $0.actor,
                createdAt: $0.createdAt
            )
        }
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

    private static func scopeSummary(for lock: IntentLockDTO, locale: AppLocale) -> String {
        if locale == .zhCN {
            return "阻塞 \(lock.blockedPaths.count) / 允许 \(lock.allowedPaths.count)"
        }
        return "Blocked \(lock.blockedPaths.count) / Allowed \(lock.allowedPaths.count)"
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

public struct SettingsIntentLockRow: Equatable, Sendable, Identifiable {
    public let id: String
    public let missionID: String
    public let rule: String
    public let scopeSummary: String
    public let riskLabel: String
    public let isActive: Bool
    public let createdAt: String

    public init(
        id: String,
        missionID: String,
        rule: String,
        scopeSummary: String,
        riskLabel: String,
        isActive: Bool,
        createdAt: String
    ) {
        self.id = id
        self.missionID = missionID
        self.rule = rule
        self.scopeSummary = scopeSummary
        self.riskLabel = riskLabel
        self.isActive = isActive
        self.createdAt = createdAt
    }
}

public struct SettingsDecisionRow: Equatable, Sendable, Identifiable {
    public let id: String
    public let missionID: String
    public let kind: String
    public let title: String
    public let actor: String
    public let createdAt: String

    public init(
        id: String,
        missionID: String,
        kind: String,
        title: String,
        actor: String,
        createdAt: String
    ) {
        self.id = id
        self.missionID = missionID
        self.kind = kind
        self.title = title
        self.actor = actor
        self.createdAt = createdAt
    }
}
