import Testing
@testable import NaumiAgentWorkbenchCore

@MainActor
struct SettingsDashboardPresentationTests {

    @Test func summarizesRuntimeAndGovernanceState() {
        let state = AppState()
        state.locale = .zhCN
        state.connectionState = .connected
        state.daemonStatus = DaemonStatusDTO(
            status: "running",
            version: "preview",
            pid: 4242,
            host: "127.0.0.1",
            port: 8765,
            startedAt: "2026-06-27T09:00:00",
            workspaceCount: 1
        )
        state.capabilities = CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: true,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 1,
            supportedActions: ["create_session", "send_message", "run_validation"],
            routeTemplates: [
                "create_session": "/workbench/sessions",
                "send_message": "/sessions/{session_id}/messages",
                "run_validation": "/workbench/sessions/{session_id}/validation-runs",
            ]
        )
        state.missions = [
            MissionDTO(
                id: "mission-1",
                sessionID: "sess-1",
                title: "实现 SwiftUI 工作台骨架",
                goal: "Build",
                status: "active",
                createdAt: "2026-06-27T09:00:00",
                updatedAt: "2026-06-27T09:10:00"
            )
        ]
        state.intentLocks = [
            IntentLockDTO(
                id: " lock-001 ",
                sessionID: "sess-1",
                missionID: " mission-1 ",
                rule: "禁止直接改动认证模块",
                blockedPaths: ["src/auth/**"],
                allowedPaths: ["docs/**"],
                requireProposalForRisk: "high",
                active: true,
                createdAt: "2026-06-27T09:12:00"
            )
        ]
        state.decisions = [
            DecisionDTO(
                id: " decision-001 ",
                sessionID: "sess-1",
                missionID: " mission-1 ",
                kind: "architecture",
                title: "采用本地 daemon 桥接",
                content: "SwiftUI 只通过 Workbench API 访问本地服务。",
                actor: "Planner-Agent",
                createdAt: "2026-06-27T09:18:00"
            )
        ]

        let presentation = SettingsDashboardPresentation(appState: state)

        #expect(presentation.runtimeEndpoint == "127.0.0.1:8765")
        #expect(presentation.activeMissionTitle == "实现 SwiftUI 工作台骨架")
        #expect(presentation.enabledCapabilityCount == 2)
        #expect(presentation.supportedActionCount == 3)
        #expect(presentation.routeTemplateCount == 3)
        #expect(presentation.missingActionRouteTemplates == [])
        #expect(presentation.governancePolicyCount == 3)
        #expect(presentation.runtimeChecklist.map(\.kind) == [
            .loopbackOnly,
            .protocolCompatible,
            .validationRunnerAvailable,
            .actionRouteTemplates,
        ])
        #expect(presentation.runtimeChecklist.map(\.state) == [
            .passed,
            .passed,
            .passed,
            .passed,
        ])
        #expect(presentation.governanceChecklist.map(\.kind) == [
            .humanApproval,
            .workbenchWritePath,
            .intentLockReady,
        ])
        #expect(presentation.intentLocks.count == 1)
        #expect(presentation.intentLocks.first?.id == " lock-001 ")
        #expect(presentation.intentLocks.first?.missionID == " mission-1 ")
        #expect(presentation.intentLocks.first?.rule == "禁止直接改动认证模块")
        #expect(presentation.intentLocks.first?.scopeSummary == "阻塞 1 / 允许 1")
        #expect(presentation.intentLocks.first?.riskLabel == "high")
        #expect(presentation.intentLocks.first?.isActive == true)
        #expect(presentation.decisions.count == 1)
        #expect(presentation.decisions.first?.id == " decision-001 ")
        #expect(presentation.decisions.first?.missionID == " mission-1 ")
        #expect(presentation.decisions.first?.title == "采用本地 daemon 桥接")
        #expect(presentation.decisions.first?.kind == "architecture")
        #expect(presentation.decisions.first?.actor == "Planner-Agent")
        #expect(presentation.decisions.first?.createdAt == "2026-06-27T09:18:00")
    }

    @Test func disconnectedRuntimeUsesPlaceholderEndpoint() {
        let presentation = SettingsDashboardPresentation(appState: AppState())

        #expect(presentation.runtimeEndpoint == "-")
        #expect(presentation.activeMissionTitle == "-")
        #expect(presentation.enabledCapabilityCount == 0)
        #expect(presentation.supportedActionCount == 0)
        #expect(presentation.routeTemplateCount == 0)
        #expect(presentation.missingActionRouteTemplates == [])
        #expect(presentation.runtimeChecklist.map(\.state) == [
            .blocked,
            .blocked,
            .blocked,
            .blocked,
        ])
        #expect(presentation.intentLocks == [])
        #expect(presentation.decisions == [])
    }

    @Test func flagsMissingActionRouteTemplates() {
        let state = AppState()
        state.daemonStatus = DaemonStatusDTO(
            status: "running",
            version: "preview",
            pid: 4242,
            host: "127.0.0.1",
            port: 8765,
            startedAt: "2026-06-27T09:00:00",
            workspaceCount: 1
        )
        state.capabilities = CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: false,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 1,
            supportedActions: [
                "create_session",
                "run_validation",
                "send_message_with_issue",
            ],
            routeTemplates: [
                "create_session": "/workbench/sessions",
                "send_message_with_issue": "/sessions/{session_id}/messages",
            ]
        )

        let presentation = SettingsDashboardPresentation(appState: state)

        #expect(presentation.supportedActionCount == 3)
        #expect(presentation.routeTemplateCount == 2)
        #expect(presentation.missingActionRouteTemplates == ["run_validation"])
        #expect(presentation.runtimeChecklist.last?.kind == .actionRouteTemplates)
        #expect(presentation.runtimeChecklist.last?.state == .warning)
    }
}
