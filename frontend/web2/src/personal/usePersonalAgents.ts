import { useCallback, useEffect, useRef, useState } from 'react'
import type { PersonalAgent, WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'

export type PersonalAgentsApi = Pick<WorkbenchRuntimeClient,
  'personalAgents' | 'personalTools' | 'createPersonalAgent' | 'updatePersonalAgent' |
  'personalAgentHistory' | 'openPersonalSession' | 'currentPersonalAgent'
>
export const personalError = (cause: unknown) => cause instanceof Error ? cause.message : '操作未完成，请重试'

export function usePersonalAgents({ api, sessionId, busy, connected, onSelect }: {
  api: PersonalAgentsApi
  sessionId: string | null
  busy: boolean
  connected: boolean
  onSelect: (sessionId: string | null) => Promise<unknown>
}) {
  const [agents, setAgents] = useState<PersonalAgent[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [currentState, setCurrentState] = useState<{
    api: PersonalAgentsApi; sessionId: string | null; agent: PersonalAgent | null; error: string
    readOnly: boolean; collaborationId: string
  } | null>(null)
  const [opening, setOpening] = useState<string | null>(null)
  const listSequence = useRef(0)
  const currentSequence = useRef(0)
  const actionSequence = useRef(0)
  const actionBusy = useRef(false)
  const alive = useRef(true)
  const busyRef = useRef(busy)
  busyRef.current = busy

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      listSequence.current += 1
      currentSequence.current += 1
      actionSequence.current += 1
    }
  }, [])

  const refreshList = useCallback(async () => {
    const sequence = ++listSequence.current
    if (!connected) { setAgents([]); setLoading(false); setError(''); return }
    setLoading(true)
    try {
      const result = await api.personalAgents(true)
      if (!Array.isArray(result.agents)) throw new Error('助理目录返回不完整，请重试')
      if (alive.current && sequence === listSequence.current) {
        setAgents(result.agents)
        setError('')
      }
    } catch (cause) {
      if (alive.current && sequence === listSequence.current) setError(personalError(cause))
    } finally {
      if (alive.current && sequence === listSequence.current) setLoading(false)
    }
  }, [api, connected])

  const refreshCurrent = useCallback(async () => {
    const sequence = ++currentSequence.current
    if (!sessionId || !connected) {
      setCurrentState({ api, sessionId, agent: null, error: '', readOnly: false, collaborationId: '' })
      return
    }
    try {
      const result = await api.currentPersonalAgent(sessionId)
      if (!Object.prototype.hasOwnProperty.call(result, 'agent')) throw new Error('未能确认当前助理，请重试')
      if (alive.current && sequence === currentSequence.current)
        setCurrentState({ api, sessionId, agent: result.agent, error: '',
          readOnly: result.read_only === true, collaborationId: result.collaboration_id || '' })
    } catch (cause) {
      if (alive.current && sequence === currentSequence.current)
        setCurrentState({ api, sessionId, agent: null, error: personalError(cause), readOnly: false, collaborationId: '' })
    }
  }, [api, sessionId, connected])

  useEffect(() => {
    setAgents([])
    setOpening(null)
    actionBusy.current = false
    actionSequence.current += 1
    void refreshList()
    return () => { listSequence.current += 1 }
  }, [refreshList])
  useEffect(() => {
    void refreshCurrent()
    return () => { currentSequence.current += 1 }
  }, [refreshCurrent])
  const wasBusy = useRef(busy)
  useEffect(() => {
    if (wasBusy.current && !busy) { void refreshList(); void refreshCurrent() }
    wasBusy.current = busy
  }, [busy, refreshList, refreshCurrent])

  const select = async (agent: PersonalAgent | null) => {
    if (actionBusy.current || busyRef.current || !connected) return
    if (agent && agent.status !== 'active') return
    const sequence = ++actionSequence.current
    actionBusy.current = true
    setOpening(agent?.id || 'main')
    setError('')
    try {
      const binding = agent ? await api.openPersonalSession(agent.id) : null
      if (!alive.current || sequence !== actionSequence.current || busyRef.current) return
      if (binding && (!binding.session_id || binding.agent_id !== agent?.id))
        throw new Error('会话与助理不匹配，请刷新后重试')
      await onSelect(binding?.session_id ?? null)
    } catch (cause) {
      if (alive.current && sequence === actionSequence.current) setError(personalError(cause))
    } finally {
      if (alive.current && sequence === actionSequence.current) {
        actionBusy.current = false
        setOpening(null)
      }
    }
  }
  const resolved = !sessionId || (currentState?.api === api && currentState.sessionId === sessionId)
  const current = resolved && sessionId ? currentState?.agent ?? null : null
  const currentError = resolved && sessionId ? currentState?.error || '' : ''
  const readOnly = Boolean(resolved && sessionId && currentState?.readOnly)
  const collaborationId = resolved && sessionId ? currentState?.collaborationId || '' : ''
  const refresh = async () => { await Promise.all([refreshList(), refreshCurrent()]) }

  return { agents, loading, error, current, currentError, readOnly, collaborationId,
    currentPending: Boolean(sessionId && connected && !resolved), opening, select, refresh }
}

export type PersonalAgentsState = ReturnType<typeof usePersonalAgents>
