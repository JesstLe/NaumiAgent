import { useEffect, useRef, useState, useCallback } from 'react'
import { usePlatform } from '@/platform/PlatformContext'
import { WorkbenchConnectionCoordinator } from '@/api/WorkbenchConnectionCoordinator'
import type { WorkbenchApiClient } from '@/api/WorkbenchApiClient'
import type { ConnectionStatus } from '@/api/WorkbenchConnectionCoordinator'
import type { WorkbenchSnapshot } from '@/api/types'

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

export function useWorkbenchConnection(): UseWorkbenchConnectionResult {
  const platform = usePlatform()
  const coordinatorRef = useRef<WorkbenchConnectionCoordinator | null>(null)
  const [status, setStatus] = useState<ConnectionStatus>({
    isConnected: false,
    daemon: null,
    error: null,
  })
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  const [snapshot, setSnapshot] = useState<WorkbenchSnapshot | null>(null)
  const [isReady, setIsReady] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const coordinator = new WorkbenchConnectionCoordinator(
      async () => platform.getToken(),
      {
        onSnapshot: (next) => setSnapshot(next),
      },
    )
    coordinatorRef.current = coordinator
    const unsubscribe = coordinator.subscribeToStatus((next) => setStatus(next))
    return () => {
      unsubscribe()
      coordinator.disconnect()
      coordinatorRef.current = null
    }
  }, [platform])

  const connect = useCallback(async () => {
    const coordinator = coordinatorRef.current
    if (!coordinator) return
    try {
      await coordinator.connect()
      setError(null)
    } catch (err) {
      const message = err instanceof Error ? err.message : '连接失败'
      setError(message)
      throw err
    }
  }, [])

  const selectSession = useCallback(
    async (sessionId: string) => {
      const coordinator = coordinatorRef.current
      if (!coordinator) return
      const next = await coordinator.selectSession(sessionId)
      setCurrentSessionId(sessionId)
      setSnapshot(next)
      setError(null)
    },
    [],
  )

  const bootstrap = useCallback(async () => {
    const coordinator = coordinatorRef.current
    if (!coordinator) return
    try {
      await coordinator.connect()
      const result = await coordinator.bootstrap()
      setCurrentSessionId(result.sessionId)
      setSnapshot(result.snapshot)
      setIsReady(true)
      setError(null)
    } catch (err) {
      const message = err instanceof Error ? err.message : '启动失败'
      setError(message)
      setIsReady(false)
      throw err
    }
  }, [])

  const disconnect = useCallback(() => {
    coordinatorRef.current?.disconnect()
    setCurrentSessionId(null)
    setSnapshot(null)
    setIsReady(false)
  }, [])

  return {
    client: coordinatorRef.current?.client ?? null,
    status,
    currentSessionId,
    snapshot,
    isReady,
    error,
    connect,
    selectSession,
    bootstrap,
    disconnect,
  }
}
