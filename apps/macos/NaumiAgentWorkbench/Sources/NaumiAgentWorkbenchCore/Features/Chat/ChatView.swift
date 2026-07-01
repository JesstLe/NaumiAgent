import SwiftUI

/// Daily chat surface that can turn a conversation turn into a Workbench issue.
public struct ChatView: View {
    let appState: AppState
    let daemonController: DaemonController

    @State private var draftMessage = ""
    @State private var createsIssue = false
    @State private var selectedMissionID = ""
    @State private var issueTitle = ""
    @State private var issueDescription = ""
    @State private var acceptanceCriteria = ""
    @State private var parallelMode = "exclusive"
    @State private var riskLevel = "medium"
    @State private var isSending = false

    public init(appState: AppState, daemonController: DaemonController) {
        self.appState = appState
        self.daemonController = daemonController
    }

    public var body: some View {
        HStack(spacing: 0) {
            leftRail
                .frame(width: 300)

            Divider()

            conversationPane
                .frame(width: 760)

            Divider()

            issuePane
                .frame(width: 380)
        }
        .frame(width: 1440, height: 858, alignment: .topLeading)
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear {
            ensureSelectedMission()
        }
        .onChange(of: appState.missions) { _, _ in
            ensureSelectedMission()
        }
    }

    private var leftRail: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(AppStrings.Chat.title(appState.locale))
                .font(.title3)
                .fontWeight(.semibold)

            panel(title: AppStrings.Chat.sessionSection(appState.locale)) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(appState.selectedSessionID ?? "session")
                        .font(.callout)
                        .fontWeight(.medium)
                        .lineLimit(1)
                        .truncationMode(.middle)

                    Text(appState.connectionState.displayName(locale: appState.locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            panel(title: AppStrings.Chat.missionSection(appState.locale)) {
                if appState.missions.isEmpty {
                    Text(AppStrings.Chat.noMission(appState.locale))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    Picker("", selection: $selectedMissionID) {
                        ForEach(appState.missions, id: \.id) { mission in
                            Text(mission.title)
                                .tag(mission.id)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.menu)
                }
            }

            panel(title: AppStrings.GlobalStatus.openIssues(appState.locale)) {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array(appState.issues.prefix(6)), id: \.taskID) { issue in
                        HStack(spacing: 8) {
                            Circle()
                                .fill(color(forRisk: issue.riskLevel))
                                .frame(width: 7, height: 7)
                            Text(issue.taskID)
                                .font(.caption)
                                .fontWeight(.medium)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer()
                            Text(issue.riskLevel)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }

            Spacer()
        }
        .padding(18)
    }

    private var conversationPane: some View {
        VStack(spacing: 0) {
            ScrollViewReader { reader in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        if appState.chatMessages.isEmpty {
                            Text(AppStrings.Chat.emptyMessages(appState.locale))
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, minHeight: 520, alignment: .center)
                        } else {
                            ForEach(appState.chatMessages, id: \.id) { message in
                                messageBubble(message)
                                    .id(message.id)
                            }
                        }
                    }
                    .padding(22)
                }
                .onChange(of: appState.chatMessages.count) { _, _ in
                    guard let lastID = appState.chatMessages.last?.id else { return }
                    withAnimation(.easeOut(duration: 0.18)) {
                        reader.scrollTo(lastID, anchor: .bottom)
                    }
                }
            }

            Divider()

            VStack(alignment: .leading, spacing: 10) {
                TextField(
                    AppStrings.Chat.messagePlaceholder(appState.locale),
                    text: $draftMessage,
                    axis: .vertical
                )
                .textFieldStyle(.roundedBorder)
                .lineLimit(3...6)

                HStack {
                    if let error = appState.lastError {
                        Text(error.localizedMessage(locale: appState.locale))
                            .font(.caption)
                            .foregroundStyle(.red)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }

                    Spacer()

                    Button {
                        sendMessage()
                    } label: {
                        Label(
                            isSending
                                ? AppStrings.Chat.sending(appState.locale)
                                : AppStrings.Chat.sendButton(appState.locale),
                            systemImage: "paperplane.fill"
                        )
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!canSend)
                }
            }
            .padding(16)
            .background(Color(nsColor: .controlBackgroundColor))
        }
    }

    private var issuePane: some View {
        VStack(alignment: .leading, spacing: 16) {
            Toggle(AppStrings.Chat.createIssueToggle(appState.locale), isOn: $createsIssue)
                .toggleStyle(.switch)
                .disabled(currentMissionID == nil)

            panel(title: AppStrings.TaskMarket.createIssueSectionTitle(appState.locale)) {
                VStack(alignment: .leading, spacing: 12) {
                    TextField(AppStrings.Chat.issueTitle(appState.locale), text: $issueTitle)
                        .textFieldStyle(.roundedBorder)
                        .disabled(!createsIssue)

                    TextField(
                        AppStrings.Chat.issueDescription(appState.locale),
                        text: $issueDescription,
                        axis: .vertical
                    )
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(3...5)
                    .disabled(!createsIssue)

                    TextField(
                        AppStrings.Chat.acceptanceCriteria(appState.locale),
                        text: $acceptanceCriteria,
                        axis: .vertical
                    )
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(3...5)
                    .disabled(!createsIssue)

                    Picker(AppStrings.Chat.parallelMode(appState.locale), selection: $parallelMode) {
                        ForEach(["exclusive", "cooperative", "competitive", "exploratory"], id: \.self) {
                            Text(parallelModeLabel($0)).tag($0)
                        }
                    }
                    .disabled(!createsIssue)

                    Picker(AppStrings.Chat.riskLevel(appState.locale), selection: $riskLevel) {
                        ForEach(["low", "medium", "high", "critical"], id: \.self) {
                            Text(riskLabel($0)).tag($0)
                        }
                    }
                    .disabled(!createsIssue)
                }
            }

            if appState.chatMessages.contains(where: hasLinkedIssue) {
                Label(
                    AppStrings.Chat.linkedIssueCreated(appState.locale),
                    systemImage: "checkmark.circle.fill"
                )
                .font(.callout)
                .foregroundStyle(.green)
            }

            Spacer()
        }
        .padding(18)
    }

    private var canSend: Bool {
        let hasMessage = !draftMessage.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        return hasMessage && !isSending && (!createsIssue || currentMissionID != nil)
    }

    private var currentMissionID: String? {
        if !selectedMissionID.isEmpty {
            return selectedMissionID
        }
        return appState.selectedMission?.id ?? appState.missions.first?.id
    }

    private func sendMessage() {
        let trimmedMessage = draftMessage.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedMessage.isEmpty else { return }

        let draft = issueDraft(for: trimmedMessage)
        isSending = true
        Task {
            await daemonController.sendDailyMessage(content: trimmedMessage, issueDraft: draft)
            isSending = false
            if appState.lastError == nil {
                draftMessage = ""
                if draft != nil {
                    issueTitle = ""
                    issueDescription = ""
                    acceptanceCriteria = ""
                }
            }
        }
    }

    private func issueDraft(for message: String) -> ChatIssueDraftDTO? {
        guard createsIssue, let missionID = currentMissionID else {
            return nil
        }

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

    private var acceptanceCriteriaLines: [String] {
        acceptanceCriteria
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
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

    private func messageBubble(_ message: ChatMessageDTO) -> some View {
        let isUser = message.role == "user"
        return HStack {
            if isUser {
                Spacer(minLength: 80)
            }

            VStack(alignment: .leading, spacing: 6) {
                Text(roleLabel(message.role))
                    .font(.caption2)
                    .foregroundStyle(.secondary)

                Text(message.content)
                    .font(.body)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)

                if hasLinkedIssue(message) {
                    Label(
                        AppStrings.Chat.linkedIssueCreated(appState.locale),
                        systemImage: "checkmark.circle"
                    )
                    .font(.caption)
                    .foregroundStyle(.green)
                }
            }
            .padding(12)
            .frame(maxWidth: 520, alignment: .leading)
            .background(isUser ? Color.accentColor.opacity(0.14) : Color(nsColor: .controlBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 8))

            if !isUser {
                Spacer(minLength: 80)
            }
        }
        .frame(maxWidth: .infinity, alignment: isUser ? .trailing : .leading)
    }

    private func panel<Content: View>(title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(.secondary)
                .textCase(.uppercase)

            content()
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func hasLinkedIssue(_ message: ChatMessageDTO) -> Bool {
        guard case .object? = message.metadata["workbench_issue"] else {
            return false
        }
        return true
    }

    private func roleLabel(_ role: String) -> String {
        switch role {
        case "user":
            return appState.locale == .zhCN ? "你" : "You"
        case "assistant":
            return "NaumiAgent"
        default:
            return role
        }
    }

    private func parallelModeLabel(_ value: String) -> String {
        switch (appState.locale, value) {
        case (.zhCN, "exclusive"):
            return "独占"
        case (.zhCN, "cooperative"):
            return "协作"
        case (.zhCN, "competitive"):
            return "竞争"
        case (.zhCN, "exploratory"):
            return "探索"
        default:
            return value
        }
    }

    private func riskLabel(_ value: String) -> String {
        switch (appState.locale, value) {
        case (.zhCN, "low"):
            return "低"
        case (.zhCN, "medium"):
            return "中"
        case (.zhCN, "high"):
            return "高"
        case (.zhCN, "critical"):
            return "严重"
        default:
            return value
        }
    }

    private func color(forRisk risk: String) -> Color {
        switch risk {
        case "critical":
            return .red
        case "high":
            return .orange
        case "medium":
            return .yellow
        default:
            return .green
        }
    }
}

#if DEBUG
struct ChatView_Previews: PreviewProvider {
    @MainActor
    static var previews: some View {
        let state = AppState()
        let controller = DaemonController(
            appState: state,
            apiProvider: WorkbenchAPIClient()
        )
        return ChatView(appState: state, daemonController: controller)
            .frame(width: 1440, height: 858)
    }
}
#endif
