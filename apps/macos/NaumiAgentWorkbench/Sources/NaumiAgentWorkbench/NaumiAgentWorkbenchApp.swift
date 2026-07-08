import SwiftUI
import AppKit
import NaumiAgentWorkbenchCore

@main
struct NaumiAgentWorkbenchApp: App {
    @NSApplicationDelegateAdaptor(WorkbenchAppDelegate.self) private var appDelegate
    @State private var environment = AppEnvironment()
    private let shellPresentation = WorkbenchShellPresentation()

    var body: some Scene {
        WindowGroup(shellPresentation.nativeWindowTitle) {
            WorkbenchShellView(environment: environment)
                .background(WindowChromeConfigurator())
                .onAppear { appDelegate.environment = environment }
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

/// Intercepts app termination to offer keeping a supervised daemon alive.
final class WorkbenchAppDelegate: NSObject, NSApplicationDelegate {
    var environment: AppEnvironment?

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let environment,
              environment.appState.supervisedDaemonState == .running else {
            return .terminateNow
        }
        let locale = environment.appState.locale
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = AppStrings.SupervisedDaemon.shutdownPromptTitle(locale)
        alert.informativeText = AppStrings.SupervisedDaemon.shutdownPromptMessage(locale)
        alert.addButton(withTitle: AppStrings.SupervisedDaemon.shutdownKeepButton(locale))
        alert.addButton(withTitle: AppStrings.SupervisedDaemon.shutdownStopButton(locale))

        let response = alert.runModal()
        if response == .alertFirstButtonReturn {
            // Keep the daemon running; quit immediately.
            return .terminateNow
        }
        // Stop the supervised daemon, then complete termination.
        Task { @MainActor in
            await environment.daemonProcessController.stop()
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
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
