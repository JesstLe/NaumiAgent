import SwiftUI
import NaumiAgentWorkbenchCore

@main
struct NaumiAgentWorkbenchApp: App {
    @State private var environment = AppEnvironment()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(environment)
                .task {
                    await environment.daemonController.refreshConnection()
                }
        }
    }
}

struct ContentView: View {
    @Environment(AppEnvironment.self) private var environment

    var body: some View {
        @Bindable var appState = environment.appState
        NavigationSplitView {
            List(AppRoute.allCases, selection: $appState.currentRoute) { route in
                NavigationLink(value: route) {
                    Label(
                        route.displayName(locale: appState.locale),
                        systemImage: route.systemImage
                    )
                }
            }
            .navigationSplitViewColumnWidth(min: 160, ideal: 180)
        } detail: {
            DashboardView(appState: environment.appState)
        }
    }
}
