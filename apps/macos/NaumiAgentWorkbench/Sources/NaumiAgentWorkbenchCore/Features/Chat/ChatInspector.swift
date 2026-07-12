import SwiftUI

struct ChatInspector: View {
    let appState: AppState
    let onReview: () -> Void
    let onMission: (String) -> Void
    let onIssues: () -> Void
    let onSource: (ChatSourceReferenceDTO) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                inspectorSection(AppStrings.Chat.environmentSection(appState.locale)) {
                    valueRow(
                        icon: "server.rack",
                        title: AppStrings.Chat.localService(appState.locale),
                        value: appState.daemonStatus?.status ?? appState.connectionState.rawValue
                    )
                    valueRow(
                        icon: "folder",
                        title: AppStrings.Chat.workspace(appState.locale),
                        value: environment?.workspaceName ?? workspaceName
                    )
                    if let git = environment?.git, git.available {
                        valueRow(
                            icon: "point.3.connected.trianglepath.dotted",
                            title: "Git",
                            value: git.branch
                        )
                    }
                }

                Divider()

                inspectorSection(AppStrings.Chat.changesSection(appState.locale)) {
                    if let git = environment?.git, git.available {
                        actionRow(
                            icon: "doc.badge.ellipsis",
                            title: AppStrings.Chat.changedFiles(appState.locale),
                            value: "\(git.changedFiles)",
                            action: onReview
                        )
                        valueRow(
                            icon: "plus.forwardslash.minus",
                            title: AppStrings.Chat.lineChanges(appState.locale),
                            value: "+\(git.additions)  -\(git.deletions)"
                        )
                    } else {
                        emptyRow(AppStrings.Chat.notGitWorkspace(appState.locale))
                    }
                }

                Divider()

                inspectorSection(AppStrings.Chat.workspaceSection(appState.locale)) {
                    if let worktree = activeWorktree {
                        valueRow(icon: "externaldrive", title: worktree.name, value: worktree.status)
                        valueRow(
                            icon: "arrow.up.right",
                            title: AppStrings.Chat.commitsAhead(appState.locale),
                            value: "\(environment?.git.ahead ?? worktree.commitsAhead)"
                        )
                        if let behind = environment?.git.behind, behind > 0 {
                            valueRow(
                                icon: "arrow.down.left",
                                title: AppStrings.Chat.commitsBehind(appState.locale),
                                value: "\(behind)"
                            )
                        }
                    } else {
                        emptyRow(AppStrings.Chat.noWorktree(appState.locale))
                    }
                }

                Divider()

                inspectorSection(AppStrings.Chat.backgroundProcessesSection(appState.locale)) {
                    if let processes = environment?.processes, !processes.isEmpty {
                        ForEach(processes.prefix(4)) { process in
                            valueRow(
                                icon: process.status == "running" ? "terminal.fill" : "terminal",
                                title: process.command,
                                value: process.pid.map(String.init) ?? process.status
                            )
                        }
                    } else {
                        emptyRow(AppStrings.Chat.noBackgroundProcesses(appState.locale))
                    }
                }

                Divider()

                inspectorSection(AppStrings.Chat.linkedObjectsSection(appState.locale)) {
                    if let mission = selectedMission {
                        actionRow(
                            icon: "scope",
                            title: "Mission",
                            value: mission.title,
                            action: { onMission(mission.id) }
                        )
                    }
                    actionRow(
                        icon: "checklist",
                        title: AppStrings.Chat.openIssuesLabel(appState.locale),
                        value: "\(appState.issues.count)",
                        action: onIssues
                    )
                    valueRow(
                        icon: "checkmark.shield",
                        title: AppStrings.Chat.approvalsLabel(appState.locale),
                        value: "\(appState.approvals.count)"
                    )
                    if let run = appState.selectedChatRun {
                        valueRow(icon: "clock", title: run.id, value: run.status)
                    }
                }

                Divider()

                inspectorSection(AppStrings.Chat.sourcesSection(appState.locale)) {
                    if let sources = environment?.sources, !sources.isEmpty {
                        ForEach(sources.prefix(5)) { source in
                            actionRow(
                                icon: "doc.text",
                                title: source.title,
                                value: source.path,
                                action: { onSource(source) }
                            )
                        }
                    } else {
                        emptyRow(AppStrings.Chat.noSources(appState.locale))
                    }
                }
            }
            .workbenchSurface(.group)
            .padding(14)
        }
        .background(WorkbenchComponentTheme.surface(.rail))
    }

    private var activeWorktree: WorktreeDTO? {
        appState.selectedWorktree ?? appState.worktrees.first
    }

    private var environment: ChatEnvironmentDTO? {
        appState.chatEnvironment
    }

    private var selectedMission: MissionDTO? {
        appState.selectedMission ?? appState.missions.first
    }

    private var workspaceName: String {
        let name = appState.daemonStatus?.workspaceName ?? ""
        if !name.isEmpty { return name }
        return appState.selectedWorkspace ?? "-"
    }

    @ViewBuilder
    private func inspectorSection<Content: View>(
        _ title: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 11) {
            Text(title)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.secondary)
            content()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func valueRow(icon: String, title: String, value: String) -> some View {
        HStack(spacing: 9) {
            Image(systemName: icon)
                .frame(width: 16)
                .foregroundStyle(.secondary)
            Text(title)
                .font(.system(size: 12, weight: .medium))
            Spacer(minLength: 8)
            Text(value)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    private func actionRow(
        icon: String,
        title: String,
        value: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            valueRow(icon: icon, title: title, value: value)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func emptyRow(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}
