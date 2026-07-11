import SwiftUI

public struct NaumiBrandMark: View {
    private let size: CGFloat

    public init(size: CGFloat = 20) {
        self.size = size
    }

    public var body: some View {
        Canvas { context, canvasSize in
            let stroke = max(1.5, canvasSize.width * 0.09)
            let junction = CGPoint(x: canvasSize.width * 0.50, y: canvasSize.height * 0.74)
            let nodes = [
                (CGPoint(x: canvasSize.width * 0.19, y: canvasSize.height * 0.30), Color.blue),
                (CGPoint(x: canvasSize.width * 0.50, y: canvasSize.height * 0.18), Color.teal),
                (CGPoint(x: canvasSize.width * 0.81, y: canvasSize.height * 0.30), Color.green),
            ]

            for (node, color) in nodes {
                var branch = Path()
                branch.move(to: junction)
                branch.addLine(to: node)
                context.stroke(branch, with: .color(color), lineWidth: stroke)
                context.fill(
                    Path(ellipseIn: CGRect(
                        x: node.x - stroke * 1.35,
                        y: node.y - stroke * 1.35,
                        width: stroke * 2.7,
                        height: stroke * 2.7
                    )),
                    with: .color(color)
                )
            }

            context.fill(
                Path(ellipseIn: CGRect(
                    x: junction.x - stroke * 1.15,
                    y: junction.y - stroke * 1.15,
                    width: stroke * 2.3,
                    height: stroke * 2.3
                )),
                with: .color(.teal)
            )
        }
        .frame(width: size, height: size)
        .accessibilityLabel("NaumiAgent")
    }
}
