import Foundation

/// Shared layout constants for fixed workbench navigation pages.
public struct WorkbenchPageLayout: Equatable, Sendable {
    public let railWidth: Double
    public let inspectorWidth: Double
    public let contentHorizontalPadding: Double
    public let columnGap: Double
    public let primaryColumnWidth: Double
    public let secondaryColumnWidth: Double

    public static let worktrees = WorkbenchPageLayout(
        railWidth: 286,
        inspectorWidth: 306,
        contentHorizontalPadding: 36,
        columnGap: 14,
        primaryColumnWidth: 360,
        secondaryColumnWidth: 300
    )

    public var operationsGridWidth: Double {
        primaryColumnWidth + columnGap + secondaryColumnWidth + contentHorizontalPadding
    }

    public func centralAvailableWidth(in windowWidth: Double) -> Double {
        windowWidth - railWidth - inspectorWidth
    }
}
