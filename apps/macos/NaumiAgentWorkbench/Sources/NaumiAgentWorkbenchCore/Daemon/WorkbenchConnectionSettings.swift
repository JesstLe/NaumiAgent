import Foundation

/// Persisted connection configuration for the local NaumiAgent daemon.
///
/// Local-first boundary: the default endpoint is always `127.0.0.1`. The token
/// is stored alongside the endpoint in the user's Application Support directory.
public struct WorkbenchConnectionSettings: Codable, Equatable, Sendable {
    public var baseURLString: String
    public var bearerToken: String?

    public init(baseURLString: String, bearerToken: String? = nil) {
        self.baseURLString = baseURLString
        self.bearerToken = bearerToken
    }

    /// Parsed base URL with a guaranteed trailing slash. `nil` when the stored
    /// string is not a valid URL.
    public var baseURL: URL? {
        let trimmed = baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let normalized = trimmed.hasSuffix("/") ? trimmed : trimmed + "/"
        return URL(string: normalized)
    }

    /// Resolved bearer token, treating whitespace-only values as absent.
    public var resolvedBearerToken: String? {
        guard let bearerToken,
              !bearerToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }
        return bearerToken
    }

    public static let defaultBaseURLString = "http://127.0.0.1:8765/api/v1/"

    public static let `default` = WorkbenchConnectionSettings(
        baseURLString: defaultBaseURLString,
        bearerToken: nil
    )

    private enum CodingKeys: String, CodingKey {
        case baseURLString
        case bearerToken
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.baseURLString = try container.decodeIfPresent(String.self, forKey: .baseURLString)
            ?? WorkbenchConnectionSettings.defaultBaseURLString
        let rawToken = try container.decodeIfPresent(String.self, forKey: .bearerToken)
        self.bearerToken = (rawToken?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false)
            ? rawToken
            : nil
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(baseURLString, forKey: .baseURLString)
        try container.encodeIfPresent(resolvedBearerToken, forKey: .bearerToken)
    }
}

/// File-backed persistence for `WorkbenchConnectionSettings`.
///
/// The default store location is `~/Library/Application Support/NaumiAgentWorkbench/connection.json`.
/// Tests inject a custom URL to exercise round-tripping without touching user data.
public final class WorkbenchConnectionSettingsStore: @unchecked Sendable {
    public let url: URL
    private let fileManager: FileManager

    public init(url: URL, fileManager: FileManager = .default) {
        self.url = url
        self.fileManager = fileManager
    }

    /// Loads persisted settings, falling back to `.default` when the file is
    /// missing, unreadable, or corrupt.
    public func load() -> WorkbenchConnectionSettings {
        guard let data = try? Data(contentsOf: url) else {
            return .default
        }
        let decoder = JSONDecoder()
        guard let decoded = try? decoder.decode(WorkbenchConnectionSettings.self, from: data) else {
            return .default
        }
        return decoded
    }

    /// Persists settings, creating the parent directory if necessary.
    public func save(_ settings: WorkbenchConnectionSettings) throws {
        let directory = url.deletingLastPathComponent()
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(settings)
        try data.write(to: url, options: [.atomic])
    }

    /// Default store under the user's Application Support directory.
    public static var defaultStoreURL: URL {
        let appSupport = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return appSupport
            .appendingPathComponent("NaumiAgentWorkbench", isDirectory: true)
            .appendingPathComponent("connection.json", isDirectory: false)
    }

    /// Convenience store backed by `defaultStoreURL`.
    public static var `default`: WorkbenchConnectionSettingsStore {
        WorkbenchConnectionSettingsStore(url: defaultStoreURL)
    }
}
