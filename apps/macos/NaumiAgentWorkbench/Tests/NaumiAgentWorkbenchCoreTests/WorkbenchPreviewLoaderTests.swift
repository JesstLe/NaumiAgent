import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

@Suite(.serialized)
@MainActor
final class WorkbenchPreviewLoaderTests {

    @Test func requestedModeReadsEnglishLocale() {
        let mode = WorkbenchPreviewLoader.requestedMode(from: ["/bin/test", "--preview-fixture", "en"])

        #expect(mode == .enabled(.enUS))
    }

    @Test func requestedRouteReadsTopNavigationRoute() {
        let route = WorkbenchPreviewLoader.requestedRoute(
            from: ["/bin/test", "--preview-fixture", "zh", "--preview-route", "task-market"]
        )

        #expect(route == .taskMarket)
    }

    @Test func requestedModeDefaultsDisabledWhenMissingFlag() {
        let mode = WorkbenchPreviewLoader.requestedMode(from: ["/bin/test"])

        #expect(mode == .disabled)
    }

    @Test func requestedModeHandlesMalformedArgumentsAsMalformed() {
        let mode = WorkbenchPreviewLoader.requestedMode(from: ["/bin/test", "--preview-fixture"])

        #expect(mode == .malformed)
    }

    @Test func applyChineseFixtureIntoAppState() throws {
        let appState = AppState()
        let fixturesDirectory = fixtureDirectory()

        try WorkbenchPreviewLoader.applyPreviewState(
            locale: .zhCN,
            to: appState,
            fixtureDirectory: fixturesDirectory
        )

        #expect(appState.locale == .zhCN)
        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == "sess-zh-001")
        #expect(appState.sessions.count == 1)
        #expect(appState.snapshot?.sessionID == "sess-zh-001")
        #expect(appState.isPreviewFixture == true)
        #expect(appState.daemonStatus != nil)
        #expect(appState.capabilities != nil)
        #expect(appState.missions.count == 1)
        #expect(appState.issues.count == 1)
        #expect(appState.failures.count == 1)
        #expect(!appState.timelineEvents.isEmpty)
        #expect(appState.validationRuns.count == 2)
        #expect(appState.contextSnapshots.count == 6)
        #expect(appState.worktrees.count == 3)
        #expect(appState.worktrees.map(\.name) == ["wt-api-client", "wt-review-risk", "wt-validation-card"])
        #expect(appState.approvals.count == 2)
    }

    @Test func applyEnglishFixtureIntoAppState() throws {
        let appState = AppState()
        let fixturesDirectory = fixtureDirectory()

        try WorkbenchPreviewLoader.applyPreviewState(
            locale: .enUS,
            to: appState,
            fixtureDirectory: fixturesDirectory
        )

        #expect(appState.locale == .enUS)
        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == "sess-en-001")
        #expect(appState.snapshot?.sessionID == "sess-en-001")
        #expect(appState.sessions.count == 1)
        #expect(appState.sessions.first?.id == "sess-en-001")
        #expect(appState.daemonStatus?.status == "running")
        #expect(appState.worktrees.count == 3)
        #expect(appState.worktrees.last?.keptReason == "Waiting for human review")
    }

    @Test func malformedLocaleErrorsByDefault() {
        let malformedMode = WorkbenchPreviewLoader.requestedMode(from: ["/bin/test", "--preview-fixture", "xx"])

        #expect(malformedMode == .malformed)
    }

    @Test func missingFixtureThrows() {
        let appState = AppState()
        let missingDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("naumi-preview-fixture-missing-\(UUID().uuidString)")

        #expect(throws: WorkbenchPreviewLoader.Error.self) {
            try WorkbenchPreviewLoader.applyPreviewState(
                locale: .zhCN,
                to: appState,
                fixtureDirectory: missingDirectory
            )
        }
    }

    @Test func applyMinimalFixtureByNameLoadsSparseSession() throws {
        let appState = AppState()
        let fixturesDirectory = fixtureDirectory()

        try WorkbenchPreviewLoader.applyPreviewState(
            locale: .enUS,
            to: appState,
            fixtureDirectory: fixturesDirectory,
            fixtureName: "workbench_snapshot_minimal_en.json"
        )

        // The minimal fixture carries one mission and no other entities, so the
        // loaded preview state must reflect that sparse real session.
        #expect(appState.snapshot?.sessionID == "sess-minimal-en")
        #expect(appState.snapshot?.missions.count == 1)
        #expect(appState.snapshot?.agentProfiles == [])
        #expect(appState.snapshot?.tasks == [])
        #expect(appState.snapshot?.issues == [])
        #expect(appState.snapshot?.leases == [])
        #expect(appState.snapshot?.failures == [])
        #expect(appState.snapshot?.events == [])
    }

    @Test func explicitFixtureNameMissingThrows() {
        let appState = AppState()
        let fixturesDirectory = fixtureDirectory()

        #expect(throws: WorkbenchPreviewLoader.Error.self) {
            try WorkbenchPreviewLoader.applyPreviewState(
                locale: .enUS,
                to: appState,
                fixtureDirectory: fixturesDirectory,
                fixtureName: "workbench_snapshot_does_not_exist.json"
            )
        }
    }

    private func fixtureDirectory() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures")
    }
}
