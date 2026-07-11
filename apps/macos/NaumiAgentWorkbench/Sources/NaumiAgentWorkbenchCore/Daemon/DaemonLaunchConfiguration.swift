import Foundation

/// Configuration for launching a local NaumiAgent daemon process.
///
/// The app does not bundle a Python runtime; it supervises an existing
/// `naumi` binary found on the user's machine. All network surfaces stay
/// on the loopback interface.
public struct DaemonLaunchConfiguration: Equatable, Sendable {
    /// Optional explicit path to the `naumi-agent` binary. When `nil`, the
    /// command resolver searches `PATH`.
    public var executablePath: String?
    public var host: String
    public var portRange: ClosedRange<Int>
    /// Last-used port, persisted so the app prefers the same port next time.
    public var preferredPort: Int?
    /// Extra CLI arguments appended after `serve --host <h> --port <p>`.
    public var extraArgs: [String]

    public init(
        executablePath: String? = nil,
        host: String = "127.0.0.1",
        portRange: ClosedRange<Int> = 8765...8799,
        preferredPort: Int? = 8765,
        extraArgs: [String] = []
    ) {
        self.executablePath = executablePath
        self.host = host
        self.portRange = portRange
        self.preferredPort = preferredPort
        self.extraArgs = extraArgs
    }

    public static let `default` = DaemonLaunchConfiguration()

    /// Builds the argv for the daemon subcommand on a chosen port.
    public func launchArguments(forPort port: Int) -> [String] {
        ["serve", "--host", host, "--port", String(port)] + extraArgs
    }

    /// HTTP base URL the daemon is expected to serve on for the given port.
    public func endpointURL(forPort port: Int) -> URL {
        URL(string: "http://\(host):\(port)/api/v1/")!
    }
}

/// Resolves the `naumi` executable path. Abstracted so tests can supply a
/// deterministic binary location without touching the real `PATH`.
public protocol DaemonCommandResolving: Sendable {
    /// Returns an absolute path to the `naumi` binary, or `nil` when it
    /// cannot be found. `hint` overrides the search when it points to an
    /// existing executable.
    func resolve(executablePath hint: String?) -> String
}

/// File-system backed resolver: honours an explicit hint, then searches `PATH`.
public struct DaemonCommandResolver: DaemonCommandResolving {
    public init() {}

    public func resolve(executablePath hint: String?) -> String {
        let fileManager = FileManager.default
        if let hint, !hint.isEmpty, fileManager.isExecutableFile(atPath: hint) {
            return hint
        }
        if let pathResolved = searchPath(fileManager: fileManager) {
            return pathResolved
        }
        // Fall back to the bare command name so a later exec reports a clear
        // error instead of hiding that resolution failed.
        return hint?.isEmpty == false ? hint! : "naumi"
    }

    private func searchPath(fileManager: FileManager) -> String? {
        guard let path = ProcessInfo.processInfo.environment["PATH"] else { return nil }
        for directory in path.split(separator: ":", omittingEmptySubsequences: true) {
            let candidate = URL(fileURLWithPath: String(directory))
                .appendingPathComponent("naumi").path
            if fileManager.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return nil
    }
}

/// Persisted launch preferences: the chosen executable path and last-used port.
public struct DaemonLaunchSettings: Codable, Equatable, Sendable {
    public var executablePath: String?
    public var preferredPort: Int?

    public init(executablePath: String? = nil, preferredPort: Int? = 8765) {
        self.executablePath = executablePath
        self.preferredPort = preferredPort
    }

    public static let `default` = DaemonLaunchSettings()

    private enum CodingKeys: String, CodingKey {
        case executablePath
        case preferredPort
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let rawPath = try container.decodeIfPresent(String.self, forKey: .executablePath)
        self.executablePath = (rawPath?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false)
            ? rawPath
            : nil
        self.preferredPort = try container.decodeIfPresent(Int.self, forKey: .preferredPort)
            ?? 8765
    }
}

/// File-backed persistence for `DaemonLaunchSettings`.
///
/// Stored at `~/Library/Application Support/NaumiAgentWorkbench/daemon-launch.json`.
public final class DaemonLaunchSettingsStore: @unchecked Sendable {
    public let url: URL
    private let fileManager: FileManager

    public init(url: URL, fileManager: FileManager = .default) {
        self.url = url
        self.fileManager = fileManager
    }

    public func load() -> DaemonLaunchSettings {
        guard let data = try? Data(contentsOf: url) else {
            return .default
        }
        guard let decoded = try? JSONDecoder().decode(DaemonLaunchSettings.self, from: data) else {
            return .default
        }
        return decoded
    }

    public func save(_ settings: DaemonLaunchSettings) throws {
        let directory = url.deletingLastPathComponent()
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(settings)
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
            .appendingPathComponent("daemon-launch.json", isDirectory: false)
    }

    public static var `default`: DaemonLaunchSettingsStore {
        DaemonLaunchSettingsStore(url: defaultStoreURL)
    }
}
