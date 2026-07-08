import Foundation
import Testing
@testable import NaumiAgentWorkbenchCore

struct TaskMarketDesignPresentationTests {

    // MARK: - Preview mode (fixture fillers explicitly allowed)

    @Test func fixtureExpandsToDenseReferenceMarket() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = TaskMarketDesignPresentation(snapshot: snapshot, policy: .preview)

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
        let presentation = TaskMarketDesignPresentation(snapshot: nil, policy: .preview)

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
            refreshedLeases: [refreshedLease],
            policy: .preview
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
            refreshedLeases: [releasedLease, activeLease],
            policy: .preview
        )

        #expect(presentation.activeLeases.map(\.leaseID) == ["lease-active"])
    }

    // MARK: - Real mode (no fixture fillers)

    @Test func realModeNeverAppendsFixtureRows() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = TaskMarketDesignPresentation(snapshot: snapshot, policy: .real)

        #expect(presentation.rows.contains { $0.taskID.hasPrefix("design-") } == false)
        #expect(presentation.activeLeases.contains { $0.leaseID.hasPrefix("fixture-lease-") } == false)
    }

    @Test func realModeShowsNoBidsWhenBidModelAbsent() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = TaskMarketDesignPresentation(snapshot: snapshot, policy: .real)

        // No persisted bid model exists yet (see M08). Real mode must not
        // fabricate bids.
        #expect(presentation.bids.isEmpty)
    }

    @Test func realModeEmptySnapshotShowsNoFixtures() {
        let presentation = TaskMarketDesignPresentation(snapshot: nil, policy: .real)

        #expect(presentation.rows.isEmpty)
        #expect(presentation.bids.isEmpty)
        #expect(presentation.activeLeases.isEmpty)
    }

    @Test func realModeMapsLiveRowsWithoutBackfill() throws {
        let snapshot = try loadZHSnapshot()
        let presentation = TaskMarketDesignPresentation(snapshot: snapshot, policy: .real)

        // The zh fixture snapshot carries live tasks/issues; real mode should
        // surface only those mapped rows, never padded to 8.
        #expect(presentation.rows.count <= snapshot.tasks.count)
        #expect(presentation.rows.allSatisfy { !$0.taskID.hasPrefix("design-") })
    }

    @Test func realModeRespectsRefreshedActiveLeasesOnly() throws {
        let snapshot = try loadZHSnapshot()
        let refreshedLease = makeLease(
            id: "lease-real-1",
            taskID: "2",
            agentID: "agent-real",
            state: "active",
            expiresAt: "2026-06-27T09:15:00",
            worktreeName: "wt-real"
        )

        let presentation = TaskMarketDesignPresentation(
            snapshot: snapshot,
            refreshedLeases: [refreshedLease],
            policy: .real
        )

        #expect(presentation.activeLeases.map(\.leaseID) == ["lease-real-1"])
        #expect(presentation.activeLeases.allSatisfy { !$0.leaseID.hasPrefix("fixture-lease-") })
    }

    @Test func realModeDefaultsToNoFixtureFillers() throws {
        // The default policy must be real — production views that forget to
        // pass a policy must never accidentally render fixtures.
        let snapshot = try loadZHSnapshot()
        let presentation = TaskMarketDesignPresentation(snapshot: snapshot)

        #expect(presentation.rows.contains { $0.taskID.hasPrefix("design-") } == false)
        #expect(presentation.activeLeases.contains { $0.leaseID.hasPrefix("fixture-lease-") } == false)
        #expect(presentation.bids.isEmpty)
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
