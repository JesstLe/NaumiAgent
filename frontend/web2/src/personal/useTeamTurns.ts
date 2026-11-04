import { useEffect, useRef, useState } from 'react'
import type {
  PersonalAgent, PersonalPeerGrant, PersonalTeamTurn, PersonalTeamTurnInput, PersonalTool, WorkbenchRuntimeClient,
} from '@naumi/shared/api/WorkbenchRuntimeClient'
import { validateCollaboration } from './Collaborations'
import { defaultPeerTools } from './PeerMessages'
import { personalError } from './usePersonalAgents'

export type TeamTurnsApi = Pick<WorkbenchRuntimeClient,
  'personalTeamTurns' | 'personalTeamTurn' | 'createPersonalTeamTurn' | 'cancelPersonalTeamTurn' | 'personalTools'
>
const states = new Set(['queued', 'planning', 'working', 'summarizing', 'completed', 'cancelled', 'error', 'unknown', 'blocked', 'delivery_pending'])
export const activeTeamTurnStates = new Set(['queued', 'planning', 'working', 'summarizing', 'delivery_pending'])
const closedStates = new Set(['completed', 'cancelled', 'error', 'unknown', 'blocked'])
const agentId = /^pa_[a-f0-9]{32}$/
const digest = /^[a-f0-9]{64}$/
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value) && value > 0

export function validateTeamTurn(value: PersonalTeamTurn, scope: { teamId: string; sessionId: string }, expected?: PersonalTeamTurnInput) {
  const text: (keyof PersonalTeamTurn)[] = ['id', 'request_id', 'team_id', 'team_name', 'source_session_id',
    'team_session_id', 'source_user_message_id', 'result_message_id', 'content', 'context', 'created_at',
    'updated_at', 'response', 'error', 'result_sha256', 'status']
  if (!value || text.some(field => typeof value[field] !== 'string') || !/^pt_[a-f0-9]{32}$/.test(value.id)
    || value.team_id !== scope.teamId || value.team_session_id !== scope.sessionId
    || !positive(value.team_version) || typeof value.cancel_requested !== 'boolean' || !states.has(value.status)
    || (value.result_sha256 && !digest.test(value.result_sha256)) || !value.usage
    || !Number.isInteger(value.usage.total_tokens) || value.usage.total_tokens < 0
    || !Number.isInteger(value.usage.job_count) || value.usage.job_count < 0
    || typeof value.usage.total_cost_usd !== 'number' || !Number.isFinite(value.usage.total_cost_usd) || value.usage.total_cost_usd < 0
    || !value.roster || !Array.isArray(value.roster.teams) || !Array.isArray(value.roster.agents)
    || value.roster.teams.some(team => !team || !agentId.test(team.agent_id) || !positive(team.agent_version)
      || !Array.isArray(team.member_ids) || team.member_ids.some(id => typeof id !== 'string' || !agentId.test(id)))
    || value.roster.agents.some(agent => !agent || !agentId.test(agent.agent_id) || !positive(agent.agent_version)
      || !Array.isArray(agent.tool_names) || agent.tool_names.some(name => typeof name !== 'string'))
    || !value.roster.teams.some(team => team.agent_id === value.team_id && team.agent_version === value.team_version)
    || !Array.isArray(value.phases) || value.phases.some(phase => !phase || !['planning', 'members', 'summary'].includes(phase.phase)
      || typeof phase.collaboration_id !== 'string' || validateCollaboration(phase.collaboration).id !== phase.collaboration_id)
    || new Set(value.phases.map(phase => phase.phase)).size !== value.phases.length)
    throw new Error('团队任务记录不完整或来源不匹配，请重新加载原团队对话')
  if (expected && (value.request_id !== expected.request_id || value.source_session_id !== expected.source_session_id
    || value.team_id !== expected.team_id || value.team_version !== expected.team_version
    || value.content !== expected.content || value.context !== (expected.context || '')))
    throw new Error('返回的团队任务与原始请求不一致，请核对原执行记录')
  if (value.status === 'completed' && (!value.result_message_id || !value.result_sha256))
    throw new Error('团队结果尚无对话交付凭据，请重新加载原任务')
  return value
}

export function retainTeamTurn(previous: PersonalTeamTurn | undefined, next: PersonalTeamTurn) {
  if (!previous) return next
  const immutable: (keyof PersonalTeamTurn)[] = ['id', 'request_id', 'team_id', 'team_version', 'team_name',
    'source_session_id', 'team_session_id', 'content', 'context', 'created_at']
  if (immutable.some(key => previous[key] !== next[key]) || JSON.stringify(previous.roster) !== JSON.stringify(next.roster)
    || (previous.source_user_message_id && next.source_user_message_id && previous.source_user_message_id !== next.source_user_message_id)
    || (previous.result_message_id && next.result_message_id && previous.result_message_id !== next.result_message_id)
    || (previous.result_sha256 && next.result_sha256 && (previous.result_sha256 !== next.result_sha256 || previous.response !== next.response))
    || previous.phases.some(phase => next.phases.some(candidate => candidate.phase === phase.phase && candidate.collaboration_id !== phase.collaboration_id)))
    throw new Error('团队任务身份或结果摘要发生冲突，请保留原始记录')
  if ((previous.result_sha256 && !next.result_sha256) || (previous.result_message_id && !next.result_message_id)
    || previous.phases.length > next.phases.length || (closedStates.has(previous.status) && !closedStates.has(next.status))) return previous
  const newlyDelivered = next.status === 'completed' && next.result_message_id && next.result_sha256 && !previous.result_message_id
  if (closedStates.has(previous.status) && previous.status !== next.status && !newlyDelivered) return previous
  return { ...next, cancel_requested: previous.cancel_requested || next.cancel_requested }
}

export function resolveTeamRoster(team: PersonalAgent, catalog: PersonalAgent[]) {
  const byId = new Map(catalog.map(agent => [agent.id, agent]))
  byId.set(team.id, team)
  const teams: PersonalAgent[] = [], members: PersonalAgent[] = [], errors: string[] = []
  const visited = new Set<string>(), stack = new Set<string>()
  const visit = (identity: string) => {
    if (stack.has(identity)) { errors.push('团队成员关系形成循环'); return }
    if (visited.has(identity)) return
    const entry = byId.get(identity)
    if (!entry) { errors.push(`成员资料暂不可用 · ${identity}`); return }
    visited.add(identity)
    if (entry.kind !== 'team') { members.push(entry); return }
    // Leaf availability is checked against the selected grants, not the roster.
    if (entry.status !== 'active') errors.push(`${entry.name}已${entry.status === 'paused' ? '暂停' : '归档'}`)
    teams.push(entry); stack.add(identity)
    entry.member_ids.forEach(visit)
    stack.delete(identity)
  }
  visit(team.id)
  return { teams, members: members.sort((a, b) => a.id.localeCompare(b.id)), errors: [...new Set(errors)] }
}

type View = { scope: string; turns: PersonalTeamTurn[]; pending: PersonalTeamTurnInput | null; error: string;
  loading: boolean; posting: boolean; historyError: string; identityError: boolean }
const empty = (scope: string): View => ({ scope, turns: [], pending: null, error: '', loading: true, posting: false, historyError: '', identityError: false })
const pendingKey = (namespace: string, teamId: string, sessionId: string) => `naumi:team-pending:${namespace}:${teamId}:${sessionId}`

export function useTeamTurns({ api, namespace, team, sessionId, catalog, connected, linkedTurnIds = [], onAccepted, onConversationRefresh }: {
  api: TeamTurnsApi; namespace: string; team: PersonalAgent | null; sessionId: string | null
  catalog: PersonalAgent[]; connected: boolean
  linkedTurnIds?: string[]
  onAccepted?: (body: PersonalTeamTurnInput, namespace: string) => void
  onConversationRefresh?: (sessionId: string) => Promise<unknown>
}) {
  const scope = team && sessionId ? `${namespace}\0${team.id}\0${sessionId}` : ''
  const linksKey = [...new Set(linkedTurnIds.filter(identity => /^pt_[a-f0-9]{32}$/.test(identity)))].sort().join('|')
  const [view, setView] = useState<View>(() => empty(scope))
  const [revision, setRevision] = useState(0)
  const [toolState, setToolState] = useState<{ api: TeamTurnsApi; tools: PersonalTool[]; loading: boolean; error: string } | null>(null)
  const [selectedGrants, setSelectedGrants] = useState<{ scope: string; value: PersonalPeerGrant[] } | null>(null)
  const [contextState, setContextState] = useState<{ scope: string; value: string } | null>(null)
  const current = useRef({ scope, api, team, sessionId })
  current.current = { scope, api, team, sessionId }
  const callbacks = useRef({ onAccepted, onConversationRefresh })
  callbacks.current = { onAccepted, onConversationRefresh }
  const pending = useRef(new Map<string, PersonalTeamTurnInput>())
  const known = useRef(new Map<string, PersonalTeamTurn>())
  const inFlight = useRef(new Set<string>())
  const historyFacts = useRef(new Map<string, string>())
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const coordination = Boolean(team?.tool_names.includes('personal_team'))
  const roster = team ? resolveTeamRoster(team, catalog) : { teams: [], members: [], errors: [] }
  useEffect(() => {
    if (!team || !coordination || !connected) return
    let stopped = false
    setToolState({ api, tools: [], loading: true, error: '' })
    void api.personalTools().then(result => {
      if (!Array.isArray(result.tools) || result.tools.some(tool => !tool || typeof tool.name !== 'string'
        || typeof tool.description !== 'string' || (tool.read_only !== undefined && typeof tool.read_only !== 'boolean')))
        throw new Error('工具权限返回不完整，请重新加载')
      if (!stopped) setToolState({ api, tools: result.tools, loading: false, error: '' })
    }).catch(cause => { if (!stopped) setToolState({ api, tools: [], loading: false, error: personalError(cause) }) })
    return () => { stopped = true }
  }, [api, Boolean(team), coordination, connected, revision])

  const savePending = (body: PersonalTeamTurnInput | null, at: { scope: string; teamId: string; sessionId: string }) => {
    if (body) pending.current.set(at.scope, body)
    else pending.current.delete(at.scope)
    try {
      const key = pendingKey(namespace, at.teamId, at.sessionId)
      if (body) localStorage.setItem(key, JSON.stringify(body))
      else localStorage.removeItem(key)
    } catch { /* The in-memory identity still protects this window's retries. */ }
  }
  const refreshHistory = async (atScope: string, targetSession: string, turns: PersonalTeamTurn[]) => {
    const facts = turns.map(turn => `${turn.id}:${turn.source_user_message_id}:${turn.result_message_id}:${turn.result_sha256}`).join('|')
    if (historyFacts.current.get(atScope) === facts) return
    if (current.current.scope !== atScope || !alive.current) return
    try {
      const refreshed = await callbacks.current.onConversationRefresh?.(targetSession)
      if (refreshed !== false) historyFacts.current.set(atScope, facts)
      if (current.current.scope === atScope && alive.current) setView(previous => previous.scope === atScope ? { ...previous, historyError: '' } : previous)
    } catch (cause) {
      if (current.current.scope === atScope && alive.current) setView(previous => previous.scope === atScope
        ? { ...previous, historyError: '团队任务已保存，对话刷新失败：' + personalError(cause) } : previous)
    }
  }

  useEffect(() => {
    if (!scope || !team || !sessionId || !connected) { setView(empty(scope)); return }
    const captured = { scope, teamId: team.id, sessionId }
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined
    let restored = pending.current.get(scope) || null
    let restoreError = ''
    if (!restored) {
      try {
        const raw = localStorage.getItem(pendingKey(namespace, team.id, sessionId))
        if (raw) {
          const body = JSON.parse(raw) as PersonalTeamTurnInput
          if (!body || body.team_id !== team.id || body.source_session_id !== sessionId || !positive(body.team_version)
            || typeof body.request_id !== 'string' || !body.request_id || typeof body.content !== 'string' || !body.content.trim())
            throw new Error('原请求身份不完整，请先核对团队执行记录')
          restored = body; pending.current.set(scope, body)
        }
      } catch (cause) { restoreError = personalError(cause) }
    }
    setView(previous => ({ ...(previous.scope === scope ? previous : empty(scope)), pending: restored,
      loading: true, posting: inFlight.current.has(scope), error: restoreError, identityError: Boolean(restoreError) }))
    const load = async () => {
      try {
        const result = await api.personalTeamTurns(sessionId)
        if (!Array.isArray(result.turns)) throw new Error('团队任务列表返回不完整，请重新加载')
        const values = result.turns.map(turn => validateTeamTurn(turn, captured))
        if (new Set(values.map(turn => turn.id)).size !== values.length) throw new Error('团队任务列表包含重复身份，请重新加载')
        // The list is bounded, while historical messages retain their exact turn
        // links. Hydrate older links through the existing identity-scoped GET.
        const missing = (linksKey ? linksKey.split('|') : []).filter(identity => !values.some(turn => turn.id === identity)
          && (!known.current.has(scope + '\0' + identity)
            || activeTeamTurnStates.has(known.current.get(scope + '\0' + identity)!.status)
            || known.current.get(scope + '\0' + identity)!.status === 'unknown'))
        for (let index = 0; index < missing.length; index += 4) {
          if (stopped || current.current.scope !== scope) return
          const batch = await Promise.all(missing.slice(index, index + 4).map(async identity => {
            const turn = validateTeamTurn(await api.personalTeamTurn(identity), captured)
            if (turn.id !== identity) throw new Error('历史团队任务与原消息链接不一致')
            return turn
          }))
          values.push(...batch)
        }
        if (stopped || current.current.scope !== scope) return
        const accepted = values.map(turn => retainTeamTurn(known.current.get(scope + '\0' + turn.id), turn))
        accepted.forEach(turn => known.current.set(scope + '\0' + turn.id, turn))
        for (const [key, turn] of known.current) {
          if (key.startsWith(scope + '\0') && !accepted.some(value => value.id === turn.id)) accepted.push(turn)
        }
        accepted.sort((a, b) => b.created_at.localeCompare(a.created_at))
        const original = pending.current.get(scope)
        const receipt = original && accepted.find(turn => turn.request_id === original.request_id)
        if (original && receipt) {
          validateTeamTurn(receipt, captured, original)
          savePending(null, captured)
          callbacks.current.onAccepted?.(original, namespace)
        }
        setView(previous => previous.scope === scope ? { ...previous, turns: accepted,
          pending: pending.current.get(scope) || null,
          error: pending.current.has(scope) ? previous.error : restoreError, loading: false } : previous)
        void refreshHistory(scope, sessionId, accepted)
        if (accepted.some(turn => activeTeamTurnStates.has(turn.status)) || pending.current.has(scope))
          timer = setTimeout(() => void load(), 1800)
      } catch (cause) {
        if (!stopped && current.current.scope === scope) setView(previous => previous.scope === scope
          ? { ...previous, loading: false, error: personalError(cause) } : previous)
      }
    }
    void load()
    return () => { stopped = true; clearTimeout(timer) }
  }, [api, scope, sessionId, connected, namespace, revision, linksKey])

  const visible = view.scope === scope ? view : empty(scope)
  const tools = toolState?.api === api ? toolState.tools : []
  const grants = selectedGrants?.scope === scope ? selectedGrants.value
    : roster.members.filter(member => member.status === 'active').map(member => ({ agent_id: member.id,
      agent_version: member.version, tool_names: defaultPeerTools(member, tools) }))
  const context = contextState?.scope === scope ? contextState.value : ''
  const toolsLoading = coordination && (!toolState || toolState.api !== api || toolState.loading)
  const toolsError = coordination ? toolState?.error || '' : ''
  const staleGrants = grants.some(grant => !roster.members.some(member => member.id === grant.agent_id
    && member.version === grant.agent_version && member.status === 'active'))

  const submit = async (content: string, sourceIds: string[] = []) => {
    const captured = current.current
    if (!captured.scope || !captured.team || !captured.sessionId || !connected || inFlight.current.has(captured.scope) || visible.identityError) return false
    let body = pending.current.get(captured.scope)
    if (!body) {
      if (!content.trim() || content.length > 16000 || captured.team.status !== 'active'
        || (coordination && (toolsLoading || toolsError || staleGrants || roster.errors.length))) return false
      body = { source_session_id: captured.sessionId, team_id: captured.team.id, team_version: captured.team.version,
        request_id: crypto.randomUUID(), content: content.trim(), context,
        source_ids: [...sourceIds], ...(coordination ? { member_grants: grants.map(grant => ({ ...grant,
          ...(grant.tool_names ? { tool_names: [...grant.tool_names] } : {}) })) } : {}) }
      savePending(body, { scope: captured.scope, teamId: captured.team.id, sessionId: captured.sessionId })
    }
    const sent = body
    inFlight.current.add(captured.scope)
    setView(previous => previous.scope === captured.scope ? { ...previous, pending: sent, posting: true, error: '' } : previous)
    try {
      const value = validateTeamTurn(await captured.api.createPersonalTeamTurn(sent),
        { teamId: captured.team.id, sessionId: captured.sessionId }, sent)
      const merged = retainTeamTurn(known.current.get(captured.scope + '\0' + value.id), value)
      known.current.set(captured.scope + '\0' + merged.id, merged)
      const needsAcknowledgement = pending.current.get(captured.scope)?.request_id === sent.request_id
      savePending(null, { scope: captured.scope, teamId: captured.team.id, sessionId: captured.sessionId })
      if (needsAcknowledgement) callbacks.current.onAccepted?.(sent, namespace)
      if (!alive.current || current.current.scope !== captured.scope || current.current.api !== captured.api) return false
      setView(previous => previous.scope === captured.scope ? { ...previous, pending: null,
        turns: [merged, ...previous.turns.filter(turn => turn.id !== merged.id)], error: '' } : previous)
      setRevision(value => value + 1)
      return true
    } catch (cause) {
      if (alive.current && current.current.scope === captured.scope && current.current.api === captured.api)
        setView(previous => previous.scope === captured.scope ? { ...previous, pending: sent, error: personalError(cause) } : previous)
      return false
    } finally {
      inFlight.current.delete(captured.scope)
      if (alive.current && current.current.scope === captured.scope) setView(previous => previous.scope === captured.scope
        ? { ...previous, posting: false } : previous)
    }
  }
  const cancel = async (turn: PersonalTeamTurn) => {
    const captured = current.current
    if (!captured.team || !captured.sessionId || turn.team_id !== captured.team.id || turn.team_session_id !== captured.sessionId) return
    const value = validateTeamTurn(await captured.api.cancelPersonalTeamTurn(turn.id),
      { teamId: captured.team.id, sessionId: captured.sessionId })
    if (value.id !== turn.id) throw new Error('停止回执与原团队任务不一致')
    const merged = retainTeamTurn(known.current.get(captured.scope + '\0' + value.id), value)
    known.current.set(captured.scope + '\0' + value.id, merged)
    if (alive.current && current.current.scope === captured.scope) {
      setView(previous => previous.scope === captured.scope ? { ...previous,
        turns: previous.turns.map(item => item.id === merged.id ? merged : item) } : previous)
      setRevision(value => value + 1)
    }
  }
  const abandonPending = () => {
    const captured = current.current
    if (!captured.team || !captured.sessionId || inFlight.current.has(captured.scope)) return
    savePending(null, { scope: captured.scope, teamId: captured.team.id, sessionId: captured.sessionId })
    setView(previous => previous.scope === captured.scope ? { ...previous, pending: null, identityError: false, error: '' } : previous)
  }
  return { ...visible, enabled: Boolean(scope && connected), coordination, roster, tools, toolsLoading, toolsError,
    grants, setGrants: (value: PersonalPeerGrant[]) => setSelectedGrants({ scope, value }), context,
    setContext: (value: string) => setContextState({ scope, value }), staleGrants, submit, cancel,
    abandonPending, refresh: () => setRevision(value => value + 1),
    canSubmit: !visible.identityError && !visible.loading && Boolean(visible.pending || (!coordination
      || (!toolsLoading && !toolsError && !staleGrants && !roster.errors.length))) }
}

export type TeamTurnsState = ReturnType<typeof useTeamTurns>
