import { useCallback, useEffect, useRef, useState } from 'react'
import { WorkbenchRequestError, type WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { WorkspaceArguments, WorkspaceConnection, WorkspaceContext, WorkspaceGrant, WorkspaceMutationOperation, WorkspaceOverview, WorkspaceReadResult, WorkspaceResponses } from '@naumi/shared/api/personalWorkspaces'
import { parseWorkspaceJson, validateConnection, validateGrant, validateRecipient, validateTicket, validateWorkspaceContext, validateWorkspaceOverview, validateWorkspaceResult, workspaceCanonical, workspaceHash, workspaceId } from './workspaceValidation'

export type WorkspaceApi = Pick<WorkbenchRuntimeClient, 'getPersonalWorkspaceContext' | 'executePersonalWorkspaceOperation'>
export const workspaceError = (cause: unknown) => cause instanceof Error ? cause.message : '操作未完成，请核对原记录'
export interface WorkspacePending { version: 1; request_id: string; operation: WorkspaceMutationOperation; workspace_key: string; resource_id?: string; expected_revision?: number }
export const workspacePendingKey = (namespace: string) => `naumi:workspace-request:${namespace}`
export function parseWorkspacePending(raw: string): WorkspacePending {
  const value = parseWorkspaceJson(raw) as WorkspacePending
  if (!value || Object.keys(value).some(key => !['version', 'request_id', 'operation', 'workspace_key', 'resource_id', 'expected_revision'].includes(key)) || value.version !== 1 || typeof value.request_id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(value.request_id) || !workspaceHash(value.workspace_key) || !['issue', 'connect', 'revoke', 'disconnect'].includes(value.operation)) throw new Error('原请求身份待核对')
  if (['revoke', 'disconnect'].includes(value.operation)) {
    if (!workspaceId(value.resource_id, value.operation === 'revoke' ? 'wg' : 'wc') || !Number.isSafeInteger(value.expected_revision) || value.expected_revision! < 1) throw new Error('原请求版本待核对')
  } else if (value.resource_id !== undefined || value.expected_revision !== undefined) throw new Error('原请求字段待核对')
  return value
}

export function useWorkspaceOverview({ api, namespace, connected }: { api: WorkspaceApi; namespace: string; connected: boolean }) {
  const key = workspacePendingKey(namespace), active = useRef(key); active.current = key
  const [stateKey, setStateKey] = useState(key)
  const [context, setContext] = useState<WorkspaceContext | null>(null), [connections, setConnections] = useState<WorkspaceConnection[]>([]), [grants, setGrants] = useState<WorkspaceGrant[]>([])
  const [view, setView] = useState<WorkspaceOverview | null>(null), [result, setResult] = useState<WorkspaceReadResult | null>(null)
  const [selection, setSelection] = useState<{ connection_id: string; item_id: string } | null>(null)
  const [loading, setLoading] = useState(false), [resultLoading, setResultLoading] = useState(false), [posting, setPosting] = useState(false)
  const [error, setError] = useState(''), [writeError, setWriteError] = useState(''), [identityError, setIdentityError] = useState('')
  const [notice, setNotice] = useState('')
  const [pending, setPending] = useState<WorkspacePending | null>(null), [token, setToken] = useState(0)
  const [completedRequest, setCompletedRequest] = useState<{ request_id: string; operation: WorkspaceMutationOperation; recovered: boolean } | null>(null)
  const live = useRef({ connected, context, connections, selection }); live.current = { connected, context, connections, selection }
  const flight = useRef(false), moreLock = useRef(false), writeLock = useRef(false), generation = useRef(0), resultGeneration = useRef(0)
  const refresh = useCallback(() => setToken(value => value + 1), [])
  useEffect(() => {
    setStateKey(key); setContext(null); setConnections([]); setGrants([]); setView(null); setResult(null); setSelection(null); setError(''); setWriteError(''); setNotice(''); setIdentityError(''); setPending(null); setCompletedRequest(null); setPosting(false); writeLock.current = false
    const restore = () => { try { const raw = localStorage.getItem(key); setPending(raw ? parseWorkspacePending(raw) : null); setIdentityError('') } catch (cause) { setIdentityError(workspaceError(cause)) } }
    restore(); window.addEventListener('storage', restore)
    return () => { window.removeEventListener('storage', restore); generation.current++; resultGeneration.current++ }
  }, [key])
  useEffect(() => {
    const controller = new AbortController(), run = ++generation.current
    let timer: ReturnType<typeof setTimeout> | undefined
    const valid = () => !controller.signal.aborted && active.current === key && run === generation.current && live.current.connected
    const load = async () => {
      if (!connected) { setView(null); setResult(null); setContext(null); setConnections([]); setGrants([]); return }
      if (flight.current || moreLock.current) { timer = setTimeout(() => void load(), 150); return }
      flight.current = true; setLoading(true)
      try {
        const ctx = validateWorkspaceContext(await api.getPersonalWorkspaceContext(controller.signal))
        const [listed, issued] = await Promise.all([api.executePersonalWorkspaceOperation('list', {}, controller.signal), api.executePersonalWorkspaceOperation('grants', {}, controller.signal)])
        const rows = listed.connections.map(row => validateConnection(row, ctx.workspace_key)), sourceGrants = issued.grants.map(validateGrant)
        if (new Set(rows.map(row => row.id)).size !== rows.length || sourceGrants.some(grant => grant.source.workspace_key !== ctx.workspace_key)) throw new Error('工作区记录身份待核对')
        const overview = validateWorkspaceOverview(await api.executePersonalWorkspaceOperation('overview', { limit: 50 }, controller.signal), rows)
        if (overview.sources.length !== rows.filter(row => row.status === 'active').length) throw new Error('工作区汇总缺少来源状态')
        if (!valid()) return
        setContext(ctx); setConnections(rows); setGrants(sourceGrants); setView(overview); setError('')
        const selected = live.current.selection, connection = rows.find(row => row.id === selected?.connection_id && row.status === 'active')
        if (selected && connection && overview.sources.find(source => source.connection_id === connection.id)?.availability === 'available') {
          const captured = resultGeneration.current
          try {
            const value = validateWorkspaceResult(await api.executePersonalWorkspaceOperation('result', selected, controller.signal), connection, selected.item_id)
            if (valid() && captured === resultGeneration.current) setResult(value)
          } catch (cause) { if (valid() && captured === resultGeneration.current) { setResult(null); setError(workspaceError(cause)) } }
        } else { resultGeneration.current++; setResult(null) }
      } catch (cause) { if (valid()) { resultGeneration.current++; setView(null); setResult(null); setConnections([]); setGrants([]); setContext(null); setError(workspaceError(cause)) } }
      finally { flight.current = false; if (valid()) { setLoading(false); timer = setTimeout(() => void load(), 12000) } }
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [api, connected, key, token])
  const closeResult = () => { resultGeneration.current++; setSelection(null); setResult(null); setResultLoading(false) }
  const openResult = async (connectionId: string, itemId: string) => {
    const connection = connections.find(row => row.id === connectionId && row.status === 'active'), run = ++resultGeneration.current, viewRun = generation.current
    setSelection({ connection_id: connectionId, item_id: itemId }); setResult(null); setResultLoading(true); setError('')
    if (!connection || !connected) { setResultLoading(false); return }
    try {
      const value = validateWorkspaceResult(await api.executePersonalWorkspaceOperation('result', { connection_id: connectionId, item_id: itemId }), connection, itemId)
      if (active.current === key && run === resultGeneration.current && viewRun === generation.current && live.current.connected && live.current.connections.some(row => row.id === connection.id && row.revision === connection.revision && workspaceCanonical(row.source) === workspaceCanonical(connection.source) && workspaceCanonical(row.recipient) === workspaceCanonical(connection.recipient))) setResult(value)
    } catch (cause) { if (active.current === key && run === resultGeneration.current) setError(workspaceError(cause)) }
    finally { if (active.current === key && run === resultGeneration.current) setResultLoading(false) }
  }
  const more = async (connectionId: string) => {
    const source = view?.sources.find(row => row.connection_id === connectionId)
    if (!source?.next_after || !connected || flight.current || moreLock.current) return
    const run = generation.current; moreLock.current = true; setLoading(true)
    try {
      const page = validateWorkspaceOverview(await api.executePersonalWorkspaceOperation('overview', { connection_id: connectionId, after: source.next_after, limit: 50 }), connections)
      if (page.sources.length !== 1 || page.sources[0].connection_id !== connectionId) throw new Error('来源分页身份不一致')
      if (active.current !== key || run !== generation.current || !live.current.connected) return
      const next = page.sources[0]
      setView(previous => {
        if (!previous) return null
        // A prior page can withhold unauthorized rows while also carrying a cursor.
        // Keep its partial evidence rather than declaring the merged source complete.
        const merged = previous.sources.map(row => row.connection_id !== connectionId ? row : next.availability === 'unavailable' ? next : ({ ...next, partial: row.partial || next.partial, entries: [...row.entries.filter(entry => !next.entries.some(fresh => fresh.item_id === entry.item_id)), ...next.entries] }))
        return { ...previous, sources: merged, partial: merged.some(row => row.partial || row.next_after !== null || row.availability === 'unavailable') }
      })
      if (next.availability === 'unavailable' && selection?.connection_id === connectionId) { resultGeneration.current++; setResult(null) }
    } catch (cause) { if (active.current === key && run === generation.current) { resultGeneration.current++; setView(null); setResult(null); setError(workspaceError(cause)) } }
    finally { moreLock.current = false; if (active.current === key && run === generation.current) setLoading(false) }
  }
  const acceptedIdentity = (identity: Record<string, unknown> | undefined, saved: WorkspacePending) => {
    if (!identity || !workspaceId(identity.id, ['issue', 'revoke'].includes(saved.operation) ? 'wg' : 'wc') || !Number.isSafeInteger(identity.revision) || Number(identity.revision) < 1 || identity.status !== (saved.operation === 'revoke' ? 'revoked' : saved.operation === 'disconnect' ? 'disconnected' : 'active') || saved.resource_id && (identity.id !== saved.resource_id || identity.revision !== saved.expected_revision! + 1)) throw new Error('原请求受理身份不一致')
    if (['issue', 'connect'].includes(saved.operation) && identity.revision !== 1) throw new Error('原请求受理版本不一致')
  }
  const submit = async <K extends WorkspaceMutationOperation>(operation: K, args: WorkspaceArguments[K]): Promise<WorkspaceResponses[K] | null> => {
    if (writeLock.current || pending || identityError || !connected || !context) return null
    writeLock.current = true; setPosting(true); setWriteError(''); setNotice('')
    const payload = args.payload, saved: WorkspacePending = { version: 1, request_id: payload.request_id, operation, workspace_key: context.workspace_key, ...('grant_id' in payload ? { resource_id: payload.grant_id, expected_revision: payload.expected_revision } : 'connection_id' in payload ? { resource_id: payload.connection_id, expected_revision: payload.expected_revision } : {}) }
    let sent = false
    try {
      if (localStorage.getItem(key)) throw new Error('请先核对原请求')
      localStorage.setItem(key, JSON.stringify(parseWorkspacePending(JSON.stringify(saved)))); setPending(saved)
      sent = true
      const value = await api.executePersonalWorkspaceOperation(operation, args)
      if (active.current !== key || !live.current.connected) return null
      if (value.accepted !== true || value.request_id !== saved.request_id) throw new Error('原请求回执不一致')
      if ('projection_available' in value && value.projection_available === false) {
        if (!['issue', 'connect'].includes(operation) || value.operation !== operation || value.workspace_key !== saved.workspace_key || value.content_transport !== 'requests' || 'ticket' in value || 'grant' in value || 'connection' in value) throw new Error('原请求受理状态待核对')
        acceptedIdentity(value.identity, saved)
        setNotice(operation === 'issue' ? '授权已受理。一次性凭据未保留，请撤销该授权后重新签发。' : '连接已受理，来源状态待核对')
      } else if (operation === 'issue') {
        const receipt = value as Exclude<WorkspaceResponses['issue'], { projection_available: false }>, body = args as WorkspaceArguments['issue']
        validateGrant(receipt.grant); validateTicket(receipt.ticket)
        if (receipt.grant.source.workspace_key !== saved.workspace_key || receipt.ticket.grant_id !== receipt.grant.id || receipt.grant.revision !== 1 || receipt.grant.status !== 'active' || workspaceCanonical(receipt.ticket.recipient) !== workspaceCanonical(body.payload.recipient) || workspaceCanonical(receipt.grant.recipient) !== workspaceCanonical(body.payload.recipient) || receipt.grant.source_actor_id !== body.payload.source_actor_id || receipt.grant.source_actor_version !== body.payload.source_actor_version || workspaceCanonical(receipt.ticket.source) !== workspaceCanonical(receipt.grant.source) || receipt.ticket.source_actor_id !== receipt.grant.source_actor_id || receipt.ticket.source_actor_version !== receipt.grant.source_actor_version || workspaceCanonical(receipt.ticket.scopes) !== workspaceCanonical(body.payload.scopes) || workspaceCanonical(receipt.grant.scopes) !== workspaceCanonical(body.payload.scopes) || receipt.ticket.expires_at !== receipt.grant.expires_at || Date.parse(receipt.grant.expires_at) !== Date.parse(body.payload.expires_at)) throw new Error('一次性凭据与原授权不一致')
      } else if (operation === 'connect') {
        const receipt = value as Exclude<WorkspaceResponses['connect'], { projection_available: false }>, body = args as WorkspaceArguments['connect']
        validateConnection(receipt.connection, saved.workspace_key)
        if (receipt.connection.status !== 'active' || receipt.connection.revision !== 1 || workspaceCanonical(receipt.connection.source) !== workspaceCanonical(body.payload.ticket.source) || workspaceCanonical(receipt.connection.recipient) !== workspaceCanonical(body.payload.ticket.recipient) || receipt.connection.source_actor_id !== body.payload.ticket.source_actor_id || receipt.connection.source_actor_version !== body.payload.ticket.source_actor_version || receipt.connection.endpoint !== body.payload.endpoint || receipt.connection.label !== body.payload.label || workspaceCanonical(receipt.connection.scopes) !== workspaceCanonical(body.payload.ticket.scopes) || receipt.connection.expires_at !== body.payload.ticket.expires_at) throw new Error('连接与原凭据不一致')
      } else acceptedIdentity((value as WorkspaceResponses['revoke']).identity, saved)
      localStorage.removeItem(key); setPending(null); setCompletedRequest({ request_id: saved.request_id, operation, recovered: false }); setView(null); setResult(null); refresh(); return value
    } catch (cause) {
      if (active.current !== key) return null
      if (sent && cause instanceof WorkbenchRequestError && cause.status >= 400 && cause.status < 500 && ![408, 425, 429].includes(cause.status)) { localStorage.removeItem(key); setPending(null); refresh() }
      setWriteError(workspaceError(cause)); return null
    } finally { if (active.current === key) { writeLock.current = false; setPosting(false) } }
  }
  const reconcile = async () => {
    if (!pending || !context || pending.workspace_key !== context.workspace_key || writeLock.current || !connected) return
    writeLock.current = true; setPosting(true); setWriteError(''); setNotice('')
    try {
      const receipt = await api.executePersonalWorkspaceOperation('requests', { request_id: pending.request_id })
      if (active.current !== key || !live.current.connected) return
      if (receipt.request_id !== pending.request_id || receipt.status !== 'accepted' || receipt.operation !== pending.operation) throw new Error('原请求尚待核对，未重新提交')
      acceptedIdentity(receipt.identity, pending)
      localStorage.removeItem(key); setPending(null); setCompletedRequest({ request_id: pending.request_id, operation: pending.operation, recovered: true }); setNotice(pending.operation === 'issue' ? '授权已受理。一次性凭据未保留，请撤销该授权后重新签发。' : '原请求已受理'); setView(null); setResult(null); refresh()
    } catch (cause) { if (active.current === key) setWriteError(workspaceError(cause)) }
    finally { if (active.current === key) { writeLock.current = false; setPosting(false) } }
  }
  const recipient = async (actorId: string) => {
    const value = await api.executePersonalWorkspaceOperation('recipient', { actor_id: actorId })
    if (active.current !== key || !live.current.connected || !context) throw new Error('当前工作区已变化')
    const card = validateRecipient(value.recipient, context.workspace_key), actor = context.actors.find(row => row.id === actorId)
    if (!actor || card.actor_id !== actorId || card.actor_version !== actor.version || card.owner_key !== context.owner_key) throw new Error('接收 Agent 身份已变化')
    return card
  }
  const current = connected && stateKey === key
  return { context: current ? context : null, connections: current ? connections : [], grants: current ? grants : [], view: current ? view : null, result: current ? result : null, selection: current ? selection : null, loading, resultLoading, posting, pending, completedRequest, identityError, notice, error: writeError || error, refresh, openResult, closeResult, more, submit, reconcile, recipient,
    locked: posting || Boolean(pending) || Boolean(identityError) || !connected || !context || Boolean(pending && context && pending.workspace_key !== context.workspace_key) }
}
export type WorkspaceState = ReturnType<typeof useWorkspaceOverview>
