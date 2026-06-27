import Foundation

public enum ReviewApprovalResolutionState: String, Sendable {
    case approved
    case rejected
}

/// API command derived from the currently selected review and human approval form.
public struct ReviewApprovalResolutionCommand: Equatable, Sendable {
    public let approvalID: String
    public let actor: String
    public let state: String
    public let decisionNote: String

    public init?(
        review: ReviewDesignItem,
        draft: ApprovalResolveDraft,
        state: ReviewApprovalResolutionState
    ) {
        let approvalID = review.id.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !approvalID.isEmpty, draft.canResolve else {
            return nil
        }

        self.approvalID = approvalID
        self.actor = draft.trimmedActor
        self.state = state.rawValue
        self.decisionNote = draft.trimmedDecisionNote
    }
}
