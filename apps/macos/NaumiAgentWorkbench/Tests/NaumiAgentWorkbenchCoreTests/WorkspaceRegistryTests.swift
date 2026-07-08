import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct WorkspaceRegistryTests {

    // MARK: - Registry store

    @Test func storeRoundTripsEntriesAndSelection() throws {
        let url = makeTemporaryURL()
        let store = WorkspaceRegistryStore(url: url)
        var registry = WorkspaceRegistry.empty
        registry = registry.upserting(root: "/home/me/naumi", name: "naumi", lastEndpoint: nil, protocolVersion: 1)
        registry = registry.recordingSession("sess-1")
        try store.save(registry)

        let loaded = store.load()
        #expect(loaded.selectedRoot == "/home/me/naumi")
        #expect(loaded.entries.first?.name == "naumi")
        #expect(loaded.entries.first?.recentSessionIDs == ["sess-1"])
        #expect(loaded.entries.first?.lastSessionID == "sess-1")
    }

    @Test func storeFallsBackToEmptyOnCorrupt() {
        let url = makeTemporaryURL()
        try? Data("{ broken json".utf8).write(to: url)
        let store = WorkspaceRegistryStore(url: url)
        #expect(store.load() == .empty)
    }

    @Test func storeDefaultsToEmptyWhenMissing() {
        let store = WorkspaceRegistryStore(url: makeTemporaryURL())
        #expect(store.load() == .empty)
    }

    // MARK: - Registry logic

    @Test func upsertingPreservesRecentSessionsAcrossUpdates() {
        var registry = WorkspaceRegistry.empty
        registry = registry.upserting(root: "/ws", name: "ws", lastEndpoint: "http://127.0.0.1:8765", protocolVersion: 1)
        registry = registry.recordingSession("sess-a")
        registry = registry.recordingSession("sess-b")
        // Re-upsert (e.g. on reconnect): recent sessions must survive.
        registry = registry.upserting(root: "/ws", name: "ws-renamed", lastEndpoint: nil, protocolVersion: 1)
        let entry = registry.entry(forRoot: "/ws")
        #expect(entry?.name == "ws-renamed")
        #expect(entry?.recentSessionIDs == ["sess-b", "sess-a"])
        #expect(entry?.lastSessionID == "sess-b")
    }

    @Test func recordingSessionDedupesAndMovesToFront() {
        var registry = WorkspaceRegistry.empty
        registry = registry.upserting(root: "/ws", name: "ws", lastEndpoint: nil, protocolVersion: nil)
        registry = registry.recordingSession("sess-1")
        registry = registry.recordingSession("sess-2")
        registry = registry.recordingSession("sess-3")
        // Re-selecting sess-1 promotes it.
        registry = registry.recordingSession("sess-1")
        #expect(registry.selectedEntry?.recentSessionIDs == ["sess-1", "sess-3", "sess-2"])
    }

    @Test func recordingSessionCapsToLimit() {
        var registry = WorkspaceRegistry.empty
        registry = registry.upserting(root: "/ws", name: "ws", lastEndpoint: nil, protocolVersion: nil)
        for index in 0..<(WorkspaceRegistry.recentSessionLimit + 5) {
            registry = registry.recordingSession("sess-\(index)")
        }
        #expect(registry.selectedEntry?.recentSessionIDs.count == WorkspaceRegistry.recentSessionLimit)
    }

    @Test func recordingSessionNoOpWithoutSelectedWorkspace() {
        var registry = WorkspaceRegistry.empty
        let updated = registry.recordingSession("sess-x")
        #expect(updated == registry)
    }

    // MARK: - Switcher presentation

    @Test func switcherPresentationListsRecentSessionsResolvedAgainstLiveList() {
        let sessions = [
            makeSession(id: "sess-1", title: "Alpha"),
            makeSession(id: "sess-2", title: "Beta"),
            makeSession(id: "sess-3", title: "Gamma")
        ]
        var registry = WorkspaceRegistry.empty
        registry = registry.upserting(root: "/ws", name: "ws", lastEndpoint: nil, protocolVersion: nil)
        registry = registry.recordingSession("sess-3")
        registry = registry.recordingSession("sess-1")

        let presentation = WorkspaceSwitcherPresentation(
            registry: registry,
            sessions: sessions,
            selectedSessionID: "sess-1",
            activeWorkspaceLabel: nil
        )
        #expect(presentation.activeWorkspaceTitle == "ws")
        #expect(presentation.activeSessionTitle == "Alpha")
        #expect(presentation.recentSessions.map(\.id) == ["sess-1", "sess-3"])
        #expect(presentation.recentSessions.first?.isSelected == true)
    }

    @Test func switcherPresentationSkipsRegistrySessionsMissingFromLiveList() {
        let sessions = [makeSession(id: "sess-1", title: "Alpha")]
        var registry = WorkspaceRegistry.empty
        registry = registry.upserting(root: "/ws", name: "ws", lastEndpoint: nil, protocolVersion: nil)
        registry = registry.recordingSession("sess-1")
        registry = registry.recordingSession("sess-gone")

        let presentation = WorkspaceSwitcherPresentation(
            registry: registry,
            sessions: sessions,
            selectedSessionID: nil,
            activeWorkspaceLabel: nil
        )
        #expect(presentation.recentSessions.map(\.id) == ["sess-1"])
    }

    // MARK: - Helpers

    private func makeTemporaryURL() -> URL {
        URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("naumi-workspace-\(UUID().uuidString).json")
    }

    private func makeSession(id: String, title: String) -> SessionDTO {
        SessionDTO(
            id: id,
            title: title,
            model: "gpt-4o",
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:30:00",
            messageCount: 1,
            totalTokens: 10,
            totalCostUSD: 0.001,
            status: "active"
        )
    }
}
