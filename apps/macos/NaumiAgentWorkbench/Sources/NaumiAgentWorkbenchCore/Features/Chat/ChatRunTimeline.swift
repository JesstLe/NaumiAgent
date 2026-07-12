import SwiftUI

struct ChatRunTimeline: View {
    let execution: ChatExecutionPresentation
    let locale: AppLocale
    let onPermissionDecision: (ChatPermissionDecision) -> Void
    let onReview: () -> Void

    var body: some View {
        SwiftUI.TimelineView(.periodic(from: .now, by: 1)) { timeline in
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 8) {
                    Text(AppStrings.Chat.executionStage(locale, stage: execution.stage))
                        .font(.system(size: 13, weight: .semibold))
                    Text(elapsed(now: timeline.date))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if execution.stage != .completed,
                       execution.stage != .failed,
                       execution.stage != .cancelled {
                        ProgressView().controlSize(.small)
                    }
                }

                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(execution.steps.enumerated()), id: \.element.id) { index, step in
                        stepRow(step, showsConnector: index < execution.steps.count - 1)
                    }
                }

                ForEach(execution.artifacts) { artifact in
                    ChatArtifactView(
                        artifact: artifact,
                        locale: locale,
                        onReview: onReview
                    )
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
                    permissionCard(permission)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func stepRow(_ step: ChatExecutionStep, showsConnector: Bool) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 4) {
                Image(systemName: stepSymbol(step))
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(stepColor(step.status))
                    .frame(width: 16, height: 16)
                if showsConnector {
                    Rectangle()
                        .fill(WorkbenchComponentTheme.border)
                        .frame(width: 1, height: 18)
                }
            }

            VStack(alignment: .leading, spacing: 3) {
                Text(localizedStepTitle(step))
                    .font(.system(size: 12, weight: .medium))
                if let detail = step.detail, !detail.isEmpty {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                }
            }
            .padding(.bottom, showsConnector ? 9 : 0)

            Spacer()
        }
    }

    private func permissionCard(_ permission: ChatPermissionRequest) -> some View {
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
                        onPermissionDecision(.bypass)
                    } label: {
                        Label(
                            AppStrings.Chat.bypassAndRun(locale),
                            systemImage: "exclamationmark.shield"
                        )
                    }
                    .buttonStyle(.bordered)
                    .tint(.orange)

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

    private func elapsed(now: Date) -> String {
        let finishedAt = execution.completedAt ?? now
        let seconds = max(Int(finishedAt.timeIntervalSince(execution.startedAt)), 0)
        return AppStrings.Chat.executionElapsed(locale, seconds: seconds)
    }

    private func localizedStepTitle(_ step: ChatExecutionStep) -> String {
        switch step.kind {
        case .analysis, .response, .linkedIssue:
            AppStrings.Chat.executionStep(locale, kind: step.kind)
        case .tool: step.title
        }
    }

    private func stepSymbol(_ step: ChatExecutionStep) -> String {
        switch step.status {
        case .completed: "checkmark.circle.fill"
        case .failed: "xmark.circle.fill"
        case .cancelled: "stop.circle.fill"
        case .awaitingApproval: "hand.raised.fill"
        case .running:
            switch step.kind {
            case .analysis: "sparkles"
            case .tool: "terminal"
            case .response: "text.cursor"
            case .linkedIssue: "checklist"
            }
        }
    }

    private func stepColor(_ status: ChatExecutionStepStatus) -> Color {
        switch status {
        case .completed: .green
        case .failed: .red
        case .cancelled: .secondary
        case .awaitingApproval: .orange
        case .running: .accentColor
        }
    }
}
