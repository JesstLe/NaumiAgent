import Foundation

/// List of intent locks returned by `GET /workbench/sessions/{id}/missions/{id}/intent-locks`.
public struct IntentLocksDTO: Decodable, Equatable, Sendable {
    public let intentLocks: [IntentLockDTO]
    public let missionID: String
    public let active: Bool?

    public enum CodingKeys: String, CodingKey {
        case intentLocks = "intent_locks"
        case missionID = "mission_id"
        case active
    }

    public init(intentLocks: [IntentLockDTO], missionID: String, active: Bool? = nil) {
        self.intentLocks = intentLocks
        self.missionID = missionID
        self.active = active
    }
}
