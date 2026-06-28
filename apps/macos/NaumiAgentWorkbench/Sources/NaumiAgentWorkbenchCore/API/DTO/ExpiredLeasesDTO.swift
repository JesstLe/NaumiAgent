import Foundation

/// Result of `POST /workbench/sessions/{session_id}/leases/expire`.
public struct ExpiredLeasesDTO: Decodable, Equatable, Sendable {
    public let expired: [LeaseDTO]

    public enum CodingKeys: String, CodingKey {
        case expired
    }

    public init(expired: [LeaseDTO]) {
        self.expired = expired
    }
}

/// Result of `POST /workbench/sessions/{session_id}/leases/expire?include_snapshot=true`.
public struct ExpiredLeasesSnapshotDTO: Decodable, Equatable, Sendable {
    public let expired: [LeaseDTO]
    public let snapshot: WorkbenchSnapshotDTO

    public enum CodingKeys: String, CodingKey {
        case expired
        case snapshot
    }

    public init(expired: [LeaseDTO], snapshot: WorkbenchSnapshotDTO) {
        self.expired = expired
        self.snapshot = snapshot
    }
}
