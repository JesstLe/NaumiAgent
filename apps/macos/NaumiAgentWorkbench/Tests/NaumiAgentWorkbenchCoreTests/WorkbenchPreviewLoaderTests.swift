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
        #expect(appState.daemonStatus != nil)
        #expect(appState.capabilities != nil)
        #expect(appState.missions.count == 1)
        #expect(appState.issues.count == 1)
        #expect(appState.failures.count == 1)
        #expect(!appState.timelineEvents.isEmpty)
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

    private func fixtureDirectory() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures")
    }
}
