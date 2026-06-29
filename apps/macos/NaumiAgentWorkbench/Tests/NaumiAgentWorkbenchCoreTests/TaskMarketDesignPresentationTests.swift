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
        #expect(presentation.activeLeases[0].leaseID == "lzh-001")
        #expect(presentation.activeLeases[0].title == "实现 API Client")
        #expect(presentation.activeLeases[0].worktree == "wt-api-client")
        #expect(presentation.activeLeases[0].owner == "agent-a")
    }

    @Test func exposesClaimActionStateAndLocalizedDisabledReasons() {
        let presentation = TaskMarketDesignPresentation(snapshot: nil)

        let openIssue = presentation.rows.first { $0.taskID == "design-lease" }
        let blockedIssue = presentation.rows.first { $0.taskID == "design-snapshot" }
        let leasedIssue = presentation.rows.first { $0.taskID == "design-failure-cards" }

        #expect(openIssue?.canClaim == true)
        #expect(openIssue?.claimDisabledReason(locale: .zhCN) == nil)
        #expect(openIssue?.claimDisabledReason(locale: .enUS) == nil)
        #expect(openIssue?.defaultClaimWorktreeName == "wt-design-lease")

        #expect(blockedIssue?.canClaim == false)
        #expect(blockedIssue?.claimDisabledReason(locale: .zhCN) == "存在未完成依赖，暂不能认领")
        #expect(blockedIssue?.claimDisabledReason(locale: .enUS) == "Unresolved dependencies block this claim")

        #expect(leasedIssue?.canClaim == false)
        #expect(leasedIssue?.claimDisabledReason(locale: .zhCN) == "已有活跃租约，需先释放或转派")
        #expect(leasedIssue?.claimDisabledReason(locale: .enUS) == "An active lease must be released or reassigned first")
    }

    @Test func refreshedLeasesOverrideSnapshotAndFixtures() throws {
        let snapshot = try loadZHSnapshot()
        let refreshedLease = makeLease(
            id: "lease-refreshed",
            taskID: "2",
            agentID: "refresh-agent",
            state: "active",
            expiresAt: "2026-06-27T09:15:00",
            worktreeName: "wt-refreshed-api"
        )

        let presentation = TaskMarketDesignPresentation(
            snapshot: snapshot,
            refreshedLeases: [refreshedLease]
        )

        #expect(presentation.activeLeases.map(\.leaseID) == ["lease-refreshed"])
        #expect(presentation.activeLeases[0].number == 1)
        #expect(presentation.activeLeases[0].title == "实现 API Client")
        #expect(presentation.activeLeases[0].worktree == "wt-refreshed-api")
        #expect(presentation.activeLeases[0].owner == "refresh-agent")
        #expect(presentation.activeLeases[0].status == "Active")
        #expect(presentation.activeLeases[0].tone == "green")
    }

    @Test func refreshedLeasesIgnoreNonActiveRowsForActiveLeaseStrip() throws {
        let snapshot = try loadZHSnapshot()
        let activeLease = makeLease(
            id: "lease-active",
            taskID: "2",
            agentID: "agent-active",
            state: "active",
            expiresAt: "2026-06-27T09:15:00",
            worktreeName: "wt-active"
        )
        let releasedLease = makeLease(
            id: "lease-released",
            taskID: "2",
            agentID: "agent-released",
            state: "released",
            expiresAt: "2026-06-27T08:45:00",
            worktreeName: "wt-released"
        )

        let presentation = TaskMarketDesignPresentation(
            snapshot: snapshot,
            refreshedLeases: [releasedLease, activeLease]
        )

        #expect(presentation.activeLeases.map(\.leaseID) == ["lease-active"])
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

    private func makeLease(
        id: String,
        taskID: String,
        agentID: String,
        state: String,
        expiresAt: String,
        worktreeName: String
    ) -> LeaseDTO {
        LeaseDTO(
            id: id,
            sessionID: "sess-001",
            taskID: taskID,
            agentID: agentID,
            state: state,
            expiresAt: expiresAt,
            worktreeName: worktreeName,
            createdAt: "2026-06-27T08:00:00",
            updatedAt: "2026-06-27T08:05:00"
        )
    }
}
