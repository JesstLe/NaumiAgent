import { useEffect, useCallback, useSyncExternalStore } from 'react'
import { usePlatform } from '@/platform/PlatformContext'
import { WorkbenchConnectionCoordinator } from '@/api/WorkbenchConnectionCoordinator'
import type { WorkbenchApiClient } from '@/api/WorkbenchApiClient'
import type { ConnectionCoordinatorOptions, ConnectionStatus } from '@/api/WorkbenchConnectionCoordinator'
import type { WorkbenchSnapshot } from '@/api/types'
import { useSessionStore } from '@/stores/sessionStore'

interface UseWorkbenchConnectionResult {
  client: WorkbenchApiClient | null
  status: ConnectionStatus
  currentSessionId: string | null
  snapshot: WorkbenchSnapshot | null
  isReady: boolean
  error: string | null
  connect: () => Promise<void>
  selectSession: (sessionId: string) => Promise<void>
  bootstrap: () => Promise<void>
  disconnect: () => void
}

// --- Module-level singleton ---
// The connection coordinator is shared across all components that call
// useWorkbenchConnection(). Previously each component created its own
// coordinator, which meant client/sessionId were not shared and only the
// ConnectionBootstrap instance could drive the connection lifecycle.

interface SharedState {
  status: ConnectionStatus
  currentSessionId: string | null
  snapshot: WorkbenchSnapshot | null
  isReady: boolean
  error: string | null
}

let coordinator: WorkbenchConnectionCoordinator | null = null
let sharedState: SharedState = {
  status: { isConnected: false, daemon: null, error: null },
  currentSessionId: null,
  snapshot: null,
  isReady: false,
  error: null,
}
const listeners = new Set<() => void>()
let onSnapshotListener: ((snapshot: WorkbenchSnapshot) => void) | null = null

function notifyListeners() {
  for (const listener of listeners) {
    listener()
  }
}

function getSnapshot(): SharedState {
  return sharedState
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function ensureCoordinator(tokenProvider: () => Promise<string | null>, options?: ConnectionCoordinatorOptions): WorkbenchConnectionCoordinator {
  if (coordinator) return coordinator
  coordinator = new WorkbenchConnectionCoordinator(tokenProvider, {
    ...options,
    onSnapshot: (next) => {
      sharedState = { ...sharedState, snapshot: next }
      onSnapshotListener?.(next)
      notifyListeners()
    },
  })
  // Subscribe to status changes from the coordinator.
  coordinator.subscribeToStatus((next) => {
    sharedState = { ...sharedState, status: next }
    notifyListeners()
  })
  // Subscribe to events so permission requests can be surfaced in the UI.
  coordinator.subscribeToEvents((event) => {
    if (event.type === 'permission_bubble') {
      const data = event.payload as {
        call_id?: string
        agent_name?: string
        tool_name?: string
        reason?: string
        risk_level?: string
        status?: string
      }
      const callId = data?.call_id
      if (!callId) return
      if (data.status === 'needs_confirmation') {
        useSessionStore.getState().addPendingPermission({
          call_id: callId,
          agent_name: data.agent_name || 'Agent',
          tool_name: data.tool_name || 'tool',
          reason: data.reason || '',
          risk_level: data.risk_level,
        })
      } else {
        useSessionStore.getState().removePendingPermission(callId)
      }
    }
  })
  return coordinator
}

export function useWorkbenchConnection(options: ConnectionCoordinatorOptions = {}): UseWorkbenchConnectionResult {
  const platform = usePlatform()

  // Keep the snapshot listener in sync with the caller's onSnapshot option.
  useEffect(() => {
    onSnapshotListener = options.onSnapshot ?? null
    return () => {
      if (onSnapshotListener === options.onSnapshot) {
        onSnapshotListener = null
      }
    }
  }, [options.onSnapshot])

  // Initialize the singleton coordinator once.
  useEffect(() => {
    ensureCoordinator(async () => platform.getToken(), options)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platform])

  const state = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)

  const connect = useCallback(async () => {
    if (!coordinator) return
    try {
      await coordinator.connect()
      sharedState = { ...sharedState, error: null }
      notifyListeners()
    } catch (err) {
      const message = err instanceof Error ? err.message : '连接失败'
      sharedState = { ...sharedState, error: message }
      notifyListeners()
      throw err
    }
  }, [])

  const selectSession = useCallback(async (sessionId: string) => {
    if (!coordinator) return
    const next = await coordinator.selectSession(sessionId)
    sharedState = {
      ...sharedState,
      currentSessionId: sessionId,
      snapshot: next,
      error: null,
    }
    notifyListeners()
  }, [])

  const bootstrap = useCallback(async () => {
    if (!coordinator) return
    try {
      await coordinator.connect()
      const result = await coordinator.bootstrap()
      sharedState = {
        ...sharedState,
        currentSessionId: result.sessionId,
        snapshot: result.snapshot,
        isReady: true,
        error: null,
      }
      notifyListeners()
    } catch (err) {
      const message = err instanceof Error ? err.message : '启动失败'
      sharedState = { ...sharedState, error: message, isReady: false }
      notifyListeners()
      throw err
    }
  }, [])

  const disconnect = useCallback(() => {
    coordinator?.disconnect()
    sharedState = {
      ...sharedState,
      currentSessionId: null,
      snapshot: null,
      isReady: false,
    }
    notifyListeners()
  }, [])

  return {
    client: coordinator?.client ?? null,
    status: state.status,
    currentSessionId: state.currentSessionId,
    snapshot: state.snapshot,
    isReady: state.isReady,
    error: state.error,
    connect,
    selectSession,
    bootstrap,
    disconnect,
  }
}
