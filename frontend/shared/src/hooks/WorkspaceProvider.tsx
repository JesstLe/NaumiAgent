import { createContext, useContext, useEffect, type ReactNode } from 'react'
import { useWorkspaceController } from './useWorkspaceController'
import { useSessionStore } from '@naumi/shared/stores/sessionStore'

export type Workspace = ReturnType<typeof useWorkspaceController>
const WorkspaceContext = createContext<Workspace | null>(null)

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const workspace = useWorkspaceController()
  // Compatibility projection for the existing dashboard and domain pages.
  useEffect(() => {
    useSessionStore.setState({
      sessions: workspace.sessions,
      currentSessionId: workspace.sessionId,
      messages: workspace.messages,
      snapshot: workspace.snapshot,
      error: workspace.error || null,
    })
  }, [
    workspace.sessions,
    workspace.sessionId,
    workspace.messages,
    workspace.snapshot,
    workspace.error,
  ])
  return (
    <WorkspaceContext.Provider value={workspace}>
      {children}
    </WorkspaceContext.Provider>
  )
}

export function useWorkspace() {
  const workspace = useContext(WorkspaceContext)
  if (!workspace) throw new Error('工作区未初始化')
  return workspace
}
