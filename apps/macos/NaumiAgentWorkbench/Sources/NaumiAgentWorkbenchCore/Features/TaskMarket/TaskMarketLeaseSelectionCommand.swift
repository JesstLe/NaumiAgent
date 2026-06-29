import Foundation

/// API command for loading a selected task-market lease detail.
public struct TaskMarketLeaseSelectionCommand: Equatable, Sendable {
    public let leaseID: String

    public init?(lease: TaskMarketDesignLease) {
        let trimmedLeaseID = lease.leaseID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedLeaseID.isEmpty else {
            return nil
        }

        self.leaseID = trimmedLeaseID
    }
}
