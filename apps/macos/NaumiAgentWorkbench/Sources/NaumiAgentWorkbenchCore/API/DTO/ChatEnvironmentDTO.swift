import Foundation

public struct ChatGitEnvironmentDTO: Decodable, Equatable, Sendable {
    public let available: Bool
    public let branch: String
    public let changedFiles: Int
    public let additions: Int
    public let deletions: Int
    public let ahead: Int
    public let behind: Int
    public let dirty: Bool

    public enum CodingKeys: String, CodingKey {
        case available, branch, additions, deletions, ahead, behind, dirty
        case changedFiles = "changed_files"
    }

    public init(
        available: Bool,
        branch: String,
        changedFiles: Int,
        additions: Int,
        deletions: Int,
        ahead: Int,
        behind: Int,
        dirty: Bool
    ) {
        self.available = available
        self.branch = branch
        self.changedFiles = changedFiles
        self.additions = additions
        self.deletions = deletions
        self.ahead = ahead
        self.behind = behind
        self.dirty = dirty
    }
}

public struct ChatBackgroundProcessDTO: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let command: String
    public let pid: Int?
    public let status: String
    public let startedAt: String
    public let cwd: String

    public enum CodingKeys: String, CodingKey {
        case id, command, pid, status, cwd
        case startedAt = "started_at"
    }

    public init(
        id: String,
        command: String,
        pid: Int?,
        status: String,
        startedAt: String,
        cwd: String
    ) {
        self.id = id
        self.command = command
        self.pid = pid
        self.status = status
        self.startedAt = startedAt
        self.cwd = cwd
    }
}

public struct ChatSourceReferenceDTO: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let kind: String
    public let title: String
    public let path: String
    public let runID: String
    public let createdAt: String

    public enum CodingKeys: String, CodingKey {
        case id, kind, title, path
        case runID = "run_id"
        case createdAt = "created_at"
    }

    public init(
        id: String,
        kind: String,
        title: String,
        path: String,
        runID: String,
        createdAt: String
    ) {
        self.id = id
        self.kind = kind
        self.title = title
        self.path = path
        self.runID = runID
        self.createdAt = createdAt
    }
}

public struct ChatEnvironmentDTO: Decodable, Equatable, Sendable {
    public let sessionID: String
    public let workspaceRoot: String
    public let workspaceName: String
    public let git: ChatGitEnvironmentDTO
    public let processes: [ChatBackgroundProcessDTO]
    public let sources: [ChatSourceReferenceDTO]

    public enum CodingKeys: String, CodingKey {
        case git, processes, sources
        case sessionID = "session_id"
        case workspaceRoot = "workspace_root"
        case workspaceName = "workspace_name"
    }

    public init(
        sessionID: String,
        workspaceRoot: String,
        workspaceName: String,
        git: ChatGitEnvironmentDTO,
        processes: [ChatBackgroundProcessDTO] = [],
        sources: [ChatSourceReferenceDTO] = []
    ) {
        self.sessionID = sessionID
        self.workspaceRoot = workspaceRoot
        self.workspaceName = workspaceName
        self.git = git
        self.processes = processes
        self.sources = sources
    }
}

public protocol ChatEnvironmentProviding: Sendable {
    func fetchChatEnvironment(sessionID: String) async throws(APIError) -> ChatEnvironmentDTO
}

public protocol ChatSourceProviding: Sendable {
    func addChatSource(
        sessionID: String,
        path: String,
        kind: String,
        title: String
    ) async throws(APIError) -> ChatSourceReferenceDTO
}
