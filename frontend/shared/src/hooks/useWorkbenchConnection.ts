import { useWorkspace } from './WorkspaceProvider'
import type { ConnectionCoordinatorOptions } from '@naumi/shared/api/WorkbenchConnectionCoordinator'

// Compatibility adapter: both web shells use one workspace lifecycle and API client.
export function useWorkbenchConnection(
  _options: ConnectionCoordinatorOptions = {},
) {
  const workspace = useWorkspace()
  return {
    client: workspace.daemon ? workspace.api : null,
    status: {
      isConnected: !!workspace.daemon,
      daemon: workspace.daemon,
      error: workspace.error || null,
    },
    currentSessionId: workspace.sessionId,
    snapshot: workspace.snapshot,
    isReady: !!workspace.daemon && !workspace.connecting,
    error: workspace.daemon ? null : workspace.error || null,
    connect: workspace.connect,
    selectSession: async (id: string) => workspace.select(id),
    bootstrap: workspace.connect,
    disconnect: () => {
      void workspace.select(null)
    },
  }
}
