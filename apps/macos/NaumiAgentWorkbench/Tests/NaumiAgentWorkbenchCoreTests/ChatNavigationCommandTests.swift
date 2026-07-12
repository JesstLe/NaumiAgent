import Testing
@testable import NaumiAgentWorkbenchCore

struct ChatNavigationCommandTests {
    @Test @MainActor func issueSelectionOpensTaskMarketAndSelectsRealIssue() {
        let state = AppState()
        let issue = IssueDTO(
            sessionID: "sess-1",
            taskID: "task-1",
            missionID: "mission-1",
            parallelMode: "exclusive",
            riskLevel: "medium",
            requiresHumanApproval: false,
            acceptanceCriteria: [],
            expectedArtifacts: [],
            relatedBranch: "",
            relatedWorktree: "",
            relatedPR: "",
            createdAt: "",
            updatedAt: ""
        )
        state.issues = [issue]

        ChatNavigationCommand.issue(taskID: "task-1").apply(to: state)

        #expect(state.currentRoute == .taskMarket)
        #expect(state.selectedIssue == issue)
    }

    @Test @MainActor func reviewAndMissionCommandsUseExistingRoutes() {
        let state = AppState()
        let mission = MissionDTO(
            id: "mission-1",
            sessionID: "sess-1",
            title: "Build chat",
            goal: "",
            status: "active",
            createdAt: "",
            updatedAt: ""
        )
        state.missions = [mission]

        ChatNavigationCommand.mission(id: "mission-1").apply(to: state)
        #expect(state.currentRoute == .dashboard)
        #expect(state.selectedMission == mission)

        ChatNavigationCommand.review.apply(to: state)
        #expect(state.currentRoute == .reviews)
    }

    @Test func sourceOpenCommandRejectsPathTraversal() {
        let safe = ChatSourceReferenceDTO(
            id: "source-1",
            kind: "file",
            title: "spec.md",
            path: "docs/spec.md",
            runID: "",
            createdAt: ""
        )
        let escaped = ChatSourceReferenceDTO(
            id: "source-2",
            kind: "file",
            title: "secret",
            path: "../secret",
            runID: "",
            createdAt: ""
        )

        #expect(ChatSourceOpenCommand(source: safe, workspaceRoot: "/repo") != nil)
        #expect(ChatSourceOpenCommand(source: escaped, workspaceRoot: "/repo") == nil)
    }
}
