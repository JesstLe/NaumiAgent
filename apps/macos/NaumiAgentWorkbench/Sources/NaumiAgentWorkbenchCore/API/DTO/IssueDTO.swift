import Foundation

/// Issue metadata entity returned in workbench snapshots.
public struct IssueDTO: Decodable, Equatable, Sendable {
    public let sessionID: String
    public let taskID: String
    public let missionID: String
    public let parallelMode: String
    public let riskLevel: String
    public let requiresHumanApproval: Bool
    public let acceptanceCriteria: [String]
    public let expectedArtifacts: [String]
    public let relatedBranch: String
    public let relatedWorktree: String
    public let relatedPR: String
    public let createdAt: String
    public let updatedAt: String

    public enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case taskID = "task_id"
        case missionID = "mission_id"
        case parallelMode = "parallel_mode"
        case riskLevel = "risk_level"
        case requiresHumanApproval = "requires_human_approval"
        case acceptanceCriteria = "acceptance_criteria"
        case expectedArtifacts = "expected_artifacts"
        case relatedBranch = "related_branch"
        case relatedWorktree = "related_worktree"
        case relatedPR = "related_pr"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}
