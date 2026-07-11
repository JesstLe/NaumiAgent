import SwiftUI

/// Simple status badge used across dashboard and lists.
public struct StatusBadge: View {
    public let text: String
    public let color: Color

    public init(text: String, color: Color = .secondary) {
        self.text = text
        self.color = color
    }

    public var body: some View {
        WorkbenchStatusChip(text: text, color: color)
    }
}
