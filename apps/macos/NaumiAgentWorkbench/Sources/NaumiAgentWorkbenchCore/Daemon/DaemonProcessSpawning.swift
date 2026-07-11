import Foundation

/// A launched daemon process handle: its pid, streamed output, and lifecycle
/// controls. Closures are `@Sendable` so the handle crosses actor boundaries.
public struct SpawnedDaemonProcess: Sendable {
    public let processIdentifier: Int32
    public let stdout: AsyncStream<Data>
    public let stderr: AsyncStream<Data>
    public let terminate: @Sendable () -> Void
    public let waitForExit: @Sendable () -> Int32

    public init(
        processIdentifier: Int32,
        stdout: AsyncStream<Data>,
        stderr: AsyncStream<Data>,
        terminate: @Sendable @escaping () -> Void,
        waitForExit: @Sendable @escaping () -> Int32
    ) {
        self.processIdentifier = processIdentifier
        self.stdout = stdout
        self.stderr = stderr
        self.terminate = terminate
        self.waitForExit = waitForExit
    }
}

/// Spawns a daemon process with piped stdout/stderr. Abstracted so tests can
/// supply a controlled process double.
public protocol DaemonProcessSpawning: Sendable {
    func spawn(
        executable: String,
        arguments: [String],
        environment: [String: String]
    ) throws -> SpawnedDaemonProcess
}

/// `Foundation.Process`-backed spawner.
public struct FoundationDaemonProcessSpawner: DaemonProcessSpawning {
    public init() {}

    public func spawn(
        executable: String,
        arguments: [String],
        environment: [String: String]
    ) throws -> SpawnedDaemonProcess {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.environment = environment

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        try process.run()
        let pid = process.processIdentifier

        return SpawnedDaemonProcess(
            processIdentifier: pid,
            stdout: Self.makeStream(from: stdoutPipe),
            stderr: Self.makeStream(from: stderrPipe),
            terminate: { process.terminate() },
            waitForExit: {
                process.waitUntilExit()
                return process.terminationStatus
            }
        )
    }

    /// Turns a pipe's file handle into an `AsyncStream` of available data,
    /// ending with EOF. Runs the read loop off the main thread.
    static func makeStream(from pipe: Pipe) -> AsyncStream<Data> {
        AsyncStream { continuation in
            Task.detached(priority: .utility) {
                let handle = pipe.fileHandleForReading
                while true {
                    let data = handle.availableData
                    if data.isEmpty {
                        // EOF.
                        continuation.finish()
                        return
                    }
                    continuation.yield(data)
                }
            }
        }
    }
}

/// Polls a daemon endpoint until it answers or the deadline passes.
public protocol DaemonHealthChecking: Sendable {
    /// Returns `true` once the endpoint responds successfully within `timeout`.
    func waitForHealth(
        endpoint: URL,
        bearerToken: String?,
        timeout: TimeInterval
    ) async -> Bool
}

/// `URLSession`-backed health checker that GETs the daemon status endpoint.
public struct HTTPDaemonHealthChecker: DaemonHealthChecking {
    private let session: URLSession
    private let pollInterval: TimeInterval

    public init(
        session: URLSession = .shared,
        pollInterval: TimeInterval = 0.25
    ) {
        self.session = session
        self.pollInterval = pollInterval
    }

    public func waitForHealth(
        endpoint: URL,
        bearerToken: String?,
        timeout: TimeInterval
    ) async -> Bool {
        let statusURL = endpoint.appendingPathComponent("workbench/daemon/status")
        let deadline = Date().addingTimeInterval(timeout)

        while Date() < deadline {
            var request = URLRequest(url: statusURL)
            if let bearerToken, !bearerToken.isEmpty {
                request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
            }
            do {
                let (_, response) = try await session.data(for: request)
                if let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) {
                    return true
                }
            } catch {
                // Not up yet; keep polling.
            }
            try? await Task.sleep(nanoseconds: UInt64(pollInterval * 1_000_000_000))
        }
        return false
    }
}
