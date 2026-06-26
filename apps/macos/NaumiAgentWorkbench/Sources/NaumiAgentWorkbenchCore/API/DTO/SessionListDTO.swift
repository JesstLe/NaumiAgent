import Foundation

/// Paginated session list returned by `GET /sessions`.
///
/// Mirrors `SessionListResponse` in `src/naumi_agent/api/schemas.py`.
public struct SessionListDTO: Decodable, Equatable, Sendable {
    public let sessions: [SessionDTO]
    public let total: Int
    public let page: Int
    public let pageSize: Int

    public enum CodingKeys: String, CodingKey {
        case sessions
        case total
        case page
        case pageSize = "page_size"
    }
}
