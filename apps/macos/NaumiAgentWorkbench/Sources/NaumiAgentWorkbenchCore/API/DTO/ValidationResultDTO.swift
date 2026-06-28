import Foundation

/// Result of `POST /workbench/sessions/{id}/validation-runs`.
public struct ValidationResultDTO: Decodable, Equatable, Sendable {
    public let id: String
    public let status: String
    public let exitCode: Int
    public let output: String

    public enum CodingKeys: String, CodingKey {
        case id
        case status
        case exitCode = "exit_code"
        case output
    }

    public init(
        id: String,
        status: String,
        exitCode: Int,
        output: String
    ) {
        self.id = id
        self.status = status
        self.exitCode = exitCode
        self.output = output
    }
}

/// Response returned when a validation run asks the backend for a fresh snapshot.
public struct ValidationResultSnapshotDTO: Decodable, Equatable, Sendable {
    public let validationRun: ValidationResultDTO
    public let snapshot: WorkbenchSnapshotDTO

    public enum CodingKeys: String, CodingKey {
        case validationRun = "validation_run"
        case snapshot
    }

    public init(validationRun: ValidationResultDTO, snapshot: WorkbenchSnapshotDTO) {
        self.validationRun = validationRun
        self.snapshot = snapshot
    }
}
