import Foundation

/// A human-governed proposal created when direct execution is blocked by an
/// intent lock or risk threshold.
///
/// Instead of mutating state directly, an agent submits a proposal describing
/// the intended impact scope, the files it means to touch, its validation plan,
/// the risk, and open questions. A human then approves, rejects, or converts
/// the proposal into a tracked issue.
public struct ProposalDTO: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let sessionID: String
    public let missionID: String
    public let taskID: String
    public let agentID: String
    public let title: String
    public let impactScope: String
    public let intendedFiles: [String]
    public let validationPlan: [String]
    public let riskLevel: String
    public let questions: [String]
    public let state: String
    public let decisionNote: String
    public let convertedIssueID: String
    public let createdAt: String
    public let updatedAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case missionID = "mission_id"
        case taskID = "task_id"
        case agentID = "agent_id"
        case title
        case impactScope = "impact_scope"
        case intendedFiles = "intended_files"
        case validationPlan = "validation_plan"
        case riskLevel = "risk_level"
        case questions
        case state
        case decisionNote = "decision_note"
        case convertedIssueID = "converted_issue_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public init(
        id: String,
        sessionID: String,
        missionID: String,
        taskID: String,
        agentID: String,
        title: String,
        impactScope: String,
        intendedFiles: [String],
        validationPlan: [String],
        riskLevel: String,
        questions: [String],
        state: String,
        decisionNote: String,
        convertedIssueID: String,
        createdAt: String,
        updatedAt: String
    ) {
        self.id = id
        self.sessionID = sessionID
        self.missionID = missionID
        self.taskID = taskID
        self.agentID = agentID
        self.title = title
        self.impactScope = impactScope
        self.intendedFiles = intendedFiles
        self.validationPlan = validationPlan
        self.riskLevel = riskLevel
        self.questions = questions
        self.state = state
        self.decisionNote = decisionNote
        self.convertedIssueID = convertedIssueID
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        sessionID = try container.decode(String.self, forKey: .sessionID)
        missionID = try container.decode(String.self, forKey: .missionID)
        taskID = try container.decode(String.self, forKey: .taskID)
        agentID = try container.decode(String.self, forKey: .agentID)
        title = try container.decode(String.self, forKey: .title)
        impactScope = try container.decode(String.self, forKey: .impactScope)
        intendedFiles = try container.decodeIfPresent([String].self, forKey: .intendedFiles) ?? []
        validationPlan = try container.decodeIfPresent([String].self, forKey: .validationPlan) ?? []
        riskLevel = try container.decodeIfPresent(String.self, forKey: .riskLevel) ?? "medium"
        questions = try container.decodeIfPresent([String].self, forKey: .questions) ?? []
        state = try container.decodeIfPresent(String.self, forKey: .state) ?? "open"
        decisionNote = try container.decodeIfPresent(String.self, forKey: .decisionNote) ?? ""
        convertedIssueID = try container.decodeIfPresent(String.self, forKey: .convertedIssueID) ?? ""
        createdAt = try container.decode(String.self, forKey: .createdAt)
        updatedAt = try container.decode(String.self, forKey: .updatedAt)
    }
}

/// Response envelope for `GET /workbench/sessions/{id}/proposals`.
public struct ProposalsDTO: Decodable, Equatable, Sendable {
    public let proposals: [ProposalDTO]
    public let missionID: String?
    public let taskID: String?
    public let state: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case proposals
        case missionID = "mission_id"
        case taskID = "task_id"
        case state
        case limit
    }

    public init(
        proposals: [ProposalDTO],
        missionID: String?,
        taskID: String?,
        state: String?,
        limit: Int
    ) {
        self.proposals = proposals
        self.missionID = missionID
        self.taskID = taskID
        self.state = state
        self.limit = limit
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        proposals = try container.decode([ProposalDTO].self, forKey: .proposals)
        missionID = try container.decodeIfPresent(String.self, forKey: .missionID)
        taskID = try container.decodeIfPresent(String.self, forKey: .taskID)
        state = try container.decodeIfPresent(String.self, forKey: .state)
        limit = try container.decode(Int.self, forKey: .limit)
    }
}

/// Request body for `POST /workbench/sessions/{id}/proposals`.
public struct ProposalDraft: Encodable, Equatable, Sendable {
    public let missionID: String
    public let taskID: String
    public let agentID: String
    public let title: String
    public let impactScope: String
    public let intendedFiles: [String]
    public let validationPlan: [String]
    public let riskLevel: String
    public let questions: [String]

    public enum CodingKeys: String, CodingKey {
        case missionID = "mission_id"
        case taskID = "task_id"
        case agentID = "agent_id"
        case title
        case impactScope = "impact_scope"
        case intendedFiles = "intended_files"
        case validationPlan = "validation_plan"
        case riskLevel = "risk_level"
        case questions
    }

    public init(
        missionID: String,
        taskID: String,
        agentID: String,
        title: String,
        impactScope: String,
        intendedFiles: [String],
        validationPlan: [String],
        riskLevel: String,
        questions: [String]
    ) {
        self.missionID = missionID
        self.taskID = taskID
        self.agentID = agentID
        self.title = title
        self.impactScope = impactScope
        self.intendedFiles = intendedFiles
        self.validationPlan = validationPlan
        self.riskLevel = riskLevel
        self.questions = questions
    }
}

/// Request body for approve/reject/convert proposal actions.
public struct ProposalResolveDraft: Encodable, Equatable, Sendable {
    public let reviewer: String
    public let decisionNote: String

    public enum CodingKeys: String, CodingKey {
        case reviewer
        case decisionNote = "decision_note"
    }

    public init(reviewer: String, decisionNote: String) {
        self.reviewer = reviewer
        self.decisionNote = decisionNote
    }
}
