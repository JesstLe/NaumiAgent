import Testing
@testable import NaumiAgentWorkbenchCore

struct ContextSnapshotPresentationTests {

    @Test func healthLabelsAreLocalized() {
        let good = makePresentation(health: "good")
        let stale = makePresentation(health: "stale")
        let overloaded = makePresentation(health: "overloaded")
        let missing = makePresentation(health: "missing")
        let conflicted = makePresentation(health: "conflicted")
        let unknown = makePresentation(health: "weird")

        #expect(good.healthLabel(locale: .zhCN) == "健康")
        #expect(good.healthLabel(locale: .enUS) == "Good")
        #expect(stale.healthLabel(locale: .zhCN) == "过期")
        #expect(stale.healthLabel(locale: .enUS) == "Stale")
        #expect(overloaded.healthLabel(locale: .zhCN) == "过载")
        #expect(overloaded.healthLabel(locale: .enUS) == "Overloaded")
        #expect(missing.healthLabel(locale: .zhCN) == "缺失")
        #expect(missing.healthLabel(locale: .enUS) == "Missing")
        #expect(conflicted.healthLabel(locale: .zhCN) == "冲突")
        #expect(conflicted.healthLabel(locale: .enUS) == "Conflicted")
        #expect(unknown.healthLabel(locale: .zhCN) == "未知: weird")
        #expect(unknown.healthLabel(locale: .enUS) == "Unknown: weird")
    }

    @Test func reasonsAreJoinedIntoSingleLineSummary() {
        let snapshot = makeSnapshot(reasons: ["原因一", "原因二"])
        let presentation = ContextSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.reasonsSummary == "原因一；原因二")
        #expect(presentation.reasonsSummary(locale: .zhCN) == "原因一；原因二")
        #expect(presentation.reasonsSummary(locale: .enUS) == "原因一; 原因二")
    }

    @Test func emptyReasonsShowPlaceholder() {
        let snapshot = makeSnapshot(reasons: [])
        let presentation = ContextSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.reasonsSummary == "-")
    }

    @Test func whitespaceReasonsAreTrimmedAndSkipped() {
        let snapshot = makeSnapshot(reasons: ["", "  ", "有效原因", "\n\n"])
        let presentation = ContextSnapshotPresentation(snapshot: snapshot)

        #expect(presentation.reasonsSummary == "有效原因")
    }

    // MARK: - Helpers

    private func makeSnapshot(health: String = "good", reasons: [String] = []) -> ContextSnapshotDTO {
        ContextSnapshotDTO(
            id: "snap-1",
            sessionID: "sess-1",
            agentID: "agent-1",
            taskID: "task-1",
            health: health,
            reasons: reasons,
            createdAt: "2026-06-27T06:00:00"
        )
    }

    private func makePresentation(health: String) -> ContextSnapshotPresentation {
        ContextSnapshotPresentation(snapshot: makeSnapshot(health: health))
    }
}
