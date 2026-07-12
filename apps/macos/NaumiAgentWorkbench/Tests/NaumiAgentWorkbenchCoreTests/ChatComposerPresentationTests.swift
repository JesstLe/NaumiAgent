import Testing
@testable import NaumiAgentWorkbenchCore

struct ChatComposerPresentationTests {
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
