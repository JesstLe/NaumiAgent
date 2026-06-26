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
            routeView(for: appState.currentRoute)
        }
    }

    @ViewBuilder
    private func routeView(for route: AppRoute) -> some View {
        switch route {
        case .dashboard:
            DashboardView(appState: environment.appState)
        case .taskMarket:
            TaskMarketView(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
        case .timeline:
            TimelineView(
                appState: environment.appState,
                daemonController: environment.daemonController
            )
        case .worktrees, .reviews, .settings:
            PlaceholderRouteView(
                route: route,
                locale: environment.appState.locale
            )
        }
    }
}

/// Lightweight placeholder for routes that are not yet implemented.
private struct PlaceholderRouteView: View {
    let route: AppRoute
    let locale: AppLocale

    var body: some View {
        VStack(spacing: 12) {
            Spacer()
            Image(systemName: route.systemImage)
                .font(.system(size: 48))
                .foregroundStyle(.secondary)
            Text(route.displayName(locale: locale))
                .font(.title2)
                .fontWeight(.semibold)
            Text(AppStrings.Navigation.pageUnderConstruction(locale))
                .foregroundStyle(.secondary)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .navigationTitle(route.displayName(locale: locale))
    }
}
