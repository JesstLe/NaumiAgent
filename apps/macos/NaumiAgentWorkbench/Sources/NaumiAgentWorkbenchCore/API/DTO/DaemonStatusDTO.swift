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
    public let workspaceRoot: String
    public let workspaceName: String
    public let apiBaseURL: String
    public let workbenchBaseURL: String
    public let eventStreamURLTemplate: String
    public let authMode: String

    public enum CodingKeys: String, CodingKey {
        case status
        case version
        case pid
        case host
        case port
        case startedAt = "started_at"
        case workspaceCount = "workspace_count"
        case workspaceRoot = "workspace_root"
        case workspaceName = "workspace_name"
        case apiBaseURL = "api_base_url"
        case workbenchBaseURL = "workbench_base_url"
        case eventStreamURLTemplate = "event_stream_url_template"
        case authMode = "auth_mode"
    }

    public init(
        status: String,
        version: String,
        pid: Int,
        host: String,
        port: Int,
        startedAt: String,
        workspaceCount: Int,
        workspaceRoot: String = "",
        workspaceName: String = "",
        apiBaseURL: String = "",
        workbenchBaseURL: String = "",
        eventStreamURLTemplate: String = "",
        authMode: String = "unknown"
    ) {
        self.status = status
        self.version = version
        self.pid = pid
        self.host = host
        self.port = port
        self.startedAt = startedAt
        self.workspaceCount = workspaceCount
        self.workspaceRoot = workspaceRoot
        self.workspaceName = workspaceName
        let fallbackAPIBaseURL = "http://\(host):\(port)/api/v1"
        self.apiBaseURL = apiBaseURL.isEmpty ? fallbackAPIBaseURL : apiBaseURL
        self.workbenchBaseURL = workbenchBaseURL.isEmpty
            ? "\(fallbackAPIBaseURL)/workbench"
            : workbenchBaseURL
        self.eventStreamURLTemplate = eventStreamURLTemplate.isEmpty
            ? "ws://\(host):\(port)/api/v1/workbench/sessions/{session_id}/events/stream"
            : eventStreamURLTemplate
        self.authMode = authMode.isEmpty ? "unknown" : authMode
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let host = try container.decode(String.self, forKey: .host)
        let port = try container.decode(Int.self, forKey: .port)
        self.init(
            status: try container.decode(String.self, forKey: .status),
            version: try container.decode(String.self, forKey: .version),
            pid: try container.decode(Int.self, forKey: .pid),
            host: host,
            port: port,
            startedAt: try container.decode(String.self, forKey: .startedAt),
            workspaceCount: try container.decode(Int.self, forKey: .workspaceCount),
            workspaceRoot: try container.decodeIfPresent(String.self, forKey: .workspaceRoot) ?? "",
            workspaceName: try container.decodeIfPresent(String.self, forKey: .workspaceName) ?? "",
            apiBaseURL: try container.decodeIfPresent(String.self, forKey: .apiBaseURL) ?? "",
            workbenchBaseURL: try container.decodeIfPresent(String.self, forKey: .workbenchBaseURL) ?? "",
            eventStreamURLTemplate: try container.decodeIfPresent(
                String.self,
                forKey: .eventStreamURLTemplate
            ) ?? "",
            authMode: try container.decodeIfPresent(String.self, forKey: .authMode) ?? "unknown"
        )
    }
}
