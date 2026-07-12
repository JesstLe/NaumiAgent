import SwiftUI

struct ChatArtifactView: View {
    let artifact: ChatArtifactPresentation
    let locale: AppLocale
    let onReview: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(color)
                .frame(width: 20, height: 20)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(title)
                        .font(.system(size: 12, weight: .semibold))
                    Spacer()
                    Text(artifact.status)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                if !artifact.summary.isEmpty {
                    Text(artifact.summary)
                        .font(.system(size: 12))
                        .lineSpacing(2)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if artifact.kind == .fileChange {
                    Button(AppStrings.Navigation.reviews(locale), action: onReview)
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }
            }
        }
        .padding(11)
        .workbenchSurface(.group)
    }

    private var title: String {
        switch artifact.kind {
        case .command: locale == .zhCN ? "命令结果" : "Command result"
        case .task: locale == .zhCN ? "任务" : "Task"
        case .validation: locale == .zhCN ? "验证结果" : "Validation"
        case .fileChange: locale == .zhCN ? "文件变更" : "File changes"
        case .subagent: locale == .zhCN ? "子 Agent 结果" : "Subagent result"
        }
    }

    private var symbol: String {
        switch artifact.kind {
        case .command: "terminal"
        case .task: "checklist"
        case .validation: "checkmark.seal"
        case .fileChange: "doc.badge.ellipsis"
        case .subagent: "person.2"
        }
    }

    private var color: Color {
        artifact.status == "success" ? .green : .accentColor
    }
}
