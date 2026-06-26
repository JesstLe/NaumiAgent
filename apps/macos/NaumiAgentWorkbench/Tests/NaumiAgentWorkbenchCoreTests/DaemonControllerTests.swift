import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

/// In-memory fake conforming to `WorkbenchAPIProviding` for unit tests.
actor FakeWorkbenchAPIProvider: WorkbenchAPIProviding {
    var statusResult: Result<DaemonStatusDTO, APIError>?
    var capabilitiesResult: Result<CapabilitiesDTO, APIError>?
    var snapshotResult: Result<WorkbenchSnapshotDTO, APIError>?
    var sessionsResult: Result<SessionListDTO, APIError>?

    func fetchDaemonStatus() async throws(APIError) -> DaemonStatusDTO {
        guard let result = statusResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchCapabilities() async throws(APIError) -> CapabilitiesDTO {
        guard let result = capabilitiesResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchSnapshot(sessionID: String) async throws(APIError) -> WorkbenchSnapshotDTO {
        guard let result = snapshotResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchSessions(page: Int, pageSize: Int) async throws(APIError) -> SessionListDTO {
        guard let result = sessionsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }
}

@Suite
final class DaemonControllerTests {

    @Test @MainActor func refreshConnectionSuccess() async throws {
        let appState = AppState()
        let api = FakeWorkbenchAPIProvider()

        let status = DaemonStatusDTO(
            status: "running",
            version: "0.2.0",
            pid: 42,
            host: "127.0.0.1",
            port: 8765,
            startedAt: "2026-06-27T06:00:00",
            workspaceCount: 7
        )
        let capabilities = CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: true,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 1
        )
        let sessions = SessionListDTO(
            sessions: [],
            total: 0,
            page: 1,
            pageSize: 1
        )

        await api.setStatusResult(.success(status))
        await api.setCapabilitiesResult(.success(capabilities))
        await api.setSessionsResult(.success(sessions))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.daemonStatus == status)
        #expect(appState.capabilities == capabilities)
        #expect(appState.lastError == nil)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func refreshConnectionFailure() async throws {
        let appState = AppState()
        let api = FakeWorkbenchAPIProvider()

        await api.setStatusResult(.failure(.httpStatus(503)))
        await api.setCapabilitiesResult(.success(CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: false,
            supportsValidationRunner: false,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN"],
            protocolVersion: 1
        )))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .disconnected)
        #expect(appState.lastError == .httpStatus(503))
        #expect(appState.daemonStatus == nil)
        #expect(appState.capabilities == nil)
    }

    @Test @MainActor func refreshConnectionClearsPreviousError() async throws {
        let appState = AppState()
        appState.lastError = .httpStatus(500)

        let api = FakeWorkbenchAPIProvider()
        let status = DaemonStatusDTO(
            status: "running",
            version: "0.2.0",
            pid: 42,
            host: "127.0.0.1",
            port: 8765,
            startedAt: "2026-06-27T06:00:00",
            workspaceCount: 7
        )
        await api.setStatusResult(.success(status))
        await api.setCapabilitiesResult(.success(CapabilitiesDTO(
            supportsDaemonManagement: false,
            supportsWorkspaceRegistry: true,
            supportsValidationRunner: true,
            supportsCloudSync: false,
            supportedLocales: ["zh-CN", "en-US"],
            protocolVersion: 1
        )))
        await api.setSessionsResult(.success(SessionListDTO(
            sessions: [],
            total: 0,
            page: 1,
            pageSize: 1
        )))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshConnectionRefreshesSnapshotForSelectedSession() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-existing"

        let api = FakeWorkbenchAPIProvider()
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-existing",
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(makeCapabilities()))
        await api.setSnapshotResult(.success(snapshot))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == "sess-existing")
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshConnectionAutoSelectsMostRecentSession() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let session = SessionDTO(
            id: "sess-latest",
            title: "Latest Session",
            model: "gpt-4o",
            createdAt: "2026-06-27T06:00:00",
            updatedAt: "2026-06-27T06:30:00",
            messageCount: 3,
            totalTokens: 120,
            totalCostUSD: 0.0012,
            status: "active"
        )
        let sessions = SessionListDTO(
            sessions: [session],
            total: 1,
            page: 1,
            pageSize: 1
        )
        let snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-latest",
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(makeCapabilities()))
        await api.setSessionsResult(.success(sessions))
        await api.setSnapshotResult(.success(snapshot))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == "sess-latest")
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshConnectionKeepsConnectedWhenSessionsEmpty() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(makeCapabilities()))
        await api.setSessionsResult(.success(SessionListDTO(
            sessions: [],
            total: 0,
            page: 1,
            pageSize: 1
        )))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == nil)
        #expect(appState.snapshot == nil)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshConnectionKeepsConnectedAndClearsSnapshotOnSnapshotFailure() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-existing"
        appState.snapshot = WorkbenchSnapshotDTO(
            sessionID: "sess-stale",
            missions: [],
            tasks: [],
            issues: [],
            failures: [],
            events: []
        )

        let api = FakeWorkbenchAPIProvider()
        await api.setStatusResult(.success(makeStatus()))
        await api.setCapabilitiesResult(.success(makeCapabilities()))
        await api.setSnapshotResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshConnection()

        #expect(appState.connectionState == .connected)
        #expect(appState.selectedSessionID == "sess-existing")
        #expect(appState.snapshot == nil)
        #expect(appState.lastError == .httpStatus(500))
    }
}

extension FakeWorkbenchAPIProvider {
    fileprivate func setStatusResult(_ result: Result<DaemonStatusDTO, APIError>) {
        statusResult = result
    }

    fileprivate func setCapabilitiesResult(_ result: Result<CapabilitiesDTO, APIError>) {
        capabilitiesResult = result
    }

    fileprivate func setSnapshotResult(_ result: Result<WorkbenchSnapshotDTO, APIError>) {
        snapshotResult = result
    }

    fileprivate func setSessionsResult(_ result: Result<SessionListDTO, APIError>) {
        sessionsResult = result
    }
}

private func makeStatus() -> DaemonStatusDTO {
    DaemonStatusDTO(
        status: "running",
        version: "0.2.0",
        pid: 42,
        host: "127.0.0.1",
        port: 8765,
        startedAt: "2026-06-27T06:00:00",
        workspaceCount: 7
    )
}

private func makeCapabilities() -> CapabilitiesDTO {
    CapabilitiesDTO(
        supportsDaemonManagement: false,
        supportsWorkspaceRegistry: true,
        supportsValidationRunner: true,
        supportsCloudSync: false,
        supportedLocales: ["zh-CN", "en-US"],
        protocolVersion: 1
    )
}
