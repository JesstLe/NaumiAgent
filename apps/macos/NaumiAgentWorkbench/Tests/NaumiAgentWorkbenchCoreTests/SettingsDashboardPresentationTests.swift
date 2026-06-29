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
            protocolVersion: 1
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

        let presentation = SettingsDashboardPresentation(appState: state)

        #expect(presentation.runtimeEndpoint == "127.0.0.1:8765")
        #expect(presentation.activeMissionTitle == "实现 SwiftUI 工作台骨架")
        #expect(presentation.enabledCapabilityCount == 2)
        #expect(presentation.governancePolicyCount == 3)
        #expect(presentation.runtimeChecklist.map(\.kind) == [
            .loopbackOnly,
            .protocolCompatible,
            .validationRunnerAvailable,
        ])
        #expect(presentation.runtimeChecklist.map(\.state) == [
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
    }

    @Test func disconnectedRuntimeUsesPlaceholderEndpoint() {
        let presentation = SettingsDashboardPresentation(appState: AppState())

        #expect(presentation.runtimeEndpoint == "-")
        #expect(presentation.activeMissionTitle == "-")
        #expect(presentation.enabledCapabilityCount == 0)
        #expect(presentation.runtimeChecklist.map(\.state) == [
            .blocked,
            .blocked,
            .blocked,
        ])
        #expect(presentation.intentLocks == [])
    }
}
