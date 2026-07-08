import Foundation

/// Real review evidence for an approval, returned by
/// `GET /workbench/sessions/{id}/approvals/{approval_id}/evidence`.
///
/// Replaces the fixture diff/file/timeline data the Reviews page used to show.
/// Every field is populated from live backend data (store + local git worktree);
/// empty arrays mean "no real data", never fabricated rows.
public struct ReviewEvidenceDTO: Decodable, Equatable, Sendable {
    public let approval: EvidenceApprovalDTO
    public let issue: EvidenceIssueDTO?
    public let worktree: EvidenceWorktreeDTO
    public let validationRuns: [ValidationRunDTO]
    public let changedFiles: [ReviewChangedFileDTO]
    public let diffHunks: [ReviewDiffHunkDTO]
    public let agentNotes: [ReviewAgentNoteDTO]
    public let events: [EventDTO]

    public enum CodingKeys: String, CodingKey {
        case approval
        case issue
        case worktree
        case validationRuns = "validation_runs"
        case changedFiles = "changed_files"
        case diffHunks = "diff_hunks"
        case agentNotes = "agent_notes"
        case events
    }

    public init(
        approval: EvidenceApprovalDTO,
        issue: EvidenceIssueDTO?,
        worktree: EvidenceWorktreeDTO,
        validationRuns: [ValidationRunDTO],
        changedFiles: [ReviewChangedFileDTO],
        diffHunks: [ReviewDiffHunkDTO],
        agentNotes: [ReviewAgentNoteDTO],
        events: [EventDTO]
    ) {
        self.approval = approval
        self.issue = issue
        self.worktree = worktree
        self.validationRuns = validationRuns
        self.changedFiles = changedFiles
        self.diffHunks = diffHunks
        self.agentNotes = agentNotes
        self.events = events
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        approval = try container.decode(EvidenceApprovalDTO.self, forKey: .approval)
        issue = try container.decodeIfPresent(EvidenceIssueDTO.self, forKey: .issue)
        worktree = try container.decodeIfPresent(EvidenceWorktreeDTO.self, forKey: .worktree)
            ?? EvidenceWorktreeDTO(name: "", path: "", status: "unbound")
        validationRuns = try container.decodeIfPresent([ValidationRunDTO].self, forKey: .validationRuns) ?? []
        changedFiles = try container.decodeIfPresent([ReviewChangedFileDTO].self, forKey: .changedFiles) ?? []
        diffHunks = try container.decodeIfPresent([ReviewDiffHunkDTO].self, forKey: .diffHunks) ?? []
        agentNotes = try container.decodeIfPresent([ReviewAgentNoteDTO].self, forKey: .agentNotes) ?? []
        events = try container.decodeIfPresent([EventDTO].self, forKey: .events) ?? []
    }
}

public struct EvidenceApprovalDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let sessionID: String
    public let missionID: String
    public let taskID: String
    public let state: String
    public let title: String
    public let detail: String
    public let requester: String
    public let reviewer: String
    public let decisionNote: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case missionID = "mission_id"
        case taskID = "task_id"
        case state
        case title
        case detail
        case requester
        case reviewer
        case decisionNote = "decision_note"
    }

    public init(
        id: String,
        sessionID: String,
        missionID: String,
        taskID: String,
        state: String,
        title: String,
        detail: String,
        requester: String,
        reviewer: String,
        decisionNote: String
    ) {
        self.id = id
        self.sessionID = sessionID
        self.missionID = missionID
        self.taskID = taskID
        self.state = state
        self.title = title
        self.detail = detail
        self.requester = requester
        self.reviewer = reviewer
        self.decisionNote = decisionNote
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        sessionID = try container.decodeIfPresent(String.self, forKey: .sessionID) ?? ""
        missionID = try container.decodeIfPresent(String.self, forKey: .missionID) ?? ""
        taskID = try container.decode(String.self, forKey: .taskID)
        state = try container.decode(String.self, forKey: .state)
        title = try container.decodeIfPresent(String.self, forKey: .title) ?? ""
        detail = try container.decodeIfPresent(String.self, forKey: .detail) ?? ""
        requester = try container.decodeIfPresent(String.self, forKey: .requester) ?? ""
        reviewer = try container.decodeIfPresent(String.self, forKey: .reviewer) ?? ""
        decisionNote = try container.decodeIfPresent(String.self, forKey: .decisionNote) ?? ""
    }
}

public struct EvidenceIssueDTO: Decodable, Equatable, Sendable {
    public let sessionID: String
    public let taskID: String
    public let missionID: String
    public let parallelMode: String
    public let riskLevel: String
    public let requiresHumanApproval: Bool
    public let relatedWorktree: String

    public enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case taskID = "task_id"
        case missionID = "mission_id"
        case parallelMode = "parallel_mode"
        case riskLevel = "risk_level"
        case requiresHumanApproval = "requires_human_approval"
        case relatedWorktree = "related_worktree"
    }

    public init(
        sessionID: String,
        taskID: String,
        missionID: String,
        parallelMode: String,
        riskLevel: String,
        requiresHumanApproval: Bool,
        relatedWorktree: String
    ) {
        self.sessionID = sessionID
        self.taskID = taskID
        self.missionID = missionID
        self.parallelMode = parallelMode
        self.riskLevel = riskLevel
        self.requiresHumanApproval = requiresHumanApproval
        self.relatedWorktree = relatedWorktree
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sessionID = try container.decodeIfPresent(String.self, forKey: .sessionID) ?? ""
        taskID = try container.decodeIfPresent(String.self, forKey: .taskID) ?? ""
        missionID = try container.decodeIfPresent(String.self, forKey: .missionID) ?? ""
        parallelMode = try container.decodeIfPresent(String.self, forKey: .parallelMode) ?? ""
        riskLevel = try container.decodeIfPresent(String.self, forKey: .riskLevel) ?? ""
        requiresHumanApproval = try container.decodeIfPresent(Bool.self, forKey: .requiresHumanApproval) ?? false
        relatedWorktree = try container.decodeIfPresent(String.self, forKey: .relatedWorktree) ?? ""
    }
}

public struct EvidenceWorktreeDTO: Decodable, Equatable, Sendable {
    public let name: String
    public let path: String
    /// `present`, `missing`, or `unbound` (no worktree bound to the issue).
    public let status: String

    public init(name: String, path: String, status: String) {
        self.name = name
        self.path = path
        self.status = status
    }
}

public struct ReviewChangedFileDTO: Decodable, Equatable, Sendable, Identifiable {
    public var id: String { path }
    public let path: String
    public let status: String

    public enum CodingKeys: String, CodingKey {
        case path
        case status
    }

    public init(path: String, status: String) {
        self.path = path
        self.status = status
    }
}

public struct ReviewDiffHunkDTO: Decodable, Equatable, Sendable, Identifiable {
    public var id: String { path }
    public let path: String
    public let patch: String

    public enum CodingKeys: String, CodingKey {
        case path
        case patch
    }

    public init(path: String, patch: String) {
        self.path = path
        self.patch = patch
    }
}

public struct ReviewAgentNoteDTO: Decodable, Equatable, Sendable, Identifiable {
    public var id: String { "\(actor)-\(type)-\(timestamp)" }
    public let actor: String
    public let note: String
    public let type: String
    public let timestamp: String

    public enum CodingKeys: String, CodingKey {
        case actor
        case note
        case type
        case timestamp
    }

    public init(actor: String, note: String, type: String, timestamp: String) {
        self.actor = actor
        self.note = note
        self.type = type
        self.timestamp = timestamp
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        actor = try container.decodeIfPresent(String.self, forKey: .actor) ?? ""
        note = try container.decodeIfPresent(String.self, forKey: .note) ?? ""
        type = try container.decodeIfPresent(String.self, forKey: .type) ?? ""
        timestamp = try container.decodeIfPresent(String.self, forKey: .timestamp) ?? ""
    }
}
