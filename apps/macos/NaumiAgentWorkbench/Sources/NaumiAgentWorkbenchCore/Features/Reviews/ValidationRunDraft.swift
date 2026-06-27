import Foundation

/// User input for launching a backend validation run from the Reviews page.
public struct ValidationRunDraft: Equatable {
    public var taskID: String
    public var actor: String
    public var commandLine: String
    public var cwd: String

    public init(
        taskID: String = "",
        actor: String = "ValidationRunner",
        commandLine: String = "",
        cwd: String = ""
    ) {
        self.taskID = taskID
        self.actor = actor
        self.commandLine = commandLine
        self.cwd = cwd
    }

    public var trimmedTaskID: String {
        taskID.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var trimmedActor: String {
        actor.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var argv: [String] {
        commandLine
            .split(whereSeparator: \.isWhitespace)
            .map(String.init)
    }

    public var normalizedCWD: String? {
        let trimmed = cwd.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    public var canSubmit: Bool {
        !trimmedTaskID.isEmpty && !trimmedActor.isEmpty && !argv.isEmpty
    }
}
