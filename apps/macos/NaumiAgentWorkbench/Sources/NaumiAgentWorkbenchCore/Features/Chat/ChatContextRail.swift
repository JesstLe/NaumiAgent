import SwiftUI

public struct ChatContextRail: View {
    let sessionID: String?
    let connectionText: String
    let missions: [MissionDTO]
    @Binding var selectedMissionID: String
    let issues: [IssueDTO]
    let tasks: [TaskDTO]
    let runs: [ChatRunDTO]
    let locale: AppLocale
    let onIssueSelect: (String) -> Void
    let onRunSelect: (String) -> Void

    public init(
        sessionID: String?,
        connectionText: String,
        missions: [MissionDTO],
        selectedMissionID: Binding<String>,
        issues: [IssueDTO],
        tasks: [TaskDTO] = [],
        runs: [ChatRunDTO] = [],
        locale: AppLocale,
        onIssueSelect: @escaping (String) -> Void = { _ in },
        onRunSelect: @escaping (String) -> Void = { _ in }
    ) {
        self.sessionID = sessionID
        self.connectionText = connectionText
        self.missions = missions
        _selectedMissionID = selectedMissionID
        self.issues = issues
        self.tasks = tasks
        self.runs = runs
        self.locale = locale
        self.onIssueSelect = onIssueSelect
        self.onRunSelect = onRunSelect
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text(AppStrings.Chat.title(locale))
                .font(.title3.weight(.semibold))

            group(AppStrings.Chat.sessionSection(locale)) {
                Text(sessionID ?? "session")
                    .font(.callout.weight(.medium))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Label(connectionText, systemImage: "circle.fill")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .symbolRenderingMode(.palette)
            }

            group(AppStrings.Chat.missionSection(locale)) {
                if missions.isEmpty {
                    Text(AppStrings.Chat.noMission(locale))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    Picker("", selection: $selectedMissionID) {
                        ForEach(missions, id: \.id) { mission in
                            Text(mission.title).tag(mission.id)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.menu)
                }
            }

            group(AppStrings.GlobalStatus.openIssues(locale)) {
                let titles = Dictionary(uniqueKeysWithValues: tasks.map { ($0.id, $0.subject) })
                let summaries = ChatPresentation.issueSummaries(
                    from: issues,
                    taskTitlesByID: titles
                )
                if summaries.isEmpty {
                    Text(locale == .zhCN ? "暂无开放问题" : "No open issues")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(summaries.prefix(6)) { issue in
                        Button {
                            onIssueSelect(issue.id)
                        } label: {
                            HStack(spacing: 8) {
                                Circle()
                                    .fill(riskColor(issue.riskLevel))
                                    .frame(width: 6, height: 6)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(issue.title)
                                        .font(.system(size: 12, weight: .medium))
                                        .lineLimit(1)
                                    Text(issue.status)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text(issue.riskLevel)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .padding(.vertical, 3)
                    }
                }
            }

            group(AppStrings.Chat.recentRuns(locale)) {
                if runs.isEmpty {
                    Text(locale == .zhCN ? "暂无运行记录" : "No runs yet")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(runs.prefix(4)) { run in
                        Button {
                            onRunSelect(run.id)
                        } label: {
                            HStack(spacing: 8) {
                                Image(systemName: run.status == "completed"
                                    ? "checkmark.circle.fill"
                                    : "clock")
                                    .foregroundStyle(run.status == "completed" ? .green : .secondary)
                                    .frame(width: 14)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(run.steps.last?.summary ?? run.status)
                                        .font(.system(size: 12, weight: .medium))
                                        .lineLimit(1)
                                    Text(run.status)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .padding(.vertical, 2)
                    }
                }
            }

            Spacer()
        }
        .padding(18)
        .background(WorkbenchComponentTheme.surface(.rail))
    }

    @ViewBuilder
    private func group<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func riskColor(_ risk: String) -> Color {
        switch ChatPresentation.riskColorName(risk) {
        case "red": .red
        case "orange": .orange
        case "yellow": .yellow
        default: .green
        }
    }
}
