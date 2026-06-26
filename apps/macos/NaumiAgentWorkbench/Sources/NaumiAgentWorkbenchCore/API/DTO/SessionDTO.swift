import Foundation

/// Single session entry returned by `GET /sessions`.
///
/// Mirrors `SessionResponse` in `src/naumi_agent/api/schemas.py`.
public struct SessionDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let title: String?
    public let model: String
    public let createdAt: String
    public let updatedAt: String
    public let messageCount: Int
    public let totalTokens: Int
    public let totalCostUSD: Double
    public let status: String

    public enum CodingKeys: String, CodingKey {
        case id
        case title
        case model
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case messageCount = "message_count"
        case totalTokens = "total_tokens"
        case totalCostUSD = "total_cost_usd"
        case status
    }
}
