import SwiftUI

struct ChatIssueDraftPanel: View {
    @Binding var title: String
    @Binding var description: String
    @Binding var acceptanceCriteria: String
    @Binding var parallelMode: String
    @Binding var riskLevel: String
    let locale: AppLocale

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Divider()

            HStack {
                Label(
                    AppStrings.TaskMarket.createIssueSectionTitle(locale),
                    systemImage: "checklist"
                )
                .font(.system(size: 12, weight: .semibold))
                Spacer()
                Text(riskLabel)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            TextField(AppStrings.Chat.issueTitle(locale), text: $title)
                .textFieldStyle(.plain)
                .padding(9)
                .background(WorkbenchComponentTheme.surface(.group))
                .clipShape(RoundedRectangle(cornerRadius: 6))

            HStack(alignment: .top, spacing: 10) {
                TextField(
                    AppStrings.Chat.issueDescription(locale),
                    text: $description,
                    axis: .vertical
                )
                .lineLimit(2...4)

                TextField(
                    AppStrings.Chat.acceptanceCriteria(locale),
                    text: $acceptanceCriteria,
                    axis: .vertical
                )
                .lineLimit(2...4)
            }
            .textFieldStyle(.plain)
            .padding(9)
            .background(WorkbenchComponentTheme.surface(.group))
            .clipShape(RoundedRectangle(cornerRadius: 6))

            HStack(spacing: 14) {
                Picker(AppStrings.Chat.parallelMode(locale), selection: $parallelMode) {
                    ForEach(["exclusive", "cooperative", "competitive", "exploratory"], id: \.self) {
                        Text(modeLabel($0)).tag($0)
                    }
                }
                .controlSize(.small)

                Picker(AppStrings.Chat.riskLevel(locale), selection: $riskLevel) {
                    ForEach(["low", "medium", "high", "critical"], id: \.self) {
                        Text(localizedRisk($0)).tag($0)
                    }
                }
                .controlSize(.small)
            }
        }
    }

    private var riskLabel: String {
        "\(AppStrings.Chat.riskLevel(locale)): \(localizedRisk(riskLevel))"
    }

    private func modeLabel(_ value: String) -> String {
        guard locale == .zhCN else { return value.capitalized }
        return switch value {
        case "exclusive": "独占"
        case "cooperative": "协作"
        case "competitive": "竞争"
        default: "探索"
        }
    }

    private func localizedRisk(_ value: String) -> String {
        guard locale == .zhCN else { return value.capitalized }
        return switch value {
        case "low": "低"
        case "medium": "中"
        case "high": "高"
        default: "严重"
        }
    }
}
