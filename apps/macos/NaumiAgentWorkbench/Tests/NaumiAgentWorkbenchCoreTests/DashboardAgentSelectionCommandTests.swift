import Testing
@testable import NaumiAgentWorkbenchCore

struct DashboardAgentSelectionCommandTests {

    @Test func commandUsesSelectedAgentID() throws {
        let command = try #require(DashboardAgentSelectionCommand(
            agent: agent(id: "  agent-123  ")
        ))

        #expect(command.agentID == "agent-123")
    }

    @Test func commandIsNilWhenAgentIDIsEmpty() {
        #expect(DashboardAgentSelectionCommand(agent: agent(id: "   ")) == nil)
    }

    private func agent(id: String) -> DashboardAgentRow {
        DashboardAgentRow(
            id: id,
            name: "Backend-Agent",
            role: "coder",
            status: "busy",
            capabilityCount: 2,
            maxParallelTasks: 2
        )
    }
}
