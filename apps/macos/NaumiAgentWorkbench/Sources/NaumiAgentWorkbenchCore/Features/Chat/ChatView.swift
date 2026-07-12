import AppKit
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
    @State private var linkedIssueID = ""
    @State private var selectedSources: [ChatSourceReferenceDTO] = []
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
                    draft: $draftMessage,
                    mode: $composerMode,
                    selectedMissionID: $selectedMissionID,
                    issueTitle: $issueTitle,
                    issueDescription: $issueDescription,
                    acceptanceCriteria: $acceptanceCriteria,
                    parallelMode: $parallelMode,
                    riskLevel: $riskLevel,
                    linkedIssueID: $linkedIssueID,
                    missions: appState.missions,
                    issues: appState.issues,
                    sources: selectedSources,
                    locale: appState.locale,
                    isSending: isSending,
                    errorMessage: sendError?.localizedMessage(locale: appState.locale),
                    disabledReason: sendDisabledReason,
                    canSend: canSend,
                    onPrimaryAction: performPrimaryAction,
                    onAddSource: chooseSource,
                    onRemoveSource: { sourceID in
                        selectedSources.removeAll { $0.id == sourceID }
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
        .onDisappear {
            sendTask?.cancel()
        }
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
        guard ChatComposerPresentation.canSend(draft: draftMessage, isSending: isSending) else {
            return false
        }
        if composerMode == .createIssue {
            guard currentMissionID != nil else { return false }
            if isHighRiskIssue && acceptanceCriteriaLines.isEmpty { return false }
        }
        if composerMode == .linkIssue, linkedIssueID.isEmpty { return false }
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
        if composerMode == .linkIssue && linkedIssueID.isEmpty {
            return AppStrings.Chat.issueNeedsSelection(appState.locale)
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
            await daemonController.sendDailyMessage(
                content: trimmedMessage,
                issueDraft: draft,
                sourceIDs: selectedSources.map(\.id),
                linkedIssueID: composerMode == .linkIssue ? linkedIssueID : nil
            )
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
                selectedSources = []
                if composerMode == .linkIssue {
                    linkedIssueID = ""
                    composerMode = .chat
                }
            } else {
                sendError = appState.lastError
            }
        }
    }

    private func cancelSending() {
        let activeTask = sendTask
        sendTask = nil
        isSending = false
        Task { @MainActor in
            await daemonController.cancelActiveChatRun()
            activeTask?.cancel()
        }
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
            if !selectedSources.contains(where: { $0.id == source.id }) {
                selectedSources.append(source)
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
