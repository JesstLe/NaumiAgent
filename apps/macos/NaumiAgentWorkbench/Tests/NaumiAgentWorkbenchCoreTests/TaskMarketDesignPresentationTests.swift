import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct TaskMarketDesignPresentationTests {

    @Test func fixtureExpandsToDenseReferenceMarket() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = TaskMarketDesignPresentation(snapshot: snapshot)

        #expect(presentation.rows.count == 8)
        #expect(presentation.activeLeases.count == 4)
        #expect(presentation.bids.count == 3)
        #expect(presentation.selectedIssue?.number == 1)
        #expect(presentation.filters.riskLevels.map(\.label) == ["Critical", "High", "Medium", "Low"])
        #expect(presentation.rows[0].title == "实现 API Client")
        #expect(presentation.rows[1].status == "Blocked")
    }

    private func loadZHSnapshot() throws -> WorkbenchSnapshotDTO {
        let data = try loadFixture(named: "workbench_snapshot_zh")
        return try JSONDecoder().decode(WorkbenchSnapshotDTO.self, from: data)
    }

    private func loadFixture(named: String) throws -> Data {
        let fixturesURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/\(named).json")
        return try Data(contentsOf: fixturesURL)
    }
}
