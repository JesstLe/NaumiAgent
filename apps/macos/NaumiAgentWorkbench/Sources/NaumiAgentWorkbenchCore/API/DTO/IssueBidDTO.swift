import Foundation

/// A single agent bid to claim an issue (task), persisted in `workbench_bids`.
///
/// Bids express confidence, an effort estimate, an ETA, and a free-form note.
/// They are independent of leases: the market shows competing bids before a
/// lease is granted.
public struct IssueBidDTO: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let sessionID: String
    public let taskID: String
    public let agentID: String
    public let confidence: Double
    public let estimateMinutes: Int
    public let eta: String
    public let note: String
    public let createdAt: String
    public let updatedAt: String

    public enum CodingKeys: String, CodingKey {
        case id
        case sessionID = "session_id"
        case taskID = "task_id"
        case agentID = "agent_id"
        case confidence
        case estimateMinutes = "estimate_minutes"
        case eta
        case note
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public init(
        id: String,
        sessionID: String,
        taskID: String,
        agentID: String,
        confidence: Double,
        estimateMinutes: Int,
        eta: String,
        note: String,
        createdAt: String,
        updatedAt: String
    ) {
        self.id = id
        self.sessionID = sessionID
        self.taskID = taskID
        self.agentID = agentID
        self.confidence = confidence
        self.estimateMinutes = estimateMinutes
        self.eta = eta
        self.note = note
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }
}

/// Response envelope for `GET /workbench/sessions/{id}/issues/{task_id}/bids`.
public struct IssueBidsDTO: Decodable, Equatable, Sendable {
    public let bids: [IssueBidDTO]
    public let taskID: String?
    public let agentID: String?
    public let limit: Int

    public enum CodingKeys: String, CodingKey {
        case bids
        case taskID = "task_id"
        case agentID = "agent_id"
        case limit
    }

    public init(bids: [IssueBidDTO], taskID: String?, agentID: String?, limit: Int) {
        self.bids = bids
        self.taskID = taskID
        self.agentID = agentID
        self.limit = limit
    }
}

/// Request body for `POST /workbench/sessions/{id}/issues/{task_id}/bids`.
public struct IssueBidDraft: Encodable, Equatable, Sendable {
    public let agentID: String
    public let confidence: Double
    public let estimateMinutes: Int
    public let eta: String
    public let note: String

    public enum CodingKeys: String, CodingKey {
        case agentID = "agent_id"
        case confidence
        case estimateMinutes = "estimate_minutes"
        case eta
        case note
    }

    public init(agentID: String, confidence: Double, estimateMinutes: Int, eta: String, note: String) {
        self.agentID = agentID
        self.confidence = confidence
        self.estimateMinutes = estimateMinutes
        self.eta = eta
        self.note = note
    }
}
