import SwiftUI

struct ChatConversationView<Composer: View>: View {
    let messages: [ChatMessageDTO]
    let execution: ChatExecutionPresentation?
    let locale: AppLocale
    let onPermissionDecision: (ChatPermissionDecision) -> Void
    @ViewBuilder let composer: () -> Composer

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { reader in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 24) {
                        if messages.isEmpty {
                            ContentUnavailableView {
                                Label(
                                    AppStrings.Chat.emptyMessages(locale),
                                    systemImage: "bubble.left.and.bubble.right"
                                )
                            }
                            .frame(maxWidth: .infinity, minHeight: 520)
                        } else {
                            ForEach(messages, id: \.id) { message in
                                ChatMessageRow(
                                    message: message,
                                    locale: locale,
                                    showsLinkedIssue: hasLinkedIssue(message)
                                )
                                .id(message.id)
                            }
                        }

                        if let execution {
                            executionTimeline(execution)
                                .id(execution.id)
                        }
                    }
                    .frame(maxWidth: 760)
                    .padding(.horizontal, 28)
                    .padding(.top, 28)
                    .padding(.bottom, 136)
                    .frame(maxWidth: .infinity)
                }
                .onChange(of: messages.count) { _, _ in
                    guard let lastID = messages.last?.id else { return }
                    withAnimation(.easeOut(duration: 0.18)) {
                        reader.scrollTo(lastID, anchor: .bottom)
                    }
                }
                .onChange(of: execution) { _, execution in
                    guard let execution else { return }
                    withAnimation(.easeOut(duration: 0.18)) {
                        reader.scrollTo(execution.id, anchor: .bottom)
                    }
                }
            }
            .overlay(alignment: .bottom) {
                composer()
                    .frame(maxWidth: 720)
                    .padding(.horizontal, 22)
                    .padding(.bottom, 16)
            }
        }
        .background(WorkbenchComponentTheme.surface(.canvas))
    }

    private func executionTimeline(_ execution: ChatExecutionPresentation) -> some View {
        SwiftUI.TimelineView(.periodic(from: .now, by: 1)) { timeline in
            HStack(alignment: .top, spacing: 14) {
                VStack(spacing: 5) {
                    Circle()
                        .fill(executionColor(execution.stage))
                        .frame(width: 8, height: 8)
                    Rectangle()
                        .fill(WorkbenchComponentTheme.border)
                        .frame(width: 1)
                }
                .frame(width: 14)

                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 8) {
                        Text(AppStrings.Chat.executionStage(locale, stage: execution.stage))
                            .font(.system(size: 13, weight: .semibold))
                        Text(elapsed(execution, now: timeline.date))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        if execution.stage != .completed, execution.stage != .failed {
                            ProgressView().controlSize(.small)
                        }
                    }

                    if let toolName = execution.activeToolName {
                        Label(
                            AppStrings.Chat.executionTool(locale, toolName: toolName),
                            systemImage: "terminal"
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }

                    if let summary = execution.toolResultSummary {
                        Text(summary)
                            .font(.system(size: 13))
                            .lineSpacing(2)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    if !execution.partialResponse.isEmpty {
                        Text(execution.partialResponse)
                            .font(.system(size: 14))
                            .lineSpacing(3)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    if let failure = execution.failureMessage {
                        Label(failure, systemImage: "exclamationmark.triangle.fill")
                            .font(.callout)
                            .foregroundStyle(.red)
                    }

                    if let permission = execution.permission {
                        permissionCard(permission, execution: execution)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func permissionCard(
        _ permission: ChatPermissionRequest,
        execution: ChatExecutionPresentation
    ) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Label(
                    AppStrings.Chat.permissionRequired(locale),
                    systemImage: "hand.raised.fill"
                )
                .font(.system(size: 12, weight: .semibold))
                Spacer()
                Text(AppStrings.Chat.permissionRisk(locale, level: permission.riskLevel))
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            Text(permission.reason)
                .font(.caption)
                .foregroundStyle(.secondary)

            if execution.isResolvingPermission {
                HStack(spacing: 7) {
                    ProgressView().controlSize(.small)
                    Text(AppStrings.Chat.resolvingApproval(locale))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else {
                HStack(spacing: 8) {
                    Button {
                        onPermissionDecision(.allow)
                    } label: {
                        Label(AppStrings.Chat.allowOnce(locale), systemImage: "checkmark")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.green)

                    Button {
                        onPermissionDecision(.deny)
                    } label: {
                        Label(AppStrings.Chat.deny(locale), systemImage: "xmark")
                    }
                    .buttonStyle(.bordered)
                    .tint(.red)
                }
                .controlSize(.small)
            }
        }
        .padding(12)
        .workbenchSurface(.group)
    }

    private func elapsed(_ execution: ChatExecutionPresentation, now: Date) -> String {
        let finishedAt = execution.completedAt ?? now
        let seconds = max(Int(finishedAt.timeIntervalSince(execution.startedAt)), 0)
        return AppStrings.Chat.executionElapsed(locale, seconds: seconds)
    }

    private func executionColor(_ stage: ChatExecutionStage) -> Color {
        switch stage {
        case .awaitingApproval: .orange
        case .failed: .red
        case .completed: .green
        case .composing: .blue
        default: .accentColor
        }
    }

    private func hasLinkedIssue(_ message: ChatMessageDTO) -> Bool {
        guard case .object? = message.metadata["workbench_issue"] else { return false }
        return true
    }
}
