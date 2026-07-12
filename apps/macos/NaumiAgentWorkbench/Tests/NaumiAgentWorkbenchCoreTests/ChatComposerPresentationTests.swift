import Testing
@testable import NaumiAgentWorkbenchCore

struct ChatComposerPresentationTests {
    @Test func composerStatePreservesDraftUntilSuccessfulSubmission() {
        var state = ChatComposerSessionState()
        state.draftMessage = "保留这段输入"
        state.mode = .linkIssue
        state.linkedIssueID = "issue-1"
        state.runtimeMode = .bypass

        #expect(state.draftMessage == "保留这段输入")
        #expect(state.mode == .linkIssue)

        state.resetAfterSuccessfulSubmission()

        #expect(state.draftMessage.isEmpty)
        #expect(state.mode == .chat)
        #expect(state.linkedIssueID.isEmpty)
        #expect(state.runtimeMode == .bypass)
    }

    @Test func sendingReplacesSendWithStopAndKeepsEditorEnabled() {
        let state = ChatComposerPresentation(isSending: true, hasError: false)

        #expect(state.primaryAction == .stop)
        #expect(state.isEditorEnabled)
    }

    @Test func failureReplacesSendWithRetry() {
        let state = ChatComposerPresentation(isSending: false, hasError: true)

        #expect(state.primaryAction == .retry)
    }

    @Test func createIssueExpandsDetailsOnlyInCreateMode() {
        #expect(ChatComposerMode.createIssue.showsIssueDetails)
        #expect(!ChatComposerMode.chat.showsIssueDetails)
        #expect(ChatComposerMode.linkIssue.showsIssuePicker)
    }

    @Test func emptyDraftCannotSend() {
        #expect(!ChatComposerPresentation.canSend(draft: " \n", isSending: false))
        #expect(ChatComposerPresentation.canSend(draft: "继续", isSending: false))
        #expect(!ChatComposerPresentation.canSend(draft: "继续", isSending: true))
    }
}
