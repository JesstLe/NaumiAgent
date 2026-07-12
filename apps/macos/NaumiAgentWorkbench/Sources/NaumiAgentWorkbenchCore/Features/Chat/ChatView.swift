import AppKit
import SwiftUI

/// Codex-grade daily chat surface backed by the real Workbench session.
public struct ChatView: View {
    let appState: AppState
    let daemonController: DaemonController

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
                selectedMissionID: composerBinding(\.selectedMissionID),
                issues: appState.issues,
                tasks: appState.snapshot?.tasks ?? [],
                runs: appState.chatRuns,
                locale: appState.locale,
                onIssueSelect: { taskID in
                    ChatNavigationCommand.issue(taskID: taskID).apply(to: appState)
                },
                onRunSelect: { runID in
                    ChatNavigationCommand.run(id: runID).apply(to: appState)
                }
            )
            .frame(width: 280)

            Divider()

            ChatConversationView(
                messages: displayedChatMessages,
                execution: displayedExecution,
                locale: appState.locale,
                onPermissionDecision: resolvePermission,
                onReview: {
                    ChatNavigationCommand.review.apply(to: appState)
                }
            ) {
                ChatComposer(
                    draft: composerBinding(\.draftMessage),
                    mode: composerBinding(\.mode),
                    selectedMissionID: composerBinding(\.selectedMissionID),
                    issueTitle: composerBinding(\.issueTitle),
                    issueDescription: composerBinding(\.issueDescription),
                    acceptanceCriteria: composerBinding(\.acceptanceCriteria),
                    parallelMode: composerBinding(\.parallelMode),
                    riskLevel: composerBinding(\.riskLevel),
                    linkedIssueID: composerBinding(\.linkedIssueID),
                    runtimeMode: composerBinding(\.runtimeMode),
                    missions: appState.missions,
                    issues: appState.issues,
                    sources: appState.chatComposerState.selectedSources,
                    locale: appState.locale,
                    isSending: appState.isChatSending,
                    errorMessage: appState.chatSendError?.localizedMessage(locale: appState.locale),
                    disabledReason: sendDisabledReason,
                    canSend: canSend,
                    onPrimaryAction: performPrimaryAction,
                    onAddSource: chooseSource,
                    onRemoveSource: { sourceID in
                        appState.chatComposerState.selectedSources.removeAll { $0.id == sourceID }
                    }
                )
            }
            .frame(width: 800)

            Divider()

            ChatInspector(
                appState: appState,
                onReview: { ChatNavigationCommand.review.apply(to: appState) },
                onMission: { id in
                    ChatNavigationCommand.mission(id: id).apply(to: appState)
                },
                onIssues: {
                    if let issue = appState.issues.first {
                        ChatNavigationCommand.issue(taskID: issue.taskID).apply(to: appState)
                    }
                },
                onSource: openSource
            )
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
    }

    private func composerBinding<Value>(
        _ keyPath: WritableKeyPath<ChatComposerSessionState, Value>
    ) -> Binding<Value> {
        Binding(
            get: { appState.chatComposerState[keyPath: keyPath] },
            set: { appState.chatComposerState[keyPath: keyPath] = $0 }
        )
    }

    private var displayedChatMessages: [ChatMessageDTO] {
        ChatMessagePresentation.displayMessages(from: appState.chatMessages)
    }

    private var displayedExecution: ChatExecutionPresentation? {
        if let active = appState.activeChatExecution { return active }
        guard let run = appState.selectedChatRun else { return nil }
        return ChatExecutionPresentation.restoring(run)
    }

    private var canSend: Bool {
        guard ChatComposerPresentation.canSend(
            draft: appState.chatComposerState.draftMessage,
            isSending: appState.isChatSending
        ) else {
            return false
        }
        if appState.chatComposerState.mode == .createIssue {
            guard currentMissionID != nil else { return false }
            if isHighRiskIssue && acceptanceCriteriaLines.isEmpty { return false }
        }
        if appState.chatComposerState.mode == .linkIssue,
           appState.chatComposerState.linkedIssueID.isEmpty { return false }
        return true
    }

    private var sendDisabledReason: String? {
        let hasMessage = !appState.chatComposerState.draftMessage
            .trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        guard hasMessage else { return nil }
        if appState.chatComposerState.mode == .createIssue && currentMissionID == nil {
            return AppStrings.Chat.issueNeedsMission(appState.locale)
        }
        if appState.chatComposerState.mode == .createIssue
            && isHighRiskIssue && acceptanceCriteriaLines.isEmpty {
            return AppStrings.Chat.highRiskNeedsCriteria(appState.locale)
        }
        if appState.chatComposerState.mode == .linkIssue
            && appState.chatComposerState.linkedIssueID.isEmpty {
            return AppStrings.Chat.issueNeedsSelection(appState.locale)
        }
        return nil
    }

    private var currentMissionID: String? {
        if !appState.chatComposerState.selectedMissionID.isEmpty {
            return appState.chatComposerState.selectedMissionID
        }
        return appState.selectedMission?.id ?? appState.missions.first?.id
    }

    private var isHighRiskIssue: Bool {
        appState.chatComposerState.riskLevel == "high"
            || appState.chatComposerState.riskLevel == "critical"
    }

    private var acceptanceCriteriaLines: [String] {
        appState.chatComposerState.acceptanceCriteria
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    private func performPrimaryAction() {
        let presentation = ChatComposerPresentation(
            isSending: appState.isChatSending,
            hasError: appState.chatSendError != nil
        )
        switch presentation.primaryAction {
        case .send, .retry:
            sendMessage()
        case .stop:
            cancelSending()
        }
    }

    private func sendMessage() {
        let trimmedMessage = appState.chatComposerState.draftMessage
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedMessage.isEmpty else { return }

        let draft = issueDraft(for: trimmedMessage)
        daemonController.beginDailyMessage(
            content: trimmedMessage,
            issueDraft: draft,
            sourceIDs: appState.chatComposerState.selectedSources.map(\.id),
            linkedIssueID: appState.chatComposerState.mode == .linkIssue
                ? appState.chatComposerState.linkedIssueID
                : nil,
            runtimeMode: appState.chatComposerState.runtimeMode
        )
    }

    private func cancelSending() {
        Task { @MainActor in
            await daemonController.cancelActiveChatRun()
        }
    }

    private func resolvePermission(_ decision: ChatPermissionDecision) {
        Task { await daemonController.resolveActiveChatPermission(decision) }
    }

    private func issueDraft(for message: String) -> ChatIssueDraftDTO? {
        guard appState.chatComposerState.mode == .createIssue,
              let missionID = currentMissionID else { return nil }

        let title = appState.chatComposerState.issueTitle
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let description = appState.chatComposerState.issueDescription
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return ChatIssueDraftDTO(
            missionID: missionID,
            title: title.isEmpty ? String(message.prefix(36)) : title,
            description: description.isEmpty ? message : description,
            acceptanceCriteria: acceptanceCriteriaLines,
            parallelMode: appState.chatComposerState.parallelMode,
            riskLevel: appState.chatComposerState.riskLevel
        )
    }

    private func ensureSelectedMission() {
        if appState.chatComposerState.selectedMissionID.isEmpty,
           let first = appState.missions.first {
            appState.chatComposerState.selectedMissionID = first.id
        }
        if !appState.chatComposerState.selectedMissionID.isEmpty,
           !appState.missions.contains(where: {
               $0.id == appState.chatComposerState.selectedMissionID
           }) {
            appState.chatComposerState.selectedMissionID = appState.missions.first?.id ?? ""
        }
    }

    private func chooseSource() {
        guard !appState.isPreviewFixture else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if let root = appState.chatEnvironment?.workspaceRoot, !root.isEmpty {
            panel.directoryURL = URL(fileURLWithPath: root, isDirectory: true)
        }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { @MainActor in
            guard let source = await daemonController.addChatSource(path: url.path) else {
                return
            }
            if !appState.chatComposerState.selectedSources.contains(where: { $0.id == source.id }) {
                appState.chatComposerState.selectedSources.append(source)
            }
        }
    }

    private func openSource(_ source: ChatSourceReferenceDTO) {
        guard let workspaceRoot = appState.chatEnvironment?.workspaceRoot,
              let command = ChatSourceOpenCommand(
                source: source,
                workspaceRoot: workspaceRoot
              ) else {
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([command.fileURL])
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
