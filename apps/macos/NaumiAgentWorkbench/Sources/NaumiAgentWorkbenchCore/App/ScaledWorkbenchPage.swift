import SwiftUI

/// Hosts fixed-design workbench pages in a window-width driven scale transform.
/// The route body owns vertical overflow; horizontal overflow is treated as a bug.
public struct ScaledWorkbenchPage<Content: View>: View {
    private let layout: WorkbenchScaledPageLayout
    private let content: Content

    public init(
        layout: WorkbenchScaledPageLayout,
        @ViewBuilder content: () -> Content
    ) {
        self.layout = layout
        self.content = content()
    }

    public var body: some View {
        GeometryReader { proxy in
            let viewport = layout.viewport(for: proxy.size)
            let scale = CGFloat(viewport.scale)

            ScrollView(.vertical, showsIndicators: viewport.showsVerticalScroll) {
                content
                    .frame(
                        width: layout.baseWidth,
                        height: layout.baseHeight,
                        alignment: .topLeading
                    )
                    .scaleEffect(scale, anchor: .topLeading)
                    .frame(
                        width: viewport.scaledSize.width,
                        height: viewport.scaledSize.height,
                        alignment: .topLeading
                    )
                .frame(
                    width: viewport.containerSize.width,
                    alignment: .topLeading
                )
            }
            .frame(width: proxy.size.width, height: proxy.size.height, alignment: .topLeading)
        }
    }
}
