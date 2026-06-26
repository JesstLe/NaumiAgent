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
