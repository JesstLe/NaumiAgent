import CoreGraphics
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

/// Scales a fixed visual design into the current window while keeping all columns visible.
public struct WorkbenchScaledPageLayout: Equatable, Sendable {
    public let baseWidth: Double
    public let baseHeight: Double

    public static let dashboard = WorkbenchScaledPageLayout(baseWidth: 1440, baseHeight: 858)
    public static let reviews = WorkbenchScaledPageLayout(baseWidth: 1440, baseHeight: 858)

    public init(baseWidth: Double, baseHeight: Double) {
        self.baseWidth = baseWidth
        self.baseHeight = baseHeight
    }

    public func scale(for availableWidth: Double) -> Double {
        guard baseWidth > 0 else { return 1 }
        return max(0.1, availableWidth / baseWidth)
    }

    public func scale(for availableSize: CGSize) -> Double {
        guard baseWidth > 0, baseHeight > 0 else { return 1 }
        let widthScale = availableSize.width / baseWidth
        let heightScale = availableSize.height / baseHeight
        return max(0.1, min(widthScale, heightScale))
    }

    public func scaledSize(for availableWidth: Double) -> CGSize {
        let scale = scale(for: availableWidth)
        return CGSize(width: baseWidth * scale, height: baseHeight * scale)
    }

    public func scaledSize(for availableSize: CGSize) -> CGSize {
        let scale = scale(for: availableSize)
        return CGSize(width: baseWidth * scale, height: baseHeight * scale)
    }

    public func viewport(for availableSize: CGSize) -> WorkbenchScaledViewport {
        let scale = scale(for: availableSize)
        let scaledSize = CGSize(width: baseWidth * scale, height: baseHeight * scale)

        return WorkbenchScaledViewport(
            scale: scale,
            scaledSize: scaledSize,
            containerSize: CGSize(
                width: availableSize.width,
                height: availableSize.height
            ),
            showsVerticalScroll: scaledSize.height > availableSize.height
        )
    }
}

public struct WorkbenchScaledViewport: Equatable, Sendable {
    public let scale: Double
    public let scaledSize: CGSize
    public let containerSize: CGSize
    public let showsVerticalScroll: Bool
}
