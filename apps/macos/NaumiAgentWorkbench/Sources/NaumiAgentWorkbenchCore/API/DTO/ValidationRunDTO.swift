import Foundation

/// Single validation run returned by `GET /workbench/sessions/{id}/validation-runs`.
public struct ValidationRunDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let taskID: String
    public let actor: String
    public let command: [String]
    public let cwd: String
    public let status: String
    public let exitCode: Int
    public let output: String
    public let startedAt: String
    public let completedAt: String
    public let task: TaskDTO?

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
        case startedAt = "started_at"
        case completedAt = "completed_at"
        case task
    }

    public init(
        id: String,
        sessionID: String,
        taskID: String,
        actor: String,
        command: [String],
        cwd: String,
        status: String,
        exitCode: Int,
        output: String,
        startedAt: String,
        completedAt: String,
        task: TaskDTO? = nil
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
        self.startedAt = startedAt
        self.completedAt = completedAt
        self.task = task
    }
}
