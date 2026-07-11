import SwiftUI

public enum WorkbenchSurfaceStyle: Equatable, Sendable {
    case canvas
    case rail
    case group
    case selectedRow
}

public enum WorkbenchComponentTheme {
    public static let cornerRadius: CGFloat = 8
    public static let compactCornerRadius: CGFloat = 6
    public static let selectionStripeWidth: CGFloat = 3

    public static var border: Color {
        Color(nsColor: .separatorColor).opacity(0.72)
    }

    public static var connector: Color {
        Color.accentColor.opacity(0.20)
    }

    public static func surface(_ style: WorkbenchSurfaceStyle) -> Color {
        switch style {
        case .canvas:
            return Color(nsColor: .windowBackgroundColor)
        case .rail:
            return Color(nsColor: .controlBackgroundColor).opacity(0.66)
        case .group:
            return Color(nsColor: .controlBackgroundColor).opacity(0.48)
        case .selectedRow:
            return Color.accentColor.opacity(0.10)
        }
    }
}

public struct WorkbenchSurface: ViewModifier {
    let style: WorkbenchSurfaceStyle
    let radius: CGFloat
    let showsBorder: Bool

    public func body(content: Content) -> some View {
        content
            .background(WorkbenchComponentTheme.surface(style))
            .overlay {
                if showsBorder {
                    RoundedRectangle(cornerRadius: radius)
                        .stroke(WorkbenchComponentTheme.border, lineWidth: 1)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: radius))
    }
}

public struct WorkbenchListRow: ViewModifier {
    let isSelected: Bool
    let accent: Color

    public func body(content: Content) -> some View {
        content
            .background(
                WorkbenchComponentTheme.surface(isSelected ? .selectedRow : .group)
            )
            .overlay(alignment: .leading) {
                if isSelected {
                    Rectangle()
                        .fill(accent)
                        .frame(width: WorkbenchComponentTheme.selectionStripeWidth)
                }
            }
            .overlay {
                RoundedRectangle(cornerRadius: WorkbenchComponentTheme.cornerRadius)
                    .stroke(
                        isSelected ? Color.accentColor.opacity(0.42) : WorkbenchComponentTheme.border,
                        lineWidth: 1
                    )
            }
            .clipShape(RoundedRectangle(cornerRadius: WorkbenchComponentTheme.cornerRadius))
    }
}

public struct WorkbenchStatusChip: View {
    let text: String
    let color: Color

    public init(text: String, color: Color) {
        self.text = text
        self.color = color
    }

    public var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(color)
                .frame(width: 5, height: 5)
            Text(text)
                .lineLimit(1)
        }
        .font(.system(size: 11, weight: .medium))
        .foregroundStyle(color)
        .padding(.horizontal, 7)
        .padding(.vertical, 3)
        .background(color.opacity(0.10))
        .clipShape(RoundedRectangle(cornerRadius: WorkbenchComponentTheme.compactCornerRadius))
    }
}

public struct WorkbenchIconButton: View {
    let systemImage: String
    let help: String
    let action: () -> Void

    public init(systemImage: String, help: String, action: @escaping () -> Void) {
        self.systemImage = systemImage
        self.help = help
        self.action = action
    }

    public var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .frame(width: 14, height: 14)
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .help(help)
    }
}

public extension View {
    func workbenchSurface(
        _ style: WorkbenchSurfaceStyle = .group,
        radius: CGFloat = WorkbenchComponentTheme.cornerRadius,
        showsBorder: Bool = true
    ) -> some View {
        modifier(WorkbenchSurface(style: style, radius: radius, showsBorder: showsBorder))
    }

    func workbenchListRow(isSelected: Bool, accent: Color = .accentColor) -> some View {
        modifier(WorkbenchListRow(isSelected: isSelected, accent: accent))
    }
}
