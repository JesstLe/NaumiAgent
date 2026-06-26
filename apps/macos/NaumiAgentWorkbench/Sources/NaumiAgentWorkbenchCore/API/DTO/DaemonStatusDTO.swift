import Foundation

/// Response from `GET /workbench/daemon/status`.
public struct DaemonStatusDTO: Decodable, Equatable, Sendable {
    public let status: String
    public let version: String
    public let pid: Int
    public let host: String
    public let port: Int
    public let startedAt: String
    public let workspaceCount: Int

    public enum CodingKeys: String, CodingKey {
        case status
        case version
        case pid
        case host
        case port
        case startedAt = "started_at"
        case workspaceCount = "workspace_count"
    }
}
