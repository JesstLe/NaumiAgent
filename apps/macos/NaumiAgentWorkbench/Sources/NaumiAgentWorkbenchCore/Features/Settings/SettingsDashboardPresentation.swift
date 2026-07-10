import Foundation

/// Dashboard-level summary for runtime settings and governance.
@MainActor
public struct SettingsDashboardPresentation: Equatable {
    public let runtimeEndpoint: String
    public let workspaceSummary: String
    public let apiBaseURL: String
    public let workbenchBaseURL: String
    public let eventStreamURLTemplate: String
    public let authMode: String
    public let activeMissionTitle: String
    public let enabledCapabilityCount: Int
    public let supportedActionCount: Int
    public let routeTemplateCount: Int
    public let missingActionRouteTemplates: [String]
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
            self.workspaceSummary = SettingsDashboardPresentation.workspaceSummary(
                status: daemonStatus
            )
            self.apiBaseURL = daemonStatus.apiBaseURL
            self.workbenchBaseURL = daemonStatus.workbenchBaseURL
            self.eventStreamURLTemplate = daemonStatus.eventStreamURLTemplate
            self.authMode = daemonStatus.authMode
        } else {
            self.runtimeEndpoint = "-"
            self.workspaceSummary = "-"
            self.apiBaseURL = "-"
            self.workbenchBaseURL = "-"
            self.eventStreamURLTemplate = "-"
            self.authMode = "-"
        }
        self.activeMissionTitle = appState.missions.first?.title ?? "-"
        self.enabledCapabilityCount = SettingsDashboardPresentation.enabledCapabilityCount(
            capabilities: appState.capabilities
        )
        self.supportedActionCount = appState.capabilities?.supportedActions.count ?? 0
        self.routeTemplateCount = appState.capabilities?.routeTemplates.count ?? 0
        self.missingActionRouteTemplates = SettingsDashboardPresentation.missingActionRouteTemplates(
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
                createdBy: $0.createdBy,
                createdAt: $0.createdAt,
                updatedAt: $0.updatedAt
            )
        }
        self.decisions = appState.decisions.map {
            SettingsDecisionRow(
                id: $0.id,
                missionID: $0.missionID,
                kind: $0.kind,
                title: $0.title,
                actor: $0.actor,
                strength: $0.strength,
                strengthLabel: $0.strengthLabel(locale: appState.locale),
                createdAt: $0.createdAt
            )
        }
    }

    private static func workspaceSummary(status: DaemonStatusDTO) -> String {
        if !status.workspaceName.isEmpty && !status.workspaceRoot.isEmpty {
            return "\(status.workspaceName) · \(status.workspaceRoot)"
        }
        if !status.workspaceRoot.isEmpty {
            return status.workspaceRoot
        }
        if !status.workspaceName.isEmpty {
            return status.workspaceName
        }
        return "-"
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

    private static func missingActionRouteTemplates(capabilities: CapabilitiesDTO?) -> [String] {
        guard let capabilities else { return [] }
        return capabilities.supportedActions.filter {
            capabilities.routeTemplate(for: $0) == nil
        }
    }

    private static func actionRouteTemplateState(capabilities: CapabilitiesDTO?) -> SettingsChecklistState {
        guard let capabilities else { return .blocked }
        return missingActionRouteTemplates(capabilities: capabilities).isEmpty ? .passed : .warning
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
            SettingsChecklistItem(
                kind: .actionRouteTemplates,
                state: actionRouteTemplateState(capabilities: appState.capabilities)
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
    case actionRouteTemplates
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
        case .actionRouteTemplates:
            return locale == .zhCN ? "动作路由模板完整" : "Action route templates complete"
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
    public let createdBy: String
    public let createdAt: String
    public let updatedAt: String

    public init(
        id: String,
        missionID: String,
        rule: String,
        scopeSummary: String,
        riskLabel: String,
        isActive: Bool,
        createdBy: String,
        createdAt: String,
        updatedAt: String
    ) {
        self.id = id
        self.missionID = missionID
        self.rule = rule
        self.scopeSummary = scopeSummary
        self.riskLabel = riskLabel
        self.isActive = isActive
        self.createdBy = createdBy
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }
}

public struct SettingsDecisionRow: Equatable, Sendable, Identifiable {
    public let id: String
    public let missionID: String
    public let kind: String
    public let title: String
    public let actor: String
    public let strength: String
    public let strengthLabel: String
    public let createdAt: String

    public init(
        id: String,
        missionID: String,
        kind: String,
        title: String,
        actor: String,
        strength: String,
        strengthLabel: String,
        createdAt: String
    ) {
        self.id = id
        self.missionID = missionID
        self.kind = kind
        self.title = title
        self.actor = actor
        self.strength = strength
        self.strengthLabel = strengthLabel
        self.createdAt = createdAt
    }
}
