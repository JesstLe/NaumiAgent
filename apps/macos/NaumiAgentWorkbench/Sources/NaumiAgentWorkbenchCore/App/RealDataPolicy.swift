import Foundation

/// Decides whether presentation layers may enrich sparse live data with
/// deterministic design fixtures.
///
/// Real mode (`isPreviewFixture == false`) forbids fixture fillers: the UI must
/// show authoritative backend state or a polished empty state. Preview mode
/// (`isPreviewFixture == true`) keeps the rich reference screenshots intact.
public struct RealDataPolicy: Equatable, Sendable {
    public let isPreviewFixture: Bool

    public init(isPreviewFixture: Bool) {
        self.isPreviewFixture = isPreviewFixture
    }

    /// Fixture/design rows may be appended to keep screenshots dense.
    public var canUseDesignFillers: Bool { isPreviewFixture }

    /// Sparse or absent live data should surface as an empty state.
    public var shouldShowEmptyState: Bool { !isPreviewFixture }

    /// Rows sourced from fixtures should be visibly labeled as preview data.
    public var shouldLabelPreviewData: Bool { isPreviewFixture }

    /// Real-data policy: never append fixture rows, always show empty states.
    public static let real = RealDataPolicy(isPreviewFixture: false)

    /// Preview-fixture policy: design fillers are explicitly allowed.
    public static let preview = RealDataPolicy(isPreviewFixture: true)
}
