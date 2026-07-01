import AppKit
import Foundation
import SwiftUI
import NaumiAgentWorkbenchCore

@main
struct WorkbenchSnapshotTool {
    static func main() async {
        do {
            try await MainActor.run {
                try run()
            }
        } catch {
            FileHandle.standardError.write(Data("snapshot failed: \(error)\n".utf8))
            Foundation.exit(1)
        }
    }

    @MainActor
    private static func run() throws {
        let options = try SnapshotOptions(arguments: CommandLine.arguments)
        try FileManager.default.createDirectory(
            at: options.outputDirectory,
            withIntermediateDirectories: true
        )

        NSApplication.shared.setActivationPolicy(.prohibited)

        for route in options.routes {
            let state = AppState()
            try WorkbenchPreviewLoader.applyPreviewState(
                locale: options.locale,
                to: state,
                fixtureDirectory: options.fixtureDirectory
            )
            state.currentRoute = route

            let environment = AppEnvironment(appState: state)
            let imageData = try render(
                WorkbenchShellView(environment: environment)
                    .frame(width: options.width, height: options.height),
                width: options.width,
                height: options.height
            )
            let outputURL = options.outputDirectory
                .appendingPathComponent("\(fileName(for: route))-\(options.localeToken).png")
            try imageData.write(to: outputURL, options: .atomic)
            print(outputURL.path)
        }
    }

    @MainActor
    private static func render<Content: View>(
        _ view: Content,
        width: Double,
        height: Double
    ) throws -> Data {
        let size = CGSize(width: width, height: height)
        let hostingView = NSHostingView(rootView: view)
        hostingView.frame = CGRect(origin: .zero, size: size)

        let window = NSWindow(
            contentRect: CGRect(origin: .zero, size: size),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "NaumiAgent Workbench"
        window.contentView = hostingView
        window.layoutIfNeeded()
        hostingView.layoutSubtreeIfNeeded()

        guard let bitmap = hostingView.bitmapImageRepForCachingDisplay(in: hostingView.bounds) else {
            throw SnapshotError.cannotCreateBitmap
        }
        hostingView.cacheDisplay(in: hostingView.bounds, to: bitmap)

        guard let data = bitmap.representation(using: .png, properties: [:]) else {
            throw SnapshotError.cannotEncodePNG
        }
        return data
    }

    private static func fileName(for route: AppRoute) -> String {
        switch route {
        case .dashboard:
            return "01-dashboard"
        case .chat:
            return "02-chat"
        case .taskMarket:
            return "03-task-market"
        case .worktrees:
            return "04-worktrees"
        case .reviews:
            return "05-reviews"
        case .timeline:
            return "06-timeline"
        case .settings:
            return "07-settings"
        }
    }
}

private struct SnapshotOptions {
    let locale: AppLocale
    let outputDirectory: URL
    let fixtureDirectory: URL?
    let width: Double
    let height: Double
    let routes: [AppRoute]
    var localeToken: String { locale == .zhCN ? "zh" : "en" }

    init(arguments: [String]) throws {
        let localeArgument = Self.value(after: "--locale", in: arguments) ?? "zh"
        guard let parsedLocale = WorkbenchPreviewLoader.locale(for: localeArgument) else {
            throw SnapshotError.unknownLocale(localeArgument)
        }
        locale = parsedLocale
        outputDirectory = URL(fileURLWithPath: Self.value(after: "--out", in: arguments) ?? "docs/mac-app/ui-audit/screenshots")
        fixtureDirectory = Self.value(after: "--fixtures", in: arguments).map { URL(fileURLWithPath: $0) }
        width = Double(Self.value(after: "--width", in: arguments) ?? "1440") ?? 1440
        height = Double(Self.value(after: "--height", in: arguments) ?? "900") ?? 900

        if let routeArgument = Self.value(after: "--route", in: arguments), routeArgument != "all" {
            guard let route = WorkbenchPreviewLoader.route(for: routeArgument) else {
                throw SnapshotError.unknownRoute(routeArgument)
            }
            routes = [route]
        } else {
            routes = AppRoute.topNavigationRoutes
        }
    }

    private static func value(after flag: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: flag),
              arguments.indices.contains(index + 1) else {
            return nil
        }
        return arguments[index + 1]
    }
}

private enum SnapshotError: Error, CustomStringConvertible {
    case cannotCreateBitmap
    case cannotEncodePNG
    case unknownLocale(String)
    case unknownRoute(String)

    var description: String {
        switch self {
        case .cannotCreateBitmap:
            return "cannot create bitmap"
        case .cannotEncodePNG:
            return "cannot encode PNG"
        case .unknownLocale(let locale):
            return "unknown locale: \(locale)"
        case .unknownRoute(let route):
            return "unknown route: \(route)"
        }
    }
}
