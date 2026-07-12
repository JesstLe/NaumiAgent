import SwiftUI

struct ChatInspector: View {
    let appState: AppState

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                inspectorSection(AppStrings.Chat.environmentSection(appState.locale)) {
                    valueRow(
                        icon: "server.rack",
                        title: appState.locale == .zhCN ? "本地服务" : "Local service",
                        value: appState.daemonStatus?.status ?? appState.connectionState.rawValue
                    )
                    valueRow(
                        icon: "folder",
                        title: appState.locale == .zhCN ? "工作区" : "Workspace",
                        value: workspaceName
                    )
                    if let branch = activeWorktree?.branch, !branch.isEmpty {
                        valueRow(icon: "point.3.connected.trianglepath.dotted", title: "Git", value: branch)
                    }
                }

                Divider()

                inspectorSection(AppStrings.Chat.changesSection(appState.locale)) {
                    let dirtyFiles = appState.worktrees.reduce(0) { $0 + $1.dirtyFiles }
                    valueRow(
                        icon: "doc.badge.ellipsis",
                        title: appState.locale == .zhCN ? "修改文件" : "Changed files",
                        value: "\(dirtyFiles)"
                    )
                }

                Divider()

                inspectorSection(AppStrings.Chat.workspaceSection(appState.locale)) {
                    if let worktree = activeWorktree {
                        valueRow(icon: "externaldrive", title: worktree.name, value: worktree.status)
                        valueRow(
                            icon: "arrow.up.right",
                            title: appState.locale == .zhCN ? "领先提交" : "Commits ahead",
                            value: "\(worktree.commitsAhead)"
                        )
                    } else {
                        emptyRow(appState.locale == .zhCN ? "暂无工作区" : "No worktree")
                    }
                }

                Divider()

                inspectorSection(AppStrings.Chat.linkedObjectsSection(appState.locale)) {
                    if let mission = selectedMission {
                        valueRow(icon: "scope", title: "Mission", value: mission.title)
                    }
                    valueRow(
                        icon: "checklist",
                        title: appState.locale == .zhCN ? "开放问题" : "Open issues",
                        value: "\(appState.issues.count)"
                    )
                    valueRow(
                        icon: "checkmark.shield",
                        title: appState.locale == .zhCN ? "待审批" : "Approvals",
                        value: "\(appState.approvals.count)"
                    )
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

    private func emptyRow(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}
