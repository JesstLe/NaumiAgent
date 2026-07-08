import Foundation
import Network

/// Tests whether a TCP port is bindable on the loopback interface, so the app
/// can pick a free port for a supervised daemon without colliding.
public protocol DaemonPortProbing: Sendable {
    /// Returns `true` when a listener can bind to `port` on `host`.
    func isPortAvailable(_ port: Int, host: String) async -> Bool
    /// Returns the first bindable port in `range` on `host`, or `nil`.
    func findAvailablePort(in range: ClosedRange<Int>, host: String) async -> Int?
}

/// One-shot thread-safe flag so the continuation resumes exactly once even when
/// `NWListener` emits several state transitions.
private final class ResumeFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var fired = false

    func tryFire() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if fired { return false }
        fired = true
        return true
    }
}

/// Production port probe backed by `Network.NWListener`.
public struct DaemonPortProbe: DaemonPortProbing {
    private let queue: DispatchQueue

    public init(queue: DispatchQueue = DispatchQueue(label: "NaumiAgentWorkbench.portProbe")) {
        self.queue = queue
    }

    public func isPortAvailable(_ port: Int, host: String) async -> Bool {
        guard (1...65535).contains(port),
              let endpointPort = NWEndpoint.Port(rawValue: UInt16(port)) else {
            return false
        }
        let params = NWParameters.tcp
        params.requiredLocalEndpoint = NWEndpoint.hostPort(
            host: NWEndpoint.Host(host),
            port: endpointPort
        )
        params.allowLocalEndpointReuse = true

        return await withCheckedContinuation { continuation in
            let flag = ResumeFlag()

            let listener: NWListener
            do {
                listener = try NWListener(using: params)
            } catch {
                if flag.tryFire() { continuation.resume(returning: false) }
                return
            }

            listener.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    listener.cancel()
                    if flag.tryFire() { continuation.resume(returning: true) }
                case .failed:
                    listener.cancel()
                    if flag.tryFire() { continuation.resume(returning: false) }
                case .cancelled:
                    if flag.tryFire() { continuation.resume(returning: false) }
                default:
                    break
                }
            }
            listener.newConnectionHandler = { connection in
                connection.cancel()
            }
            listener.start(queue: queue)

            // Safety timeout: never leave the caller hanging if the stack does
            // not emit a terminal state.
            queue.asyncAfter(deadline: .now() + 0.5) {
                listener.cancel()
                if flag.tryFire() { continuation.resume(returning: false) }
            }
        }
    }

    public func findAvailablePort(in range: ClosedRange<Int>, host: String) async -> Int? {
        for port in range {
            if await isPortAvailable(port, host: host) {
                return port
            }
        }
        return nil
    }
}
