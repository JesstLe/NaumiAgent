import Foundation

/// One known workspace and the sessions recently used within it.
public struct WorkspaceEntry: Codable, Equatable, Sendable, Identifiable {
    public var root: String
    public var name: String
    public var recentSessionIDs: [String]
    public var lastSessionID: String?
    public var lastEndpoint: String?
    public var protocolVersion: Int?

    public var id: String { root }

    public init(
        root: String,
        name: String = "",
        recentSessionIDs: [String] = [],
        lastSessionID: String? = nil,
        lastEndpoint: String? = nil,
        protocolVersion: Int? = nil
    ) {
        self.root = root
        self.name = name
        self.recentSessionIDs = recentSessionIDs
        self.lastSessionID = lastSessionID
        self.lastEndpoint = lastEndpoint
        self.protocolVersion = protocolVersion
    }

    private enum CodingKeys: String, CodingKey {
        case root, name, recentSessionIDs, lastSessionID, lastEndpoint, protocolVersion
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.root = try c.decodeIfPresent(String.self, forKey: .root) ?? ""
        self.name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        self.recentSessionIDs = try c.decodeIfPresent([String].self, forKey: .recentSessionIDs) ?? []
        self.lastSessionID = try c.decodeIfPresent(String.self, forKey: .lastSessionID)
        self.lastEndpoint = try c.decodeIfPresent(String.self, forKey: .lastEndpoint)
        self.protocolVersion = try c.decodeIfPresent(Int.self, forKey: .protocolVersion)
    }
}

/// Persisted registry of known workspaces and their recent sessions.
public struct WorkspaceRegistry: Codable, Equatable, Sendable {
    public static let recentSessionLimit: Int = 12

    public var entries: [WorkspaceEntry]
    public var selectedRoot: String?

    public init(entries: [WorkspaceEntry] = [], selectedRoot: String? = nil) {
        self.entries = entries
        self.selectedRoot = selectedRoot
    }

    public static let empty = WorkspaceRegistry()

    public var selectedEntry: WorkspaceEntry? {
        guard let selectedRoot else { return nil }
        return entries.first { $0.root == selectedRoot }
    }

    /// Returns the entry for a root, if present.
    public func entry(forRoot root: String) -> WorkspaceEntry? {
        entries.first { $0.root == root }
    }

    /// Returns a copy with the workspace upserted from daemon status, marking
    /// it selected. Recent sessions are preserved.
    public func upserting(
        root: String,
        name: String,
        lastEndpoint: String?,
        protocolVersion: Int?
    ) -> WorkspaceRegistry {
        var updated = entries.filter { $0.root != root }
        let prior = entries.first { $0.root == root }
        let entry = WorkspaceEntry(
            root: root,
            name: name.isEmpty ? (prior?.name ?? name) : name,
            recentSessionIDs: prior?.recentSessionIDs ?? [],
            lastSessionID: prior?.lastSessionID,
            lastEndpoint: lastEndpoint ?? prior?.lastEndpoint,
            protocolVersion: protocolVersion ?? prior?.protocolVersion
        )
        updated.insert(entry, at: 0)
        return WorkspaceRegistry(entries: updated, selectedRoot: root)
    }

    /// Records a session as the most-recently-used for the selected workspace,
    /// moving it to the front and capping the list.
    public func recordingSession(_ sessionID: String) -> WorkspaceRegistry {
        guard let selectedRoot,
              let index = entries.firstIndex(where: { $0.root == selectedRoot }) else {
            return self
        }
        var entries = self.entries
        var entry = entries[index]
        entry.recentSessionIDs = [sessionID] + entry.recentSessionIDs.filter { $0 != sessionID }
        if entry.recentSessionIDs.count > Self.recentSessionLimit {
            entry.recentSessionIDs = Array(entry.recentSessionIDs.prefix(Self.recentSessionLimit))
        }
        entry.lastSessionID = sessionID
        entries[index] = entry
        return WorkspaceRegistry(entries: entries, selectedRoot: selectedRoot)
    }

    /// Selects a workspace root when it is known to the registry.
    public func selecting(root: String) -> WorkspaceRegistry {
        guard entries.contains(where: { $0.root == root }) else { return self }
        return WorkspaceRegistry(entries: entries, selectedRoot: root)
    }
}

/// File-backed persistence for `WorkspaceRegistry`.
public final class WorkspaceRegistryStore: @unchecked Sendable {
    public let url: URL
    private let fileManager: FileManager

    public init(url: URL, fileManager: FileManager = .default) {
        self.url = url
        self.fileManager = fileManager
    }

    public func load() -> WorkspaceRegistry {
        guard let data = try? Data(contentsOf: url) else {
            return .empty
        }
        guard let decoded = try? JSONDecoder().decode(WorkspaceRegistry.self, from: data) else {
            return .empty
        }
        return decoded
    }

    public func save(_ registry: WorkspaceRegistry) throws {
        let directory = url.deletingLastPathComponent()
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(registry)
        try data.write(to: url, options: [.atomic])
    }

    public static var defaultStoreURL: URL {
        let appSupport = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return appSupport
            .appendingPathComponent("NaumiAgentWorkbench", isDirectory: true)
            .appendingPathComponent("workspace-registry.json", isDirectory: false)
    }

    public static var `default`: WorkspaceRegistryStore {
        WorkspaceRegistryStore(url: defaultStoreURL)
    }
}
