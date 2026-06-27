import Foundation

/// Main navigation routes for the workbench shell.
public enum AppRoute: String, CaseIterable, Identifiable, Hashable, Sendable {
    case dashboard
    case taskMarket
    case worktrees
    case reviews
    case timeline
    case settings

    public var id: String { rawValue }

    public static let topNavigationRoutes: [AppRoute] = [
        .dashboard,
        .taskMarket,
        .worktrees,
        .reviews,
        .timeline,
        .settings
    ]

    /// Localized display name. 默认中文，en-US fallback。
    public func displayName(locale: AppLocale) -> String {
        switch self {
        case .dashboard:
            return AppStrings.Navigation.dashboard(locale)
        case .taskMarket:
            return AppStrings.Navigation.taskMarket(locale)
        case .worktrees:
            return AppStrings.Navigation.worktrees(locale)
        case .reviews:
            return AppStrings.Navigation.reviews(locale)
        case .timeline:
            return AppStrings.Navigation.timeline(locale)
        case .settings:
            return AppStrings.Navigation.settings(locale)
        }
    }

    public var systemImage: String {
        switch self {
        case .dashboard:
            return "square.grid.2x2"
        case .taskMarket:
            return "cart"
        case .worktrees:
            return "folder"
        case .reviews:
            return "checkmark.shield"
        case .timeline:
            return "clock"
        case .settings:
            return "gearshape"
        }
    }
}
