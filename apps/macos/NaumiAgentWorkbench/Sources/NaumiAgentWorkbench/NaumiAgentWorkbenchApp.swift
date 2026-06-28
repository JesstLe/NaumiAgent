import SwiftUI
import AppKit
import NaumiAgentWorkbenchCore

@main
struct NaumiAgentWorkbenchApp: App {
    @State private var environment = AppEnvironment()
    private let shellPresentation = WorkbenchShellPresentation()

    var body: some Scene {
        WindowGroup(shellPresentation.nativeWindowTitle) {
            WorkbenchShellView(environment: environment)
                .background(WindowChromeConfigurator())
                .task {
                    switch WorkbenchPreviewLoader.requestedMode(from: CommandLine.arguments) {
                    case .disabled:
                        await withTaskGroup(of: Void.self) { group in
                            group.addTask {
                                await environment.refreshCoordinator.startPeriodicRefresh()
                            }
                            group.addTask {
                                await environment.refreshCoordinator.startPeriodicEventStreamHealthProbes()
                            }
                        }
                    case .enabled(let locale):
                        do {
                            try WorkbenchPreviewLoader.applyPreviewState(
                                locale: locale,
                                to: environment.appState
                            )
                            if let previewRoute = WorkbenchPreviewLoader.requestedRoute(
                                from: CommandLine.arguments
                            ) {
                                environment.appState.currentRoute = previewRoute
                            }
                        } catch {
                            environment.appState.connectionState = .disconnected
                        }
                    case .malformed:
                        environment.appState.connectionState = .disconnected
                    }
                }
        }
    }
}

private struct WindowChromeConfigurator: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async {
            configure(window: view.window)
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        DispatchQueue.main.async {
            configure(window: nsView.window)
        }
    }

    private func configure(window: NSWindow?) {
        guard let window else { return }
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = false
    }
}
