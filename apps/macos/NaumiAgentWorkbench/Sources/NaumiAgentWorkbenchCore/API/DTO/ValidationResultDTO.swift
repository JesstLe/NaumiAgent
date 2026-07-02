import Foundation

/// Result of `POST /workbench/sessions/{id}/validation-runs`.
public struct ValidationResultDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let taskID: String
    public let actor: String
    public let command: [String]
    public let cwd: String
    public let status: String
    public let exitCode: Int
    public let output: String
    public let task: TaskDTO?
    public let startedAt: String
    public let completedAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case taskID = "task_id"
        case actor
        case command
        case cwd
        case status
        case exitCode = "exit_code"
        case output
        case task
        case startedAt = "started_at"
        case completedAt = "completed_at"
    }

    public init(
        id: String,
        sessionID: String = "",
        taskID: String = "",
        actor: String = "",
        command: [String] = [],
        cwd: String = "",
        status: String,
        exitCode: Int,
        output: String,
        task: TaskDTO? = nil,
        startedAt: String = "",
        completedAt: String = ""
    ) {
        self.id = id
        self.sessionID = sessionID
        self.taskID = taskID
        self.actor = actor
        self.command = command
        self.cwd = cwd
        self.status = status
        self.exitCode = exitCode
        self.output = output
        self.task = task
        self.startedAt = startedAt
        self.completedAt = completedAt
    }
}

/// Response returned when a validation run asks the backend for a fresh snapshot.
public struct ValidationResultSnapshotDTO: Decodable, Equatable, Sendable {
    public let validationRun: ValidationResultDTO
    public let snapshot: WorkbenchSnapshotDTO

    public enum CodingKeys: String, CodingKey {
        case validationRun = "validation_run"
        case snapshot
    }

    public init(validationRun: ValidationResultDTO, snapshot: WorkbenchSnapshotDTO) {
        self.validationRun = validationRun
        self.snapshot = snapshot
    }
}
