import { useCallback, useEffect, useRef, useState } from 'react'
import type { GoalSnapshot, WorkbenchRuntimeClient, WorkspaceGoal } from '../api/WorkbenchRuntimeClient'

export function useWorkspaceGoals(api: WorkbenchRuntimeClient, connected: boolean, ensureSession: () => Promise<string>) {
  const [goalSnapshot, setSnapshot] = useState<GoalSnapshot | null>(null)
  const [goalError, setError] = useState('')
  const [goalsMutating, setMutating] = useState(false)
  const revision = useRef(0)
  const pending = useRef(false)
  const refreshGoals = useCallback(async () => {
    if (!connected || pending.current) return
    const version = ++revision.current
    try { const next = await api.goals(); if (version === revision.current) { setSnapshot(next); setError('') } }
    catch (error) { if (version === revision.current) setError(error instanceof Error ? error.message : '目标加载失败') }
  }, [api, connected])
  useEffect(() => {
    setSnapshot(null); setError(''); void refreshGoals()
    const timer = setInterval(() => { if (!document.hidden) void refreshGoals() }, 15000)
    return () => { revision.current++; clearInterval(timer) }
  }, [refreshGoals])
  const mutate = async (operation: () => Promise<GoalSnapshot>) => {
    if (pending.current) return false
    pending.current = true; setMutating(true); setError('')
    const version = ++revision.current
    try { const result = await operation(); if (version === revision.current) setSnapshot(result); return true }
    catch (error) { if (version === revision.current) setError(error instanceof Error ? error.message : '目标未保存，请重试'); return false }
    finally { pending.current = false; setMutating(false) }
  }
  return { goalSnapshot, goalError, goalsMutating, refreshGoals,
    addGoal: (objective: string) => mutate(async () => api.createGoal(await ensureSession(), objective)),
    updateGoal: (id: string, status: WorkspaceGoal['status'], note: string) => mutate(() => api.updateGoal(id, status, note)),
  }
}
