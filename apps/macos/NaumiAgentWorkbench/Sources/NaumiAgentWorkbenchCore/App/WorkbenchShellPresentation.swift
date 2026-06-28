import Foundation

/// Presentation constants for the macOS workbench shell.
/// The app relies on native macOS window controls instead of drawing fake ones.
public struct WorkbenchShellPresentation: Equatable, Sendable {
    public let showsSyntheticWindowControls: Bool
    public let placesNavigationBelowTitleBar: Bool
    public let leadingContentInset: Double
    public let topNavigationHeight: Double
    public let globalStatusHeight: Double
    public let minimumWindowWidth: Double
    public let minimumWindowHeight: Double
    public let navigationRoutes: [AppRoute]

    public init(
        showsSyntheticWindowControls: Bool = false,
        placesNavigationBelowTitleBar: Bool = true,
        leadingContentInset: Double = 14,
        topNavigationHeight: Double = 42,
        globalStatusHeight: Double = 0,
        minimumWindowWidth: Double = 1180,
        minimumWindowHeight: Double = 760,
        navigationRoutes: [AppRoute] = AppRoute.topNavigationRoutes
    ) {
        self.showsSyntheticWindowControls = showsSyntheticWindowControls
        self.placesNavigationBelowTitleBar = placesNavigationBelowTitleBar
        self.leadingContentInset = leadingContentInset
        self.topNavigationHeight = topNavigationHeight
        self.globalStatusHeight = globalStatusHeight
        self.minimumWindowWidth = minimumWindowWidth
        self.minimumWindowHeight = minimumWindowHeight
        self.navigationRoutes = navigationRoutes
    }
}
