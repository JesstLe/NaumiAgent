import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

/// In-memory fake conforming to `WorkbenchAPIProviding` for unit tests.
actor FakeWorkbenchAPIProvider: WorkbenchAPIProviding {
    var statusResult: Result<DaemonStatusDTO, APIError>?
    var capabilitiesResult: Result<CapabilitiesDTO, APIError>?
    var snapshotResult: Result<WorkbenchSnapshotDTO, APIError>?
    var sessionsResult: Result<SessionListDTO, APIError>?
    var eventsResult: Result<WorkbenchEventsDTO, APIError>?
    var validationRunsResult: Result<ValidationRunsDTO, APIError>?
    var contextSnapshotsResult: Result<ContextSnapshotsDTO, APIError>?
    var claimIssueResult: Result<LeaseDTO, APIError>?
    var releaseLeaseResult: Result<LeaseDTO, APIError>?
    var expireLeasesResult: Result<ExpiredLeasesDTO, APIError>?
    var createMissionResult: Result<MissionDTO, APIError>?
    var attachIssueResult: Result<IssueDTO, APIError>?
    var runValidationResult: Result<ValidationResultDTO, APIError>?

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

    func fetchEvents(sessionID: String, limit: Int) async throws(APIError) -> WorkbenchEventsDTO {
        guard let result = eventsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchValidationRuns(
        sessionID: String,
        taskID: String?,
        limit: Int
    ) async throws(APIError) -> ValidationRunsDTO {
        guard let result = validationRunsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func fetchContextSnapshots(
        sessionID: String,
        taskID: String?,
        agentID: String?,
        limit: Int
    ) async throws(APIError) -> ContextSnapshotsDTO {
        guard let result = contextSnapshotsResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func claimIssue(
        sessionID: String,
        taskID: String,
        agentID: String,
        durationMinutes: Int,
        worktreeName: String
    ) async throws(APIError) -> LeaseDTO {
        guard let result = claimIssueResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func releaseLease(sessionID: String, leaseID: String) async throws(APIError) -> LeaseDTO {
        guard let result = releaseLeaseResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func expireLeases(sessionID: String) async throws(APIError) -> ExpiredLeasesDTO {
        guard let result = expireLeasesResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func createMission(
        sessionID: String,
        title: String,
        goal: String
    ) async throws(APIError) -> MissionDTO {
        guard let result = createMissionResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func attachIssue(
        sessionID: String,
        missionID: String,
        taskID: String,
        acceptanceCriteria: [String],
        parallelMode: String,
        riskLevel: String
    ) async throws(APIError) -> IssueDTO {
        guard let result = attachIssueResult else {
            throw .invalidResponse
        }
        return try result.get()
    }

    func runValidation(
        sessionID: String,
        taskID: String,
        actor: String,
        argv: [String],
        cwd: String?
    ) async throws(APIError) -> ValidationResultDTO {
        guard let result = runValidationResult else {
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

    @Test @MainActor func claimIssueSuccessRefreshesSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let lease = makeLease(id: "lease-001", taskID: "task-001", state: "active")
        let snapshot = makeSnapshot(sessionID: "sess-001", lease: lease)

        await api.setClaimIssueResult(.success(lease))
        await api.setSnapshotResult(.success(snapshot))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.claimIssue(
            taskID: "task-001",
            agentID: "local-agent",
            durationMinutes: 30,
            worktreeName: "wt-001"
        )

        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func claimIssueWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.claimIssue(
            taskID: "task-001",
            agentID: "local-agent",
            durationMinutes: 30,
            worktreeName: "wt-001"
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func releaseLeaseSuccessRefreshesSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let lease = makeLease(id: "lease-001", taskID: "task-001", state: "released")
        let snapshot = makeSnapshot(sessionID: "sess-001", lease: lease)

        await api.setReleaseLeaseResult(.success(lease))
        await api.setSnapshotResult(.success(snapshot))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.releaseLease(leaseID: "lease-001")

        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func expireLeasesSuccessRefreshesSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let expiredLease = makeLease(id: "lease-001", taskID: "task-001", state: "expired")
        let expired = ExpiredLeasesDTO(expired: [expiredLease])
        let snapshot = makeSnapshot(sessionID: "sess-001", lease: expiredLease)

        await api.setExpireLeasesResult(.success(expired))
        await api.setSnapshotResult(.success(snapshot))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.expireLeases()

        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func expireLeasesWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.expireLeases()

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func expireLeasesFailurePreservesOldSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setExpireLeasesResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.expireLeases()

        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func createMissionSuccessRefreshesSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let mission = makeMission(id: "mission-001", sessionID: "sess-001")
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [mission])

        await api.setCreateMissionResult(.success(mission))
        await api.setSnapshotResult(.success(snapshot))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createMission(title: "Mac 工作台", goal: "补齐 API 调用面")

        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func createMissionWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createMission(title: "Title", goal: "Goal")

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func createMissionFailurePreservesOldSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setCreateMissionResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.createMission(title: "Title", goal: "Goal")

        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func attachIssueSuccessRefreshesSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let issue = makeIssue(taskID: "task-001", missionID: "mission-001")
        let snapshot = makeSnapshot(sessionID: "sess-001", issues: [issue])

        await api.setAttachIssueResult(.success(issue))
        await api.setSnapshotResult(.success(snapshot))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.attachIssue(
            missionID: "mission-001",
            taskID: "task-001",
            acceptanceCriteria: ["通过 Swift 编译"],
            parallelMode: "exclusive",
            riskLevel: "medium"
        )

        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func attachIssueWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.attachIssue(
            missionID: "mission-001",
            taskID: "task-001",
            acceptanceCriteria: [],
            parallelMode: "exclusive",
            riskLevel: "low"
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.snapshot == nil)
    }

    @Test @MainActor func attachIssueFailurePreservesOldSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", issues: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setAttachIssueResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.attachIssue(
            missionID: "mission-001",
            taskID: "task-001",
            acceptanceCriteria: [],
            parallelMode: "cooperative",
            riskLevel: "high"
        )

        #expect(appState.snapshot == staleSnapshot)
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshEventsSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let event = EventDTO(
            id: "evt-001",
            sessionID: "sess-001",
            type: "mission.created",
            actor: "Human",
            subjectID: "mission-001",
            payload: ["title": .string("Mac Workbench")],
            timestamp: "2026-06-27T06:00:00"
        )
        let events = WorkbenchEventsDTO(events: [event], limit: 50)

        await api.setEventsResult(.success(events))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshEvents(limit: 50)

        #expect(appState.timelineEvents == [event])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshEventsWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.timelineEvents = [
            EventDTO(
                id: "evt-stale",
                sessionID: "old-session",
                type: "mission.created",
                actor: "Human",
                subjectID: "mission-old",
                payload: ["title": .string("旧事件")],
                timestamp: "2026-06-27T05:00:00"
            )
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshEvents(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.timelineEvents.isEmpty)
    }

    @Test @MainActor func refreshValidationRunsSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let run = ValidationRunDTO(
            id: "run-001",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "ValidationRunner",
            command: ["pytest", "test.py"],
            cwd: "/workspace",
            status: "passed",
            exitCode: 0,
            output: "ok",
            startedAt: "2026-06-27T06:00:00",
            completedAt: "2026-06-27T06:00:01"
        )
        let runs = ValidationRunsDTO(validationRuns: [run], taskID: "task-001", limit: 25)

        await api.setValidationRunsResult(.success(runs))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshValidationRuns(taskID: "task-001", limit: 25)

        #expect(appState.validationRuns == [run])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshValidationRunsWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.validationRuns = [
            ValidationRunDTO(
                id: "run-stale",
                sessionID: "old-session",
                taskID: "task-old",
                actor: "ValidationRunner",
                command: [],
                cwd: "",
                status: "passed",
                exitCode: 0,
                output: "",
                startedAt: "2026-06-27T05:00:00",
                completedAt: "2026-06-27T05:00:01"
            )
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshValidationRuns(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.validationRuns.isEmpty)
    }

    @Test @MainActor func refreshValidationRunsFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleRun = ValidationRunDTO(
            id: "run-stale",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "ValidationRunner",
            command: [],
            cwd: "",
            status: "passed",
            exitCode: 0,
            output: "",
            startedAt: "2026-06-27T05:00:00",
            completedAt: "2026-06-27T05:00:01"
        )
        appState.validationRuns = [staleRun]

        let api = FakeWorkbenchAPIProvider()
        await api.setValidationRunsResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshValidationRuns(limit: 50)

        #expect(appState.validationRuns == [staleRun])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func refreshContextSnapshotsSuccessWritesToAppState() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let snapshot = ContextSnapshotDTO(
            id: "snap-001",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "good",
            reasons: ["上下文健康"],
            createdAt: "2026-06-27T06:00:00"
        )
        let snapshots = ContextSnapshotsDTO(
            contextSnapshots: [snapshot],
            taskID: "task-001",
            agentID: "agent-001",
            limit: 25
        )

        await api.setContextSnapshotsResult(.success(snapshots))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshContextSnapshots(taskID: "task-001", agentID: "agent-001", limit: 25)

        #expect(appState.contextSnapshots == [snapshot])
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func refreshContextSnapshotsWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.contextSnapshots = [
            ContextSnapshotDTO(
                id: "snap-stale",
                sessionID: "old-session",
                agentID: "agent-old",
                taskID: "task-old",
                health: "stale",
                reasons: ["旧数据"],
                createdAt: "2026-06-27T05:00:00"
            )
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshContextSnapshots(limit: 50)

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.contextSnapshots.isEmpty)
    }

    @Test @MainActor func refreshContextSnapshotsFailurePreservesOldList() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleSnapshot = ContextSnapshotDTO(
            id: "snap-stale",
            sessionID: "sess-001",
            agentID: "agent-001",
            taskID: "task-001",
            health: "stale",
            reasons: ["旧数据"],
            createdAt: "2026-06-27T05:00:00"
        )
        appState.contextSnapshots = [staleSnapshot]

        let api = FakeWorkbenchAPIProvider()
        await api.setContextSnapshotsResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.refreshContextSnapshots(limit: 50)

        #expect(appState.contextSnapshots == [staleSnapshot])
        #expect(appState.lastError == .httpStatus(500))
    }

    @Test @MainActor func runValidationSuccessRefreshesValidationRunsAndSnapshot() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"

        let api = FakeWorkbenchAPIProvider()
        let result = ValidationResultDTO(
            id: "run-001",
            status: "passed",
            exitCode: 0,
            output: "ok"
        )
        let run = ValidationRunDTO(
            id: "run-001",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "Human",
            command: ["pytest"],
            cwd: "/workspace",
            status: "passed",
            exitCode: 0,
            output: "ok",
            startedAt: "2026-06-27T06:00:00",
            completedAt: "2026-06-27T06:00:01"
        )
        let runs = ValidationRunsDTO(validationRuns: [run], taskID: "task-001", limit: 50)
        let snapshot = makeSnapshot(sessionID: "sess-001", missions: [])

        await api.setRunValidationResult(.success(result))
        await api.setValidationRunsResult(.success(runs))
        await api.setSnapshotResult(.success(snapshot))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.runValidation(
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(appState.validationRuns == [run])
        #expect(appState.snapshot == snapshot)
        #expect(appState.lastError == nil)
    }

    @Test @MainActor func runValidationWithoutSelectedSessionRecordsError() async throws {
        let appState = AppState()
        appState.validationRuns = [
            ValidationRunDTO(
                id: "run-stale",
                sessionID: "old-session",
                taskID: "task-old",
                actor: "ValidationRunner",
                command: [],
                cwd: "",
                status: "passed",
                exitCode: 0,
                output: "",
                startedAt: "2026-06-27T05:00:00",
                completedAt: "2026-06-27T05:00:01"
            )
        ]
        #expect(appState.selectedSessionID == nil)

        let api = FakeWorkbenchAPIProvider()
        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.runValidation(
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(appState.lastError != nil)
        #expect(appState.lastError == .missingSelectedSession)
        #expect(appState.validationRuns.count == 1)
    }

    @Test @MainActor func runValidationFailurePreservesOldData() async throws {
        let appState = AppState()
        appState.selectedSessionID = "sess-001"
        let staleRun = ValidationRunDTO(
            id: "run-stale",
            sessionID: "sess-001",
            taskID: "task-001",
            actor: "ValidationRunner",
            command: [],
            cwd: "",
            status: "passed",
            exitCode: 0,
            output: "",
            startedAt: "2026-06-27T05:00:00",
            completedAt: "2026-06-27T05:00:01"
        )
        appState.validationRuns = [staleRun]
        let staleSnapshot = makeSnapshot(sessionID: "sess-001", missions: [])
        appState.snapshot = staleSnapshot

        let api = FakeWorkbenchAPIProvider()
        await api.setRunValidationResult(.failure(.httpStatus(500)))

        let controller = DaemonController(appState: appState, apiProvider: api)
        await controller.runValidation(
            taskID: "task-001",
            actor: "Human",
            argv: ["pytest"],
            cwd: "/workspace"
        )

        #expect(appState.validationRuns == [staleRun])
        #expect(appState.snapshot == staleSnapshot)
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

    fileprivate func setEventsResult(_ result: Result<WorkbenchEventsDTO, APIError>) {
        eventsResult = result
    }

    fileprivate func setValidationRunsResult(_ result: Result<ValidationRunsDTO, APIError>) {
        validationRunsResult = result
    }

    fileprivate func setContextSnapshotsResult(_ result: Result<ContextSnapshotsDTO, APIError>) {
        contextSnapshotsResult = result
    }

    fileprivate func setClaimIssueResult(_ result: Result<LeaseDTO, APIError>) {
        claimIssueResult = result
    }

    fileprivate func setReleaseLeaseResult(_ result: Result<LeaseDTO, APIError>) {
        releaseLeaseResult = result
    }

    fileprivate func setExpireLeasesResult(_ result: Result<ExpiredLeasesDTO, APIError>) {
        expireLeasesResult = result
    }

    fileprivate func setCreateMissionResult(_ result: Result<MissionDTO, APIError>) {
        createMissionResult = result
    }

    fileprivate func setAttachIssueResult(_ result: Result<IssueDTO, APIError>) {
        attachIssueResult = result
    }

    fileprivate func setRunValidationResult(_ result: Result<ValidationResultDTO, APIError>) {
        runValidationResult = result
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

private func makeSnapshot(sessionID: String, lease: LeaseDTO) -> WorkbenchSnapshotDTO {
    let task = makeTask(id: lease.taskID, subject: "Sample Task", status: "open")
    let issue = makeIssue(taskID: lease.taskID)
    return WorkbenchSnapshotDTO(
        sessionID: sessionID,
        missions: [],
        tasks: [task],
        issues: [issue],
        leases: [lease],
        failures: [],
        events: []
    )
}

private func makeSnapshot(sessionID: String, missions: [MissionDTO]) -> WorkbenchSnapshotDTO {
    WorkbenchSnapshotDTO(
        sessionID: sessionID,
        missions: missions,
        tasks: [],
        issues: [],
        failures: [],
        events: []
    )
}

private func makeSnapshot(sessionID: String, issues: [IssueDTO]) -> WorkbenchSnapshotDTO {
    WorkbenchSnapshotDTO(
        sessionID: sessionID,
        missions: [],
        tasks: [],
        issues: issues,
        failures: [],
        events: []
    )
}

private func makeTask(id: String, subject: String, status: String) -> TaskDTO {
    TaskDTO(
        id: id,
        sessionID: "sess-001",
        subject: subject,
        description: "",
        status: status,
        activeForm: nil,
        owner: nil,
        blocks: [],
        blockedBy: [],
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:00"
    )
}

private func makeIssue(taskID: String, missionID: String = "mission-001") -> IssueDTO {
    IssueDTO(
        sessionID: "sess-001",
        taskID: taskID,
        missionID: missionID,
        parallelMode: "exclusive",
        riskLevel: "medium",
        requiresHumanApproval: false,
        acceptanceCriteria: [],
        expectedArtifacts: [],
        relatedBranch: "",
        relatedWorktree: "",
        relatedPR: "",
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:00"
    )
}

private func makeMission(id: String, sessionID: String) -> MissionDTO {
    MissionDTO(
        id: id,
        sessionID: sessionID,
        title: "Mac 工作台",
        goal: "补齐 API 调用面",
        status: "active",
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:00"
    )
}

private func makeLease(id: String, taskID: String, state: String) -> LeaseDTO {
    LeaseDTO(
        id: id,
        sessionID: "sess-001",
        taskID: taskID,
        agentID: "local-agent",
        state: state,
        expiresAt: "2026-06-27T08:00:00",
        worktreeName: "wt-001",
        createdAt: "2026-06-27T06:00:00",
        updatedAt: "2026-06-27T06:00:00"
    )
}
