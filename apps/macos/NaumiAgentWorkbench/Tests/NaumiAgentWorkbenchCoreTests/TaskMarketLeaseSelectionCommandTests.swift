import Testing
@testable import NaumiAgentWorkbenchCore

struct TaskMarketLeaseSelectionCommandTests {

    @Test func commandUsesSelectedLeaseID() throws {
        let command = try #require(TaskMarketLeaseSelectionCommand(
            lease: lease(leaseID: "  lease-123  ")
        ))

        #expect(command.leaseID == "lease-123")
    }

    @Test func commandIsNilWhenLeaseIDIsEmpty() {
        #expect(TaskMarketLeaseSelectionCommand(lease: lease(leaseID: "   ")) == nil)
    }

    private func lease(leaseID: String) -> TaskMarketDesignLease {
        TaskMarketDesignLease(
            leaseID: leaseID,
            number: 1,
            title: "实现 API Client",
            worktree: "wt-api-client",
            owner: "Backend-Agent",
            status: "Active",
            time: "2026-06-27T10:00:00Z",
            tone: "green"
        )
    }
}
