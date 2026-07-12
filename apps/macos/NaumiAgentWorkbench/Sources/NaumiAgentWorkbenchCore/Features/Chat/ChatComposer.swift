import SwiftUI

struct ChatComposer: View {
    @FocusState private var isDraftFocused: Bool
    @Binding var draft: String
    @Binding var mode: ChatComposerMode
    @Binding var selectedMissionID: String
    @Binding var issueTitle: String
    @Binding var issueDescription: String
    @Binding var acceptanceCriteria: String
    @Binding var parallelMode: String
    @Binding var riskLevel: String
    @Binding var linkedIssueID: String
    @Binding var runtimeMode: ChatRuntimeMode

    let missions: [MissionDTO]
    let issues: [IssueDTO]
    let sources: [ChatSourceReferenceDTO]
    let locale: AppLocale
    let isSending: Bool
    let errorMessage: String?
    let disabledReason: String?
    let canSend: Bool
    let onPrimaryAction: () -> Void
    let onAddSource: () -> Void
    let onRemoveSource: (String) -> Void

    var body: some View {
        let presentation = ChatComposerPresentation(
            isSending: isSending,
            hasError: errorMessage != nil
        )

        VStack(alignment: .leading, spacing: 10) {
            TextField(
                AppStrings.Chat.messagePlaceholder(locale),
                text: $draft,
                axis: .vertical
            )
            .textFieldStyle(.plain)
            .font(.system(size: 14))
            .lineLimit(3...8)
            .disabled(!presentation.isEditorEnabled)
            .focused($isDraftFocused)

            if mode.showsIssueDetails {
                ChatIssueDraftPanel(
                    title: $issueTitle,
                    description: $issueDescription,
                    acceptanceCriteria: $acceptanceCriteria,
                    parallelMode: $parallelMode,
                    riskLevel: $riskLevel,
                    locale: locale
                )
            }

            if mode.showsIssuePicker, !issues.isEmpty {
                Picker(AppStrings.Chat.linkExistingIssue(locale), selection: $linkedIssueID) {
                    ForEach(issues, id: \.taskID) { issue in
                        Text(issue.task?.subject ?? issue.taskID).tag(issue.taskID)
                    }
                }
                .pickerStyle(.menu)
                .controlSize(.small)
            }

            if !sources.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(sources) { source in
                        HStack(spacing: 7) {
                            Image(systemName: source.kind == "screenshot" ? "photo" : "doc")
                                .foregroundStyle(.secondary)
                            Text(source.title)
                                .font(.caption)
                                .lineLimit(1)
                            Spacer()
                            Button {
                                onRemoveSource(source.id)
                            } label: {
                                Image(systemName: "xmark")
                            }
                            .buttonStyle(.plain)
                            .help(AppStrings.Chat.removeSource(locale))
                            .accessibilityLabel(AppStrings.Chat.removeSource(locale))
                        }
                    }
                }
            }

            HStack(spacing: 8) {
                Menu {
                    Button {
                        mode = .chat
                    } label: {
                        Label(AppStrings.Chat.chatOnly(locale), systemImage: "bubble.left")
                    }
                    Button {
                        mode = .createIssue
                    } label: {
                        Label(AppStrings.Chat.createIssueToggle(locale), systemImage: "checklist")
                    }
                    Button {
                        mode = .linkIssue
                        if linkedIssueID.isEmpty {
                            linkedIssueID = issues.first?.taskID ?? ""
                        }
                    } label: {
                        Label(AppStrings.Chat.linkExistingIssue(locale), systemImage: "link.badge.plus")
                    }
                } label: {
                    Label(
                        mode == .chat
                            ? AppStrings.Chat.taskLinkage(locale)
                            : AppStrings.Chat.createIssueToggle(locale),
                        systemImage: mode == .chat ? "link" : "checklist"
                    )
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .help(AppStrings.Chat.taskLinkage(locale))

                Button(action: onAddSource) {
                    Image(systemName: "paperclip")
                }
                .buttonStyle(.plain)
                .disabled(sources.count >= 3)
                .help(AppStrings.Chat.addSource(locale))
                .accessibilityLabel(AppStrings.Chat.addSource(locale))

                if !missions.isEmpty {
                    Picker(AppStrings.Chat.missionSection(locale), selection: $selectedMissionID) {
                        ForEach(missions, id: \.id) { mission in
                            Text(mission.title).tag(mission.id)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.menu)
                    .controlSize(.small)
                    .frame(maxWidth: 190)
                }

                if let errorMessage {
                    Label(errorMessage, systemImage: "exclamationmark.circle")
                        .font(.caption)
                        .foregroundStyle(.red)
                        .lineLimit(1)
                } else if let disabledReason {
                    Text(disabledReason)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                Spacer()

                Menu {
                    ForEach(ChatRuntimeMode.allCases, id: \.self) { option in
                        Button {
                            runtimeMode = option
                        } label: {
                            Label(
                                AppStrings.Chat.runtimeMode(locale, mode: option),
                                systemImage: runtimeModeSymbol(option)
                            )
                        }
                    }
                } label: {
                    Label(
                        AppStrings.Chat.runtimeMode(locale, mode: runtimeMode),
                        systemImage: runtimeModeSymbol(runtimeMode)
                    )
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .help(AppStrings.Chat.runtimeModeHelp(locale, mode: runtimeMode))

                Button(action: onPrimaryAction) {
                    Image(systemName: primarySymbol(presentation.primaryAction))
                        .font(.system(size: 13, weight: .semibold))
                        .frame(width: 16, height: 16)
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.circle)
                .controlSize(.large)
                .disabled(presentation.primaryAction != .stop && !canSend)
                .help(primaryHelp(presentation.primaryAction))
                .accessibilityLabel(primaryHelp(presentation.primaryAction))
            }
            .font(.caption)
        }
        .padding(14)
        .background(Color(nsColor: .textBackgroundColor))
        .overlay {
            RoundedRectangle(cornerRadius: WorkbenchComponentTheme.cornerRadius)
                .stroke(WorkbenchComponentTheme.border, lineWidth: 1)
        }
        .clipShape(RoundedRectangle(cornerRadius: WorkbenchComponentTheme.cornerRadius))
        .onAppear {
            focusDraftAfterLayout()
        }
        .onChange(of: isSending) { wasSending, isSending in
            if wasSending, !isSending {
                focusDraftAfterLayout()
            }
        }
    }

    private func focusDraftAfterLayout() {
        Task { @MainActor in
            await Task.yield()
            isDraftFocused = true
        }
    }

    private func primarySymbol(_ action: ChatComposerPrimaryAction) -> String {
        switch action {
        case .send: "arrow.up"
        case .stop: "stop.fill"
        case .retry: "arrow.clockwise"
        }
    }

    private func runtimeModeSymbol(_ mode: ChatRuntimeMode) -> String {
        switch mode {
        case .default: "checkmark.shield"
        case .plan: "doc.text.magnifyingglass"
        case .bypass: "exclamationmark.shield"
        }
    }

    private func primaryHelp(_ action: ChatComposerPrimaryAction) -> String {
        switch action {
        case .send: AppStrings.Chat.sendButton(locale)
        case .stop: AppStrings.Chat.stopButton(locale)
        case .retry: AppStrings.Chat.retryButton(locale)
        }
    }
}
