import Foundation
import CoreGraphics

/// Presentation constants for the macOS workbench shell.
/// The app relies on native macOS window controls instead of drawing fake ones.
public struct WorkbenchShellPresentation: Equatable, Sendable {
    public let showsSyntheticWindowControls: Bool
    public let placesNavigationBelowTitleBar: Bool
    public let leadingContentInset: Double
    public let topNavigationHeight: Double
    public let globalStatusHeight: Double
    public let designCanvasWidth: Double
    public let minimumWindowWidth: Double
    public let minimumWindowHeight: Double
    public let navigationRoutes: [AppRoute]
    public let nativeWindowTitle: String

    public init(
        showsSyntheticWindowControls: Bool = false,
        placesNavigationBelowTitleBar: Bool = true,
        leadingContentInset: Double = 14,
        topNavigationHeight: Double = 42,
        globalStatusHeight: Double = 0,
        designCanvasWidth: Double = 1440,
        minimumWindowWidth: Double = 1180,
        minimumWindowHeight: Double = 760,
        navigationRoutes: [AppRoute] = AppRoute.topNavigationRoutes,
        nativeWindowTitle: String = ""
    ) {
        self.showsSyntheticWindowControls = showsSyntheticWindowControls
        self.placesNavigationBelowTitleBar = placesNavigationBelowTitleBar
        self.leadingContentInset = leadingContentInset
        self.topNavigationHeight = topNavigationHeight
        self.globalStatusHeight = globalStatusHeight
        self.designCanvasWidth = designCanvasWidth
        self.minimumWindowWidth = minimumWindowWidth
        self.minimumWindowHeight = minimumWindowHeight
        self.navigationRoutes = navigationRoutes
        self.nativeWindowTitle = nativeWindowTitle
    }

    public func navigationScale(for availableWidth: Double) -> Double {
        guard designCanvasWidth > 0 else { return 1 }
        return max(0.1, availableWidth / designCanvasWidth)
    }

    public func navigationScale(
        for availableSize: CGSize,
        pageLayout: WorkbenchScaledPageLayout
    ) -> Double {
        guard designCanvasWidth > 0, pageLayout.baseHeight > 0 else {
            return 1
        }

        let widthScale = Double(availableSize.width) / designCanvasWidth
        let heightScale = Double(availableSize.height) / (topNavigationHeight + pageLayout.baseHeight)
        return max(0.1, min(widthScale, heightScale))
    }

    public func scaledTopNavigationHeight(for availableWidth: Double) -> Double {
        topNavigationHeight * navigationScale(for: availableWidth)
    }

    public func scaledTopNavigationHeight(
        for availableSize: CGSize,
        pageLayout: WorkbenchScaledPageLayout
    ) -> Double {
        topNavigationHeight * navigationScale(
            for: availableSize,
            pageLayout: pageLayout
        )
    }

    public func shellViewport(
        for availableSize: CGSize,
        pageLayout: WorkbenchScaledPageLayout
    ) -> WorkbenchShellViewport {
        let scale = navigationScale(for: availableSize, pageLayout: pageLayout)
        let navigationHeight = topNavigationHeight * scale
        let pageHeight = pageLayout.baseHeight * scale

        return WorkbenchShellViewport(
            scale: scale,
            navigationHeight: navigationHeight,
            pageHeight: pageHeight,
            scaledSize: CGSize(
                width: designCanvasWidth * scale,
                height: navigationHeight + pageHeight
            ),
            containerSize: availableSize
        )
    }
}

public struct WorkbenchShellViewport: Equatable, Sendable {
    public let scale: Double
    public let navigationHeight: Double
    public let pageHeight: Double
    public let scaledSize: CGSize
    public let containerSize: CGSize
}

public struct WorkbenchChromePresentation: Equatable, Sendable {
    public let brandTitle: String
    public let showsBrandMark: Bool

    public init(
        brandTitle: String = "NaumiAgent Workbench",
        showsBrandMark: Bool = true
    ) {
        self.brandTitle = brandTitle
        self.showsBrandMark = showsBrandMark
    }
}
