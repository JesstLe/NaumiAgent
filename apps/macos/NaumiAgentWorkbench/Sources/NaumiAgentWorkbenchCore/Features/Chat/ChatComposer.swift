import SwiftUI

struct ChatComposer: View {
    @Binding var draft: String
    @Binding var mode: ChatComposerMode
    @Binding var selectedMissionID: String
    @Binding var issueTitle: String
    @Binding var issueDescription: String
    @Binding var acceptanceCriteria: String
    @Binding var parallelMode: String
    @Binding var riskLevel: String

    let missions: [MissionDTO]
    let locale: AppLocale
    let isSending: Bool
    let errorMessage: String?
    let disabledReason: String?
    let canSend: Bool
    let onPrimaryAction: () -> Void

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
    }

    private func primarySymbol(_ action: ChatComposerPrimaryAction) -> String {
        switch action {
        case .send: "arrow.up"
        case .stop: "stop.fill"
        case .retry: "arrow.clockwise"
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
