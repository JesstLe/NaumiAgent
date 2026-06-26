import Testing
@testable import NaumiAgentWorkbenchCore

struct ValidationRunPresentationTests {

    @Test func commandFormatsAsSingleLine() {
        let run = ValidationRunDTO(
            id: "run-1",
            sessionID: "sess-1",
            taskID: "task-1",
            actor: "ValidationRunner",
            command: ["pytest", "-x", "tests/unit"],
            cwd: "/workspace",
            status: "passed",
            exitCode: 0,
            output: "ok",
            startedAt: "2026-06-27T06:00:00",
            completedAt: "2026-06-27T06:00:01"
        )

        let presentation = ValidationRunPresentation(run: run)

        #expect(presentation.commandLine == "pytest -x tests/unit")
    }

    @Test func outputSummaryTrimsWhitespaceAndLimitsLength() {
        let run = ValidationRunDTO(
            id: "run-2",
            sessionID: "sess-1",
            taskID: "task-2",
            actor: "ValidationRunner",
            command: [],
            cwd: "/workspace",
            status: "failed",
            exitCode: 1,
            output: "\n\n  First line\n\n  Second line  \n\n",
            startedAt: "2026-06-27T06:00:00",
            completedAt: "2026-06-27T06:00:01"
        )

        let presentation = ValidationRunPresentation(run: run)

        #expect(presentation.outputSummary == "First line Second line")
    }

    @Test func outputSummaryTruncatesLongOutput() {
        let longOutput = String(repeating: "a", count: 300)
        let run = ValidationRunDTO(
            id: "run-3",
            sessionID: "sess-1",
            taskID: "task-3",
            actor: "ValidationRunner",
            command: [],
            cwd: "/workspace",
            status: "passed",
            exitCode: 0,
            output: longOutput,
            startedAt: "2026-06-27T06:00:00",
            completedAt: "2026-06-27T06:00:01"
        )

        let presentation = ValidationRunPresentation(run: run)

        #expect(presentation.outputSummary.count == 201)
        #expect(presentation.outputSummary.hasSuffix("…"))
    }

    @Test func statusLabelsAreLocalized() {
        let passed = ValidationRunPresentation(run: makeRun(status: "passed"))
        let failed = ValidationRunPresentation(run: makeRun(status: "failed"))
        let unknown = ValidationRunPresentation(run: makeRun(status: "running"))

        #expect(passed.statusLabel(locale: .zhCN) == "通过")
        #expect(passed.statusLabel(locale: .enUS) == "Passed")
        #expect(failed.statusLabel(locale: .zhCN) == "失败")
        #expect(failed.statusLabel(locale: .enUS) == "Failed")
        #expect(unknown.statusLabel(locale: .zhCN) == "未知: running")
        #expect(unknown.statusLabel(locale: .enUS) == "Unknown: running")
    }

    // MARK: - Helpers

    private func makeRun(status: String) -> ValidationRunDTO {
        ValidationRunDTO(
            id: "run-\(status)",
            sessionID: "sess-1",
            taskID: "task-1",
            actor: "ValidationRunner",
            command: [],
            cwd: "/workspace",
            status: status,
            exitCode: 0,
            output: "",
            startedAt: "2026-06-27T06:00:00",
            completedAt: "2026-06-27T06:00:01"
        )
    }
}
