import SwiftUI

/// Codex-grade daily chat surface backed by the real Workbench session.
public struct ChatView: View {
    let appState: AppState
    let daemonController: DaemonController

    @State private var draftMessage = ""
    @State private var composerMode: ChatComposerMode = .chat
    @State private var selectedMissionID = ""
    @State private var issueTitle = ""
    @State private var issueDescription = ""
    @State private var acceptanceCriteria = ""
    @State private var parallelMode = "exclusive"
    @State private var riskLevel = "medium"
    @State private var isSending = false
    @State private var sendError: APIError? = nil
    @State private var sendTask: Task<Void, Never>? = nil

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        HStack(spacing: 0) {
            ChatContextRail(
                sessionID: appState.selectedSessionID,
                connectionText: appState.connectionState.displayName(locale: appState.locale),
                missions: appState.missions,
                selectedMissionID: $selectedMissionID,
                issues: appState.issues,
                tasks: appState.snapshot?.tasks ?? [],
                locale: appState.locale
            )
            .frame(width: 280)

            Divider()

            ChatConversationView(
                messages: displayedChatMessages,
                execution: appState.activeChatExecution,
                locale: appState.locale,
                onPermissionDecision: resolvePermission
            ) {
                ChatComposer(
                    draft: $draftMessage,
                    mode: $composerMode,
                    selectedMissionID: $selectedMissionID,
                    issueTitle: $issueTitle,
                    issueDescription: $issueDescription,
                    acceptanceCriteria: $acceptanceCriteria,
                    parallelMode: $parallelMode,
                    riskLevel: $riskLevel,
                    missions: appState.missions,
                    locale: appState.locale,
                    isSending: isSending,
                    errorMessage: sendError?.localizedMessage(locale: appState.locale),
                    disabledReason: sendDisabledReason,
                    canSend: canSend,
                    onPrimaryAction: performPrimaryAction
                )
            }
            .frame(width: 800)

            Divider()

            ChatInspector(appState: appState)
                .frame(width: 360)
        }
        .frame(width: 1440, height: 858, alignment: .topLeading)
        .background(WorkbenchComponentTheme.surface(.canvas))
        .onAppear {
            ensureSelectedMission()
            if !appState.isPreviewFixture, appState.selectedSessionID != nil {
                Task { await daemonController.refreshChatMessages() }
            }
        }
        .onChange(of: appState.missions) { _, _ in
            ensureSelectedMission()
        }
        .onDisappear {
            sendTask?.cancel()
        }
    }

    private var displayedChatMessages: [ChatMessageDTO] {
        ChatMessagePresentation.displayMessages(from: appState.chatMessages)
    }

    private var canSend: Bool {
        guard ChatComposerPresentation.canSend(draft: draftMessage, isSending: isSending) else {
            return false
        }
        if composerMode == .createIssue {
            guard currentMissionID != nil else { return false }
            if isHighRiskIssue && acceptanceCriteriaLines.isEmpty { return false }
        }
        return true
    }

    private var sendDisabledReason: String? {
        let hasMessage = !draftMessage.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        guard hasMessage else { return nil }
        if composerMode == .createIssue && currentMissionID == nil {
            return AppStrings.Chat.issueNeedsMission(appState.locale)
        }
        if composerMode == .createIssue && isHighRiskIssue && acceptanceCriteriaLines.isEmpty {
            return AppStrings.Chat.highRiskNeedsCriteria(appState.locale)
        }
        return nil
    }

    private var currentMissionID: String? {
        if !selectedMissionID.isEmpty { return selectedMissionID }
        return appState.selectedMission?.id ?? appState.missions.first?.id
    }

    private var isHighRiskIssue: Bool {
        riskLevel == "high" || riskLevel == "critical"
    }

    private var acceptanceCriteriaLines: [String] {
        acceptanceCriteria
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    private func performPrimaryAction() {
        let presentation = ChatComposerPresentation(
            isSending: isSending,
            hasError: sendError != nil
        )
        switch presentation.primaryAction {
        case .send, .retry:
            sendMessage()
        case .stop:
            cancelSending()
        }
    }

    private func sendMessage() {
        let trimmedMessage = draftMessage.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedMessage.isEmpty else { return }

        let draft = issueDraft(for: trimmedMessage)
        sendTask?.cancel()
        sendError = nil
        isSending = true

        sendTask = Task { @MainActor in
            await daemonController.sendDailyMessage(content: trimmedMessage, issueDraft: draft)
            guard !Task.isCancelled else {
                isSending = false
                sendTask = nil
                return
            }

            isSending = false
            sendTask = nil
            if appState.lastError == nil {
                draftMessage = ""
                if draft != nil {
                    issueTitle = ""
                    issueDescription = ""
                    acceptanceCriteria = ""
                    composerMode = .chat
                }
            } else {
                sendError = appState.lastError
            }
        }
    }

    private func cancelSending() {
        sendTask?.cancel()
        sendTask = nil
        isSending = false
    }

    private func resolvePermission(_ decision: ChatPermissionDecision) {
        Task { await daemonController.resolveActiveChatPermission(decision) }
    }

    private func issueDraft(for message: String) -> ChatIssueDraftDTO? {
        guard composerMode == .createIssue, let missionID = currentMissionID else { return nil }

        let title = issueTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        let description = issueDescription.trimmingCharacters(in: .whitespacesAndNewlines)
        return ChatIssueDraftDTO(
            missionID: missionID,
            title: title.isEmpty ? String(message.prefix(36)) : title,
            description: description.isEmpty ? message : description,
            acceptanceCriteria: acceptanceCriteriaLines,
            parallelMode: parallelMode,
            riskLevel: riskLevel
        )
    }

    private func ensureSelectedMission() {
        if selectedMissionID.isEmpty, let first = appState.missions.first {
            selectedMissionID = first.id
        }
        if !selectedMissionID.isEmpty,
           !appState.missions.contains(where: { $0.id == selectedMissionID }) {
            selectedMissionID = appState.missions.first?.id ?? ""
        }
    }
}

#if DEBUG
struct ChatView_Previews: PreviewProvider {
    @MainActor
    static var previews: some View {
        let state = AppState()
        let controller = DaemonController(appState: state, apiProvider: WorkbenchAPIClient())
        return ChatView(appState: state, daemonController: controller)
            .frame(width: 1440, height: 858)
    }
}
#endif
