import Foundation

/// Presentation constants for the macOS workbench shell.
/// The app relies on native macOS window controls instead of drawing fake ones.
public struct WorkbenchShellPresentation: Equatable, Sendable {
    public let showsSyntheticWindowControls: Bool
    public let leadingContentInset: Double
    public let navigationRoutes: [AppRoute]

    public init(
        showsSyntheticWindowControls: Bool = false,
        leadingContentInset: Double = 78,
        navigationRoutes: [AppRoute] = AppRoute.topNavigationRoutes
    ) {
        self.showsSyntheticWindowControls = showsSyntheticWindowControls
        self.leadingContentInset = leadingContentInset
        self.navigationRoutes = navigationRoutes
    }
}
